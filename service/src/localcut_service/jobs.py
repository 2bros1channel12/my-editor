"""Persistent, local background job queue with safe artifact commit semantics."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import time
from typing import Any, Callable, Generic, Literal, Protocol, TypeVar
import uuid

from .persistence import ProjectStore, _connect, _json, _now, _sha256_file

JobState = Literal["queued", "running", "completed", "failed", "retrying", "cancelled"]
JobKind = Literal["proxy", "transcription", "analysis", "suggestion", "model_download", "indexing", "rendering", "planning"]
InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")
logger = logging.getLogger("localcut.jobs")


class JobCancelled(Exception):
    pass


class TransientJobError(RuntimeError):
    retryable = True


@dataclass(frozen=True)
class JobInput:
    project_path: str
    kind: JobKind
    payload: dict[str, Any]
    source_fingerprint: str
    pipeline_version: str = "1"
    asset_id: str | None = None
    model_id: str | None = None
    model_version: str | None = None
    generator_id: str = "localcut"
    generator_version: str = "0.1.0"
    config: dict[str, Any] = field(default_factory=dict)
    dependencies: tuple[str, ...] = ()
    max_attempts: int = 2
    retry_delay_seconds: float = 0.25


@dataclass(frozen=True)
class JobOutput:
    staging_path: str
    relative_path: str
    artifact_kind: str
    metadata: dict[str, Any] = field(default_factory=dict)
    model_license: str | None = None


@dataclass(frozen=True)
class JobProgress:
    progress: float
    stage: str
    message: str | None = None


class JobContext(Protocol):
    job_id: str
    input: JobInput

    def report(self, progress: float, stage: str, message: str | None = None) -> None: ...
    def check_cancelled(self) -> None: ...
    def staging_file(self, suffix: str = ".part") -> Path: ...


class JobHandler(Protocol[InputT, OutputT]):
    kind: JobKind
    retry_safe: bool

    def validate_input(self, payload: dict[str, Any]) -> InputT: ...
    def run(self, context: JobContext, payload: InputT) -> OutputT: ...
    def validate_output(self, output: OutputT) -> None: ...


class BasicHandler:
    """Default unavailable handlers provide the stable extension points."""
    retry_safe = True

    def __init__(self, kind: JobKind):
        self.kind = kind

    def validate_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Job payload must be an object.")
        return payload

    def run(self, context: JobContext, payload: dict[str, Any]) -> JobOutput:
        raise RuntimeError(f"No {self.kind} handler is registered yet.")

    def validate_output(self, output: JobOutput) -> None:
        if not isinstance(output, JobOutput):
            raise ValueError("Job handler returned an invalid output descriptor.")


class JobContextImpl:
    def __init__(self, queue: "JobQueue", job_id: str, job_input: JobInput, stage_root: Path):
        self.queue = queue
        self.job_id = job_id
        self.input = job_input
        self.stage_root = stage_root
        self._cancel = threading.Event()
        self._last_cancel_poll = 0.0

    def request_cancel(self) -> None:
        self._cancel.set()

    def report(self, progress: float, stage: str, message: str | None = None) -> None:
        self.check_cancelled()
        self.queue._progress(self.job_id, progress, stage, message)

    def check_cancelled(self) -> None:
        if self._cancel.is_set():
            self._cancel.set()
            raise JobCancelled("Job cancellation requested.")
        if self.queue._stop:
            raise TransientJobError("Service is stopping; retry this job on restart.")
        # Hot media loops may call this once per PCM window or decoded frame.
        # Cancellation through this queue sets the active context event directly;
        # poll SQLite periodically as a fallback without opening a connection for
        # every sample on long recordings.
        now = time.monotonic()
        if now - self._last_cancel_poll >= 0.25:
            self._last_cancel_poll = now
            if self.queue._cancel_requested(self.job_id):
                self._cancel.set()
                raise JobCancelled("Job cancellation requested.")

    def staging_file(self, suffix: str = ".part") -> Path:
        self.stage_root.mkdir(parents=True, exist_ok=True)
        return self.stage_root / f"output{suffix}"


HANDLER_KINDS: tuple[JobKind, ...] = ("proxy", "transcription", "analysis", "suggestion", "model_download", "indexing", "rendering", "planning")


class JobQueue:
    def __init__(self, data_dir: Path, *, concurrency: int = 2, handlers: dict[str, JobHandler[Any, Any]] | None = None, logger_: logging.Logger = logger, autostart: bool = True):
        self.data_dir = data_dir.resolve()
        self.database_path = self.data_dir / "jobs.sqlite3"
        self.lock_path = self.data_dir / "jobs.lock"
        self.stage_root = self.data_dir / "job-staging"
        self.concurrency = max(1, min(int(concurrency), 16))
        self.handlers = {kind: BasicHandler(kind) for kind in HANDLER_KINDS}
        self.handlers.update(handlers or {})
        self.log = logger_
        self._condition = threading.Condition()
        self._stop = False
        self._workers: list[threading.Thread] = []
        self._active_contexts: dict[str, JobContextImpl] = {}
        self._lock_stream = None
        self._acquire_process_lock()
        try:
            self._initialize()
            self.recover()
            if autostart:
                self.start()
        except Exception:
            self._release_process_lock()
            raise

    def _acquire_process_lock(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        stream = self.lock_path.open("a+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ImportError) as exc:
            stream.close()
            raise RuntimeError("Another Local Cut service already owns the persistent job queue.") from exc
        self._lock_stream = stream

    def _release_process_lock(self) -> None:
        stream = self._lock_stream
        self._lock_stream = None
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except (OSError, ImportError):
            pass
        stream.close()

    def _initialize(self) -> None:
        with _connect(self.database_path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS queue_jobs (id TEXT PRIMARY KEY,project_path TEXT NOT NULL,project_id TEXT NOT NULL,kind TEXT NOT NULL,payload_json TEXT NOT NULL,state TEXT NOT NULL,progress REAL NOT NULL DEFAULT 0,stage TEXT NOT NULL DEFAULT 'queued',message TEXT,attempts INTEGER NOT NULL DEFAULT 0,max_attempts INTEGER NOT NULL,retry_delay_seconds REAL NOT NULL DEFAULT 0.5,next_attempt_at TEXT,dedupe_key TEXT NOT NULL,source_fingerprint TEXT NOT NULL,output_json TEXT,error_json TEXT,cancel_requested INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,started_at TEXT,finished_at TEXT)")
            columns = {row[1] for row in db.execute("PRAGMA table_info(queue_jobs)")}
            if "input_json" not in columns:
                db.execute("ALTER TABLE queue_jobs ADD COLUMN input_json TEXT")
            db.execute("CREATE TABLE IF NOT EXISTS publications(job_id TEXT PRIMARY KEY,target TEXT NOT NULL,checksum TEXT NOT NULL,device INTEGER NOT NULL,inode INTEGER NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS queue_state_idx ON queue_jobs(state,next_attempt_at,created_at)")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS queue_active_dedupe_idx ON queue_jobs(project_id,dedupe_key) WHERE state IN ('queued','running','retrying')")
            db.execute("CREATE TABLE IF NOT EXISTS queue_dependencies (job_id TEXT NOT NULL REFERENCES queue_jobs(id) ON DELETE CASCADE,depends_on TEXT NOT NULL REFERENCES queue_jobs(id) ON DELETE CASCADE,PRIMARY KEY(job_id,depends_on),CHECK(job_id<>depends_on))")
            db.execute("CREATE INDEX IF NOT EXISTS queue_dependency_parent_idx ON queue_dependencies(depends_on)")
            db.commit()

    def _event(self, job_id: str, event: str, *, level: str = "info", details: dict[str, Any] | None = None) -> None:
        self.log.info("job_event", extra={"component": "jobs", "event": event, "job_id": job_id, "details": details or {}})
        try:
            row = self._get(job_id)
            if row:
                store = ProjectStore.open(Path(row["project_path"]))
                with _connect(store.database_path) as db:
                    store.add_job_event(db, job_id, level, event, details)
                    db.commit()
        except Exception as exc:
            self.log.warning("job_event_persist_failed", extra={"component": "jobs", "event": "job_event_persist_failed", "job_id": job_id, "error": exc.__class__.__name__})

    def _sync_project_job(self, row: dict[str, Any]) -> None:
        store = ProjectStore.open(Path(row["project_path"]))
        payload = json.loads(row["payload_json"])
        output_data = json.loads(row["output_json"]) if row.get("output_json") else None
        artifact_id = row.get("output_artifact_id") or (output_data or {}).get("artifactId")
        with _connect(store.database_path) as db:
            db.execute("INSERT INTO jobs(id,project_id,kind,state,progress,resumable,checkpoint_json,input_fingerprint,error_code,created_at,updated_at,input_json,output_json,stage,attempts,max_attempts,next_attempt_at,cancel_requested,dedupe_key,error_json,started_at,finished_at,can_retry,output_artifact_id,retry_delay_seconds) VALUES (?,?,?,?,?,0,'{}',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,progress=excluded.progress,error_code=excluded.error_code,updated_at=excluded.updated_at,input_json=excluded.input_json,output_json=excluded.output_json,stage=excluded.stage,attempts=excluded.attempts,max_attempts=excluded.max_attempts,next_attempt_at=excluded.next_attempt_at,cancel_requested=excluded.cancel_requested,dedupe_key=excluded.dedupe_key,error_json=excluded.error_json,started_at=excluded.started_at,finished_at=excluded.finished_at,can_retry=excluded.can_retry,output_artifact_id=COALESCE(excluded.output_artifact_id,jobs.output_artifact_id),retry_delay_seconds=excluded.retry_delay_seconds", (row["id"], row["project_id"], row["kind"], row["state"], row["progress"], row["source_fingerprint"], row.get("error_code"), row["created_at"], row["updated_at"], row.get("input_json") or row["payload_json"], row.get("output_json"), row["stage"], row["attempts"], row["max_attempts"], row.get("next_attempt_at"), row["cancel_requested"], row["dedupe_key"], row.get("error_json"), row.get("started_at"), row.get("finished_at"), int(bool(row.get("can_retry"))), artifact_id, row.get("retry_delay_seconds", 0.5)))
            db.execute("DELETE FROM job_dependencies WHERE job_id=?", (row["id"],))
            for dependency in row.get("dependencies", []):
                # Project-local shadow only; dependency IDs remain discoverable in queue DB.
                db.execute("INSERT OR IGNORE INTO job_dependencies(job_id,depends_on_job_id) SELECT ?,id FROM jobs WHERE id=?", (row["id"], dependency))
            db.commit()

    def _get(self, job_id: str) -> dict[str, Any] | None:
        with _connect(self.database_path) as db:
            row = db.execute("SELECT * FROM queue_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return None
            value = dict(row)
            value["dependencies"] = [item[0] for item in db.execute("SELECT depends_on FROM queue_dependencies WHERE job_id=?", (job_id,))]
            return value

    def _hash_input(self, job: JobInput, project_id: str, payload: dict[str, Any] | None = None) -> str:
        canonical = {"projectId": project_id, "kind": job.kind, "payload": payload if payload is not None else job.payload, "sourceFingerprint": job.source_fingerprint, "pipelineVersion": job.pipeline_version, "assetId": job.asset_id, "modelId": job.model_id, "modelVersion": job.model_version, "generatorId": job.generator_id, "generatorVersion": job.generator_version, "config": job.config}
        return hashlib.sha256(_json(canonical).encode("utf-8")).hexdigest()

    def register_handler(self, kind: JobKind, handler: JobHandler[Any, Any]) -> None:
        if kind not in HANDLER_KINDS or handler.kind != kind:
            raise ValueError("Handler kind does not match a supported job kind.")
        self.handlers[kind] = handler

    def submit(self, job: JobInput) -> dict[str, Any]:
        handler = self.handlers.get(job.kind)
        if handler is None:
            raise ValueError(f"Unsupported job kind: {job.kind}")
        validated = handler.validate_input(job.payload)
        if not isinstance(validated, dict):
            raise ValueError("Job input validator must return an object-compatible value.")
        if hasattr(handler, "validate_job"):
            handler.validate_job(job)
        store = ProjectStore.open(Path(job.project_path))
        dedupe_key = self._hash_input(job, store.project_id, validated)
        artifact = store.find_valid_artifact(dedupe_key)
        if artifact:
            return {"id": None, "state": "completed", "deduplicated": True, "artifactId": artifact["id"], "output": {"relativePath": artifact["relative_path"]}}
        job_id = str(uuid.uuid4())
        now = _now()
        payload_json = _json(validated)
        try:
            with _connect(self.database_path) as db:
                db.execute("BEGIN IMMEDIATE")
                # An identical active job is the single producer for this input.
                existing = db.execute("SELECT * FROM queue_jobs WHERE project_id=? AND dedupe_key=? AND state IN ('queued','running','retrying')", (store.project_id, dedupe_key)).fetchone()
                if existing:
                    result = {"id": existing["id"], "state": existing["state"], "deduplicated": True}
                    db.rollback()
                    return result
                db.execute("INSERT INTO queue_jobs(id,project_path,project_id,kind,payload_json,state,progress,stage,attempts,max_attempts,dedupe_key,source_fingerprint,created_at,updated_at,retry_delay_seconds) VALUES (?,?,?,?,?,'queued',0,'queued',0,?,?,?,?,?,?)", (job_id, str(store.root), store.project_id, job.kind, payload_json, max(1, min(job.max_attempts, 10)), dedupe_key, job.source_fingerprint, now, now, max(0.05, min(float(job.retry_delay_seconds), 60))))
                db.execute("UPDATE queue_jobs SET input_json=? WHERE id=?", (_json({**asdict(job), "project_path": str(store.root), "payload": validated}), job_id))
                for dependency in job.dependencies:
                    dep = db.execute("SELECT id,state FROM queue_jobs WHERE id=?", (dependency,)).fetchone()
                    if dep is None:
                        raise ValueError(f"Dependency job {dependency} does not exist.")
                    if dep["state"] in {"failed", "cancelled"}:
                        raise ValueError(f"Dependency job {dependency} is already {dep['state']}.")
                    db.execute("INSERT INTO queue_dependencies(job_id,depends_on) VALUES (?,?)", (job_id, dependency))
                if self._has_dependency_cycle(db, job_id):
                    raise ValueError("Job dependencies contain a cycle.")
                db.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError("Job could not be queued because it duplicates active work or has invalid dependencies.") from exc
        row = self._get(job_id)
        self._sync_project_job(row)
        self._event(job_id, "queued", details={"kind": job.kind})
        with self._condition:
            self._condition.notify_all()
        return {"id": job_id, "state": "queued", "deduplicated": False}

    @staticmethod
    def _has_dependency_cycle(db: sqlite3.Connection, job_id: str) -> bool:
        return db.execute("WITH RECURSIVE ancestors(id) AS (SELECT depends_on FROM queue_dependencies WHERE job_id=? UNION SELECT d.depends_on FROM queue_dependencies d JOIN ancestors a ON d.job_id=a.id) SELECT 1 FROM ancestors WHERE id=? LIMIT 1", (job_id, job_id)).fetchone() is not None

    def list_jobs(self, project_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with _connect(self.database_path) as db:
            if project_id:
                rows = db.execute("SELECT * FROM queue_jobs WHERE project_id=? ORDER BY created_at DESC LIMIT ?", (project_id, max(1, min(limit, 500)))).fetchall()
            else:
                rows = db.execute("SELECT * FROM queue_jobs ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),)).fetchall()
            values = []
            for row in rows:
                item = dict(row)
                item["payload"] = json.loads(item.pop("payload_json"))
                item["output"] = json.loads(item.pop("output_json")) if item.get("output_json") else None
                item["error"] = json.loads(item.pop("error_json")) if item.get("error_json") else None
                item["cancelRequested"] = bool(item.pop("cancel_requested"))
                item["dependencies"] = [dep[0] for dep in db.execute("SELECT depends_on FROM queue_dependencies WHERE job_id=?", (row["id"],))]
                values.append(item)
        # UI receives stable user-facing camelCase plus core fields.
        remap = {"project_path":"projectPath","project_id":"projectId","source_fingerprint":"sourceFingerprint","dedupe_key":"dedupeKey","max_attempts":"maxAttempts","next_attempt_at":"nextAttemptAt","created_at":"createdAt","updated_at":"updatedAt","started_at":"startedAt","finished_at":"finishedAt"}
        return [{remap.get(key, key): value for key, value in item.items()} for item in values]

    def cancel(self, job_id: str) -> dict[str, Any]:
        with _connect(self.database_path) as db:
            row = db.execute("SELECT state FROM queue_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise ValueError("Job was not found.")
            if row["state"] in {"completed", "failed", "cancelled"}:
                return {"id": job_id, "state": row["state"]}
            if row["state"] == "queued" or row["state"] == "retrying":
                db.execute("UPDATE queue_jobs SET state='cancelled',stage='cancelled',cancel_requested=1,finished_at=?,updated_at=? WHERE id=?", (_now(), _now(), job_id))
            else:
                db.execute("UPDATE queue_jobs SET cancel_requested=1,stage='cancelling',updated_at=? WHERE id=?", (_now(), job_id))
            db.commit()
        self._refresh_shadow(job_id)
        self._event(job_id, "cancel_requested")
        with self._condition:
            active = self._active_contexts.get(job_id)
            if active is not None:
                active.request_cancel()
            self._condition.notify_all()
        return {"id": job_id, "state": "cancelled" if row["state"] != "running" else "running", "cancelRequested": True}

    def retry(self, job_id: str) -> dict[str, Any]:
        with _connect(self.database_path) as db:
            row = db.execute("SELECT state FROM queue_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise ValueError("Job was not found.")
            if row["state"] not in {"failed", "cancelled"}:
                raise ValueError("Only failed or cancelled jobs can be retried.")
            db.execute("UPDATE queue_jobs SET state='retrying',progress=0,stage='retrying',attempts=0,cancel_requested=0,error_json=NULL,next_attempt_at=NULL,finished_at=NULL,updated_at=? WHERE id=?", (_now(), job_id))
            db.commit()
        self._refresh_shadow(job_id)
        with self._condition:
            self._condition.notify_all()
        return {"id": job_id, "state": "retrying"}

    def _refresh_shadow(self, job_id: str) -> None:
        row = self._get(job_id)
        if row:
            self._sync_project_job(row)

    def _cancel_requested(self, job_id: str) -> bool:
        with _connect(self.database_path) as db:
            row = db.execute("SELECT cancel_requested FROM queue_jobs WHERE id=?", (job_id,)).fetchone()
            return bool(row and row[0])

    def _progress(self, job_id: str, progress: float, stage: str, message: str | None) -> None:
        value = max(0.0, min(float(progress), 0.99))
        with _connect(self.database_path) as db:
            db.execute("UPDATE queue_jobs SET progress=?,stage=?,message=?,updated_at=? WHERE id=? AND state='running' AND cancel_requested=0", (value, str(stage)[:120], str(message)[:500] if message else None, _now(), job_id))
            db.commit()
        self._refresh_shadow(job_id)
        self._event(job_id, "progress", details={"progress": value, "stage": str(stage)[:120], "message": str(message)[:200] if message else None})

    def _claim(self) -> dict[str, Any] | None:
        with _connect(self.database_path) as db:
            db.execute("BEGIN IMMEDIATE")
            now = _now()
            # Dependents of terminal unsuccessful jobs are explicitly failed.
            db.execute("UPDATE queue_jobs SET state='failed',stage='dependency_failed',error_json=?,finished_at=?,updated_at=? WHERE state IN ('queued','retrying') AND id IN (SELECT d.job_id FROM queue_dependencies d JOIN queue_jobs p ON p.id=d.depends_on WHERE p.state IN ('failed','cancelled'))", (_json({"code":"dependency_failed","message":"A required job failed or was cancelled."}), now, now))
            row = db.execute("SELECT j.* FROM queue_jobs j WHERE j.state IN ('queued','retrying') AND j.cancel_requested=0 AND (j.next_attempt_at IS NULL OR j.next_attempt_at<=?) AND NOT EXISTS(SELECT 1 FROM queue_dependencies d JOIN queue_jobs p ON p.id=d.depends_on WHERE d.job_id=j.id AND p.state<>'completed') ORDER BY j.created_at LIMIT 1", (now,)).fetchone()
            if row is None:
                db.commit()
                return None
            db.execute("UPDATE queue_jobs SET state='running',stage='starting',attempts=attempts+1,started_at=COALESCE(started_at,?),updated_at=? WHERE id=?", (now, now, row["id"]))
            db.commit()
            value = dict(db.execute("SELECT * FROM queue_jobs WHERE id=?", (row["id"],)).fetchone())
            value["dependencies"] = [item[0] for item in db.execute("SELECT depends_on FROM queue_dependencies WHERE job_id=?", (row["id"],))]
        return value

    def _finish_cancelled(self, job_id: str) -> None:
        self._cleanup_stage(job_id)
        with _connect(self.database_path) as db:
            db.execute("UPDATE queue_jobs SET state='cancelled',stage='cancelled',finished_at=?,updated_at=? WHERE id=? AND state='running' AND cancel_requested=1", (_now(), _now(), job_id))
            db.commit()
        self._cleanup_stage(job_id)
        self._refresh_shadow(job_id)
        self._event(job_id, "cancelled")

    def _project_stage(self, row: dict[str, Any]) -> Path:
        return Path(row["project_path"]).resolve() / ".localcut-staging" / row["id"]

    def _cleanup_stage(self, job_id: str) -> None:
        row = self._get(job_id)
        roots = [self.stage_root / job_id]
        if row:
            roots.append(self._project_stage(row))
        for root in roots:
            if root.is_symlink():
                root.unlink(missing_ok=True)
            elif root.is_dir():
                shutil.rmtree(root, ignore_errors=True)

    def _cleanup_publication(self, job_id: str) -> None:
        with _connect(self.database_path) as db:
            record = db.execute("SELECT * FROM publications WHERE job_id=?", (job_id,)).fetchone()
        if not record:
            return
        row = self._get(job_id)
        try:
            store = ProjectStore.open(Path(row["project_path"]))
            target = Path(record["target"])
            target.resolve().relative_to(store.root.resolve())
            with _connect(store.database_path) as db:
                registered = db.execute("SELECT 1 FROM artifacts WHERE relative_path=? AND status='ready'", (target.relative_to(store.root).as_posix(),)).fetchone()
            if not registered and target.exists() and not target.is_symlink():
                stat = target.stat()
                if stat.st_dev == record["device"] and stat.st_ino == record["inode"] and _sha256_file(target) == record["checksum"]:
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
        except (OSError, ValueError, sqlite3.DatabaseError):
            return
        with _connect(self.database_path) as db:
            db.execute("DELETE FROM publications WHERE job_id=?", (job_id,))
            db.commit()

    def _execute(self, row: dict[str, Any]) -> None:
        job_id = row["id"]
        handler = self.handlers[row["kind"]]
        envelope = json.loads(row["input_json"]) if row.get("input_json") else dict(project_path=row["project_path"], kind=row["kind"], payload=json.loads(row["payload_json"]), source_fingerprint=row["source_fingerprint"], max_attempts=row["max_attempts"])
        job_input = JobInput(**envelope)
        context = JobContextImpl(self, job_id, job_input, self._project_stage(row))
        with self._condition:
            self._active_contexts[job_id] = context
        self._sync_project_job(row)
        self._event(job_id, "started", details={"attempt": row["attempts"]})
        try:
            context.check_cancelled()
            payload = handler.validate_input(job_input.payload)
            output = handler.run(context, payload)
            context.check_cancelled()
            handler.validate_output(output)
            if not isinstance(output, JobOutput):
                raise ValueError("Job handlers must return a JobOutput descriptor.")
            if Path(output.staging_path).is_symlink():
                raise ValueError("Artifact symlinks are not allowed.")
            stage_path = Path(output.staging_path).resolve(strict=True)
            stage_path.relative_to(context.stage_root.resolve(strict=True))
            target = (ProjectStore.open(Path(job_input.project_path)).root / output.relative_path).resolve(strict=False)
            target.relative_to(ProjectStore.open(Path(job_input.project_path)).root.resolve())
            if not stage_path.exists() or (stage_path.is_file() and stage_path.stat().st_size == 0):
                raise ValueError("Job output is missing or empty.")
            digest = _sha256_file(stage_path)
            store = ProjectStore.open(Path(job_input.project_path))
            with _connect(store.database_path) as db:
                artifact_config = {**job_input.config, **output.metadata, "jobDedupeKey": row["dedupe_key"]}
                valid = db.execute("SELECT 1 FROM artifacts WHERE project_id=? AND kind=? AND source_fingerprint=? AND config_json=? AND status='ready' AND checksum_sha256=?", (store.project_id, output.artifact_kind, job_input.source_fingerprint, _json(artifact_config), digest)).fetchone()
            if valid:
                with _connect(store.database_path) as db:
                    artifact_row = db.execute("SELECT id,relative_path FROM artifacts WHERE project_id=? AND kind=? AND source_fingerprint=? AND config_json=? AND status='ready' AND checksum_sha256=? LIMIT 1", (store.project_id, output.artifact_kind, job_input.source_fingerprint, _json(artifact_config), digest)).fetchone()
                if artifact_row is None:
                    raise RuntimeError("Validated artifact disappeared before reuse.")
                cached_path = store.root / artifact_row["relative_path"]
                cached_path.resolve().relative_to(store.root.resolve())
                if _sha256_file(cached_path) != digest:
                    raise ValueError("Cached artifact no longer matches its checksum.")
                artifact_id = artifact_row["id"]
                output_json = _json({"artifactId": artifact_id, "relativePath": artifact_row["relative_path"], "checksumSha256": digest, **output.metadata})
                self._cleanup_stage(job_id)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    raise FileExistsError("Refusing to overwrite an existing artifact path.")
                stat = stage_path.stat()
                with _connect(self.database_path) as journal:
                    journal.execute("INSERT OR REPLACE INTO publications VALUES (?,?,?,?,?)", (job_id, str(target), digest, stat.st_dev, stat.st_ino))
                    journal.commit()
                os.rename(stage_path, target)
                self._cleanup_stage(job_id)
                artifact_id = str(uuid.uuid4())
                output_json = _json({"artifactId": artifact_id, "relativePath": target.relative_to(store.root).as_posix(), "checksumSha256": digest, **output.metadata})
                with _connect(store.database_path) as db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("INSERT INTO artifacts(id,project_id,asset_id,kind,relative_path,status,source_fingerprint,generator_id,generator_version,model_id,model_version,model_license,config_json,checksum_sha256,created_at) VALUES (?,?,?,?,?,'ready',?,?,?,?,?,?,?,?,?)", (artifact_id, store.project_id, job_input.asset_id, output.artifact_kind, target.relative_to(store.root).as_posix(), job_input.source_fingerprint, job_input.generator_id, job_input.generator_version, job_input.model_id, job_input.model_version, output.model_license, _json(artifact_config), digest, _now()))
                    if hasattr(handler, "commit_output"):
                        handler.commit_output(db, store, job_input, artifact_id, target, output.metadata)
                    with _connect(self.database_path) as qdb:
                        qdb.execute("BEGIN IMMEDIATE")
                        state = qdb.execute("SELECT state,cancel_requested FROM queue_jobs WHERE id=?", (job_id,)).fetchone()
                        if state is None or state["cancel_requested"] or state["state"] != "running":
                            qdb.rollback()
                            db.rollback()
                            raise JobCancelled("Cancellation won before artifact commit.")
                        db.execute("UPDATE jobs SET state='completed',progress=1,stage='completed',output_json=?,output_artifact_id=?,finished_at=?,updated_at=? WHERE id=?", (output_json, artifact_id, _now(), _now(), job_id))
                        qdb.execute("UPDATE queue_jobs SET state='completed',progress=1,stage='completed',output_json=?,finished_at=?,updated_at=? WHERE id=?", (output_json, _now(), _now(), job_id))
                        db.commit()
                        qdb.commit()
                self._cleanup_stage(job_id)
            if valid:
                self._complete_existing(row, artifact_id, output_json)
            else:
                self._refresh_shadow(job_id)
            self._event(job_id, "completed", details={"artifactId": artifact_id})
        except JobCancelled:
            self._cleanup_publication(job_id)
            self._finish_cancelled(job_id)
        except Exception as exc:
            self._cleanup_stage(job_id)
            # A project commit may have succeeded immediately before the queue
            # acknowledgement failed. Preserve and reconcile that validated output.
            try:
                project = ProjectStore.open(Path(row["project_path"]))
                with _connect(project.database_path) as pdb:
                    committed = pdb.execute("SELECT j.output_json,a.relative_path,a.checksum_sha256 FROM jobs j JOIN artifacts a ON a.id=j.output_artifact_id WHERE j.id=? AND j.state='completed' AND a.status='ready'", (job_id,)).fetchone()
                if committed:
                    committed_path = (project.root / committed["relative_path"]).resolve(strict=True)
                    committed_path.relative_to(project.root.resolve())
                    if _sha256_file(committed_path) == committed["checksum_sha256"]:
                        with _connect(self.database_path) as qdb:
                            qdb.execute("UPDATE queue_jobs SET state='completed',progress=1,stage='recovered_committed_output',output_json=?,finished_at=?,updated_at=? WHERE id=?", (committed["output_json"], _now(), _now(), job_id))
                            qdb.commit()
                        self._refresh_shadow(job_id)
                        self._event(job_id, "completed", details={"recoveredCommit": True})
                        return
            except (OSError, ValueError, sqlite3.DatabaseError):
                pass
            self._cleanup_publication(job_id)
            retryable = bool(getattr(exc, "retryable", False)) and bool(handler.retry_safe)
            with _connect(self.database_path) as db:
                current = db.execute("SELECT attempts,max_attempts,cancel_requested FROM queue_jobs WHERE id=?", (job_id,)).fetchone()
                if current and current["cancel_requested"]:
                    db.execute("UPDATE queue_jobs SET state='cancelled',stage='cancelled',finished_at=?,updated_at=? WHERE id=?", (_now(), _now(), job_id))
                elif current and retryable and current["attempts"] < current["max_attempts"]:
                    delay = min(60, row["retry_delay_seconds"] * 2 ** (current["attempts"] - 1))
                    next_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
                    db.execute("UPDATE queue_jobs SET state='retrying',stage='retrying',error_json=?,next_attempt_at=?,updated_at=? WHERE id=?", (_json({"code":"transient_error","message":exc.__class__.__name__}), next_at, _now(), job_id))
                else:
                    db.execute("UPDATE queue_jobs SET state='failed',stage='failed',error_json=?,finished_at=?,updated_at=? WHERE id=?", (_json({"code":"job_failed","message":str(exc)[:500]}), _now(), _now(), job_id))
                db.commit()
            self._refresh_shadow(job_id)
            self._event(job_id, "attempt_failed", level="error", details={"error": exc.__class__.__name__, "retryable": retryable})
        finally:
            self._cleanup_publication(job_id)
            with self._condition:
                self._active_contexts.pop(job_id, None)
                self._condition.notify_all()

    def _complete_existing(self, row: dict[str, Any], artifact_id: str, output_json: str) -> None:
        with _connect(self.database_path) as db:
            db.execute("UPDATE queue_jobs SET state='completed',progress=1,stage='completed',output_json=?,finished_at=?,updated_at=? WHERE id=? AND state='running' AND cancel_requested=0", (output_json, _now(), _now(), row["id"]))
            db.commit()
        self._refresh_shadow(row["id"])

    def start(self) -> None:
        if self._workers:
            return
        self._stop = False
        for index in range(self.concurrency):
            worker = threading.Thread(target=self._worker, name=f"localcut-job-{index+1}", daemon=True)
            worker.start()
            self._workers.append(worker)

    def _worker(self) -> None:
        while True:
            with self._condition:
                if self._stop:
                    return
            row = self._claim()
            if row:
                self._execute(row)
                continue
            with self._condition:
                if self._stop:
                    return
                self._condition.wait(timeout=0.25)

    def recover(self) -> None:
        with _connect(self.database_path) as db:
            db.execute("BEGIN IMMEDIATE")
            now = _now()
            running = db.execute("SELECT * FROM queue_jobs WHERE state='running'").fetchall()
            for row in running:
                # The project DB is committed first. Reconcile a crash between
                # its commit and the queue DB acknowledgement without rerunning.
                try:
                    project = ProjectStore.open(Path(row["project_path"]))
                    with _connect(project.database_path) as project_db:
                        committed = project_db.execute("SELECT j.output_json,j.output_artifact_id,a.relative_path,a.checksum_sha256 FROM jobs j JOIN artifacts a ON a.id=j.output_artifact_id WHERE j.id=? AND j.state='completed' AND a.status='ready'", (row["id"],)).fetchone()
                    if committed:
                        artifact_path = (project.root / committed["relative_path"]).resolve(strict=True)
                        artifact_path.relative_to(project.root.resolve())
                        if artifact_path.exists() and _sha256_file(artifact_path) == committed["checksum_sha256"]:
                            db.execute("UPDATE queue_jobs SET state='completed',progress=1,stage='recovered_committed_output',output_json=?,finished_at=?,updated_at=? WHERE id=?", (committed["output_json"], now, now, row["id"]))
                            continue
                except (OSError, ValueError, sqlite3.DatabaseError):
                    pass
                handler = self.handlers.get(row["kind"])
                if row["cancel_requested"]:
                    db.execute("UPDATE queue_jobs SET state='cancelled',stage='cancelled',finished_at=?,updated_at=? WHERE id=?", (now, now, row["id"]))
                elif handler and handler.retry_safe and row["attempts"] < row["max_attempts"]:
                    db.execute("UPDATE queue_jobs SET state='retrying',stage='recovered_after_restart',next_attempt_at=NULL,error_json=?,updated_at=? WHERE id=?", (_json({"code":"interrupted","message":"Service stopped while job was running; retrying safely."}), now, row["id"]))
                else:
                    db.execute("UPDATE queue_jobs SET state='failed',stage='interrupted',error_json=?,finished_at=?,updated_at=? WHERE id=?", (_json({"code":"interrupted","message":"Service stopped while non-resumable job was running."}), now, now, row["id"]))
            db.commit()
        self.stage_root.mkdir(parents=True, exist_ok=True)
        with _connect(self.database_path) as db:
            active = {row[0] for row in db.execute("SELECT id FROM queue_jobs WHERE state='running'")}
            owned = {row[0] for row in db.execute("SELECT id FROM queue_jobs")}
            recoverable = [dict(row) for row in db.execute("SELECT * FROM queue_jobs WHERE state IN ('cancelled','retrying','failed','completed')")]
            for row in recoverable:
                self._sync_project_job(row)
        for row in recoverable:
            self._cleanup_publication(row["id"])
            self._cleanup_stage(row["id"])
        for stage in self.stage_root.iterdir():
            if stage.is_dir() and stage.name in owned and stage.name not in active:
                self._cleanup_stage(stage.name)

    def wait(self, job_id: str, timeout: float = 5.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self._get(job_id)
            if row and row["state"] in {"completed", "failed", "cancelled"}:
                return next((item for item in self.list_jobs(row["project_id"]) if item["id"] == job_id), row)
            time.sleep(0.02)
        row = self._get(job_id)
        return row or {}

    def close(self, timeout: float = 5.0) -> None:
        with self._condition:
            self._stop = True
            self._condition.notify_all()
        for worker in self._workers:
            worker.join(timeout=timeout)
        if any(worker.is_alive() for worker in self._workers):
            raise RuntimeError("A job has not stopped; retaining queue ownership until process exit.")
        self._workers.clear()
        self._release_process_lock()

