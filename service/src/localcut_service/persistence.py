"""Versioned, nondestructive local project persistence.

SQLite is canonical for indexed/editable state. `project.json` is only an atomically
written human-readable metadata sidecar. Original media paths are only read to
fingerprint assets; this module never opens them for writing.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from typing import Any, Callable, Iterator
import uuid

PROJECT_FORMAT_VERSION = 1
DATABASE_SCHEMA_VERSION = 4
PROJECT_FILE = "project.json"
DATABASE_FILE = "project.sqlite3"
RECOVERY_CHECKPOINT_MAX_REVISIONS = 10
RECOVERY_CHECKPOINT_MAX_AGE_SECONDS = 30
RECOVERY_CHECKPOINT_RETENTION = 5
logger = logging.getLogger(__name__)


class ProjectError(RuntimeError):
    """Base class for actionable project persistence errors."""


class UnsupportedSchemaError(ProjectError):
    pass


class CorruptProjectError(ProjectError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def atomic_write(path: Path, contents: bytes, *, before_replace: Any = None) -> None:
    """Write and fsync a same-directory temp file, then atomically replace target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        if before_replace is not None:
            before_replace(temp_path, path)
        os.replace(temp_path, path)
        # Best-effort directory metadata flush (not available on every platform).
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _sha256_file(path: Path, check_cancelled: Callable[[], None] | None = None) -> str:
    if check_cancelled:
        check_cancelled()
    if path.is_symlink():
        raise ValueError("Artifact symlinks are not allowed.")
    digest = hashlib.sha256()
    if path.is_dir():
        files = sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix())
        if not files:
            raise ValueError("Artifact bundle is empty.")
        for item in files:
            if check_cancelled:
                check_cancelled()
            if item.is_symlink():
                raise ValueError("Artifact symlinks are not allowed.")
            relative = item.relative_to(path).as_posix().encode("utf-8")
            digest.update((b"D" if item.is_dir() else b"F") + len(relative).to_bytes(8, "big") + relative)
            if item.is_file():
                digest.update(bytes.fromhex(_sha256_file(item, check_cancelled)))
            elif not item.is_dir():
                raise ValueError("Artifact contains a non-regular file.")
        return digest.hexdigest()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            if check_cancelled:
                check_cancelled()
            digest.update(block)
    return digest.hexdigest()


def _portable_source_path(path: Path, project_root: Path) -> tuple[str, str]:
    absolute = path.expanduser().resolve(strict=True)
    try:
        relative = absolute.relative_to(project_root.resolve())
        return relative.as_posix(), "project-relative"
    except ValueError:
        # Linked media remains outside the project; retain a file URI for platform
        # normalization and re-evaluate relative hints when the project is moved.
        return absolute.as_uri(), "file-uri"


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    try:
        yield connection
    finally:
        connection.close()


def _migration_1(connection: sqlite3.Connection) -> None:
    schema = """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            state_revision INTEGER NOT NULL DEFAULT 0,
            settings_json TEXT NOT NULL DEFAULT '{}',
            timebase_num INTEGER NOT NULL DEFAULT 1,
            timebase_den INTEGER NOT NULL DEFAULT 30,
            last_opened_at TEXT
        );
        CREATE TABLE assets (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            original_uri TEXT NOT NULL,
            path_mode TEXT NOT NULL CHECK(path_mode IN ('project-relative','file-uri')),
            last_known_path TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            duration_ticks INTEGER,
            timebase_num INTEGER NOT NULL DEFAULT 1,
            timebase_den INTEGER NOT NULL DEFAULT 1000000,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            relink_status TEXT NOT NULL DEFAULT 'available',
            created_at TEXT NOT NULL
        );
        CREATE INDEX assets_project_idx ON assets(project_id);
        CREATE TABLE media_metadata (
            asset_id TEXT PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
            probe_tool TEXT NOT NULL,
            probe_version TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE artifacts (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            asset_id TEXT REFERENCES assets(id) ON DELETE SET NULL,
            kind TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ready',
            source_fingerprint TEXT NOT NULL,
            generator_id TEXT NOT NULL,
            generator_version TEXT NOT NULL,
            model_id TEXT,
            model_version TEXT,
            model_license TEXT,
            config_json TEXT NOT NULL DEFAULT '{}',
            checksum_sha256 TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX artifacts_asset_kind_idx ON artifacts(asset_id, kind);
        CREATE TABLE transcripts (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
            language TEXT NOT NULL,
            status TEXT NOT NULL,
            generator_id TEXT NOT NULL,
            generator_version TEXT NOT NULL,
            model_id TEXT,
            model_version TEXT,
            source_fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE transcript_words (
            id TEXT PRIMARY KEY,
            transcript_id TEXT NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL,
            source_in_ticks INTEGER NOT NULL,
            source_out_ticks INTEGER NOT NULL,
            text TEXT NOT NULL,
            confidence REAL,
            speaker_ordinal INTEGER,
            is_user_corrected INTEGER NOT NULL DEFAULT 0,
            is_protected INTEGER NOT NULL DEFAULT 0,
            CHECK(source_in_ticks >= 0 AND source_out_ticks > source_in_ticks),
            UNIQUE(transcript_id, ordinal)
        );
        CREATE INDEX transcript_words_time_idx ON transcript_words(transcript_id, source_in_ticks);
        CREATE TABLE analyses (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            source_in_ticks INTEGER NOT NULL,
            source_out_ticks INTEGER NOT NULL,
            result_json TEXT NOT NULL,
            confidence REAL,
            generator_id TEXT NOT NULL,
            generator_version TEXT NOT NULL,
            model_id TEXT,
            model_version TEXT,
            source_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ready',
            created_at TEXT NOT NULL,
            CHECK(source_in_ticks >= 0 AND source_out_ticks > source_in_ticks)
        );
        CREATE INDEX analyses_asset_range_idx ON analyses(asset_id, source_in_ticks, source_out_ticks);
        CREATE TABLE briefs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            revision INTEGER NOT NULL,
            brief_text TEXT NOT NULL,
            constraints_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(project_id, revision)
        );
        CREATE TABLE style_traits (
            id TEXT PRIMARY KEY,
            brief_id TEXT NOT NULL REFERENCES briefs(id) ON DELETE CASCADE,
            trait_key TEXT NOT NULL,
            trait_value_json TEXT NOT NULL,
            origin TEXT NOT NULL CHECK(origin IN ('user','reference-derived','default')),
            generator_id TEXT NOT NULL,
            generator_version TEXT NOT NULL,
            model_id TEXT,
            model_version TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(brief_id, trait_key)
        );
        CREATE TABLE edit_plans (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            brief_id TEXT REFERENCES briefs(id) ON DELETE SET NULL,
            revision INTEGER NOT NULL,
            status TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            rationale_json TEXT NOT NULL DEFAULT '[]',
            generator_id TEXT NOT NULL,
            generator_version TEXT NOT NULL,
            model_id TEXT,
            model_version TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(project_id, revision)
        );
        CREATE TABLE timelines (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            revision INTEGER NOT NULL,
            timebase_num INTEGER NOT NULL,
            timebase_den INTEGER NOT NULL,
            is_current INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE(project_id, revision)
        );
        CREATE TABLE timeline_items (
            id TEXT PRIMARY KEY,
            timeline_id TEXT NOT NULL REFERENCES timelines(id) ON DELETE CASCADE,
            asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE RESTRICT,
            item_type TEXT NOT NULL CHECK(item_type IN ('clip','caption','audio','overlay')),
            track_id TEXT NOT NULL,
            timeline_start_ticks INTEGER NOT NULL,
            timeline_duration_ticks INTEGER NOT NULL,
            source_in_ticks INTEGER NOT NULL,
            source_out_ticks INTEGER NOT NULL,
            properties_json TEXT NOT NULL DEFAULT '{}',
            provenance_artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL,
            CHECK(timeline_start_ticks >= 0 AND timeline_duration_ticks > 0),
            CHECK(source_in_ticks >= 0 AND source_out_ticks > source_in_ticks)
        );
        CREATE INDEX timeline_items_order_idx ON timeline_items(timeline_id, track_id, timeline_start_ticks);
        CREATE TABLE captions (
            id TEXT PRIMARY KEY,
            timeline_item_id TEXT NOT NULL UNIQUE REFERENCES timeline_items(id) ON DELETE CASCADE,
            text TEXT NOT NULL,
            language TEXT NOT NULL DEFAULT 'en',
            style_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            state TEXT NOT NULL,
            progress REAL NOT NULL DEFAULT 0,
            resumable INTEGER NOT NULL DEFAULT 0,
            checkpoint_json TEXT NOT NULL DEFAULT '{}',
            input_fingerprint TEXT,
            error_code TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX jobs_state_idx ON jobs(project_id, state, updated_at);
        CREATE TABLE models (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            name TEXT NOT NULL,
            version TEXT NOT NULL,
            source TEXT NOT NULL,
            executable_path TEXT,
            license TEXT,
            checksum_sha256 TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE exports (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            timeline_id TEXT NOT NULL REFERENCES timelines(id) ON DELETE RESTRICT,
            status TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            format TEXT NOT NULL,
            settings_json TEXT NOT NULL DEFAULT '{}',
            started_at TEXT NOT NULL,
            completed_at TEXT,
            checksum_sha256 TEXT,
            error_code TEXT
        );
        CREATE TABLE autosaves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            revision INTEGER NOT NULL,
            state_json TEXT NOT NULL,
            state_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(project_id, revision)
        );
        CREATE TABLE migrations_applied (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );
        """
    # sqlite3.executescript() issues an implicit COMMIT and breaks transactional
    # migrations. All statements are schema DDL without embedded semicolons.
    for statement in schema.split(";"):
        if statement.strip():
            connection.execute(statement)


MIGRATIONS = {1: _migration_1}


def _migration_2(connection: sqlite3.Connection) -> None:
    for statement in (
        "ALTER TABLE jobs ADD COLUMN input_json TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE jobs ADD COLUMN output_json TEXT",
        "ALTER TABLE jobs ADD COLUMN stage TEXT NOT NULL DEFAULT 'queued'",
        "ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE jobs ADD COLUMN next_attempt_at TEXT",
        "ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN dedupe_key TEXT",
        "ALTER TABLE jobs ADD COLUMN output_artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL",
        "ALTER TABLE jobs ADD COLUMN retry_delay_seconds REAL NOT NULL DEFAULT 0.5",
        "ALTER TABLE jobs ADD COLUMN error_json TEXT",
        "ALTER TABLE jobs ADD COLUMN started_at TEXT",
        "ALTER TABLE jobs ADD COLUMN finished_at TEXT",
        "ALTER TABLE jobs ADD COLUMN lease_owner TEXT",
        "ALTER TABLE jobs ADD COLUMN lease_until TEXT",
        "ALTER TABLE jobs ADD COLUMN can_retry INTEGER NOT NULL DEFAULT 0",
        "CREATE INDEX jobs_queue_idx ON jobs(state,next_attempt_at,created_at)",
        "CREATE UNIQUE INDEX jobs_dedupe_active_idx ON jobs(dedupe_key) WHERE dedupe_key IS NOT NULL AND state IN ('queued','running','retrying')",
        "CREATE TABLE job_dependencies (job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, depends_on_job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, PRIMARY KEY(job_id,depends_on_job_id), CHECK(job_id <> depends_on_job_id))",
        "CREATE INDEX job_dependencies_parent_idx ON job_dependencies(depends_on_job_id)",
        "CREATE TABLE job_events (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, timestamp TEXT NOT NULL, level TEXT NOT NULL, event TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}')",
        "CREATE INDEX job_events_job_idx ON job_events(job_id,id)",
    ):
        connection.execute(statement)


MIGRATIONS[2] = _migration_2


def _migration_3(connection: sqlite3.Connection) -> None:
    """Keep inference immutable while allowing independent transcript review edits."""
    for statement in (
        "ALTER TABLE transcripts ADD COLUMN artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL",
        "ALTER TABLE transcripts ADD COLUMN analysis_start_ticks INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE transcript_words ADD COLUMN edited_text TEXT",
        "ALTER TABLE transcript_words ADD COLUMN timing_uncertain INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE transcript_words ADD COLUMN segment_id TEXT REFERENCES transcript_segments(id) ON DELETE SET NULL",
        "CREATE TABLE transcript_segments (id TEXT PRIMARY KEY, transcript_id TEXT NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE, ordinal INTEGER NOT NULL, source_in_ticks INTEGER NOT NULL, source_out_ticks INTEGER NOT NULL, original_text TEXT NOT NULL, edited_text TEXT, confidence REAL, speaker_ordinal INTEGER, CHECK(source_in_ticks >= 0 AND source_out_ticks > source_in_ticks), UNIQUE(transcript_id, ordinal))",
        "CREATE INDEX transcript_segments_time_idx ON transcript_segments(transcript_id,source_in_ticks)",
        "CREATE TABLE transcript_range_markers (id TEXT PRIMARY KEY, transcript_id TEXT NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE, source_in_ticks INTEGER NOT NULL, source_out_ticks INTEGER NOT NULL, marker TEXT NOT NULL CHECK(marker IN ('required','excluded','uncertain')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL, CHECK(source_in_ticks >= 0 AND source_out_ticks > source_in_ticks))",
        "CREATE INDEX transcript_range_markers_time_idx ON transcript_range_markers(transcript_id,source_in_ticks)",
    ):
        connection.execute(statement)

    for row in connection.execute("SELECT transcript_id,MIN(source_in_ticks) AS start,MAX(source_out_ticks) AS end FROM transcript_words GROUP BY transcript_id").fetchall():
        segment_id = str(uuid.uuid4())
        text = " ".join(word[0] for word in connection.execute("SELECT text FROM transcript_words WHERE transcript_id=? ORDER BY ordinal", (row["transcript_id"],)))
        connection.execute("INSERT INTO transcript_segments(id,transcript_id,ordinal,source_in_ticks,source_out_ticks,original_text) VALUES (?,?,0,?,?,?)", (segment_id,row["transcript_id"],row["start"],row["end"],text))
        connection.execute("UPDATE transcript_words SET segment_id=? WHERE transcript_id=?", (segment_id,row["transcript_id"]))


MIGRATIONS[3] = _migration_3


def _migration_4(connection: sqlite3.Connection) -> None:
    """Tie analysis suggestions to validated detector artifacts and settings."""
    connection.execute("ALTER TABLE analyses ADD COLUMN artifact_id TEXT REFERENCES artifacts(id) ON DELETE SET NULL")
    connection.execute("ALTER TABLE analyses ADD COLUMN config_json TEXT NOT NULL DEFAULT '{}'")
    connection.execute("CREATE INDEX analyses_artifact_idx ON analyses(artifact_id)")


MIGRATIONS[4] = _migration_4


def migrate_database(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version > DATABASE_SCHEMA_VERSION:
        raise UnsupportedSchemaError(f"Project database schema {version} is newer than supported schema {DATABASE_SCHEMA_VERSION}.")
    while version < DATABASE_SCHEMA_VERSION:
        target = version + 1
        migration = MIGRATIONS.get(target)
        if migration is None:
            raise UnsupportedSchemaError(f"No migration path to database schema {target}.")
        try:
            connection.execute("BEGIN IMMEDIATE")
            migration(connection)
            connection.execute("INSERT INTO migrations_applied(version, applied_at) VALUES (?, ?)", (target, _now()))
            connection.execute(f"PRAGMA user_version={target}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        version = target


def _manifest(project_id: str, name: str, created_at: str, updated_at: str) -> dict[str, Any]:
    return {
        "format": "localcut-project",
        "formatVersion": PROJECT_FORMAT_VERSION,
        "projectId": project_id,
        "name": name,
        "createdAt": created_at,
        "updatedAt": updated_at,
        "database": DATABASE_FILE,
        "timebase": {"numerator": 1, "denominator": 30},
    }


@dataclass
class ProjectStore:
    root: Path
    database_name: str = DATABASE_FILE
    recovery_report: dict[str, Any] | None = field(default=None, init=False, repr=False)

    @property
    def manifest_path(self) -> Path:
        return self.root / PROJECT_FILE

    @property
    def database_path(self) -> Path:
        return self.root / self.database_name

    @classmethod
    def create(cls, root: Path, name: str, settings: dict[str, Any] | None = None) -> "ProjectStore":
        if not name.strip():
            raise ValueError("Project name cannot be empty.")
        root = root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        if (root / PROJECT_FILE).exists() or (root / DATABASE_FILE).exists():
            raise ProjectError(f"A Local Cut project already exists at {root}.")
        for directory in ("cache/proxies", "cache/waveforms", "cache/thumbnails", "media", "recovery", "exports"):
            (root / directory).mkdir(parents=True, exist_ok=True)
        project_id = str(uuid.uuid4())
        created = _now()
        store = cls(root)
        with _connect(store.database_path) as connection:
            migrate_database(connection)
            connection.execute(
                "INSERT INTO projects(id,name,created_at,updated_at,settings_json) VALUES (?,?,?,?,?)",
                (project_id, name.strip(), created, created, _json(settings or {})),
            )
            connection.commit()
            store._write_checkpoint_locked(connection, project_id, 0, {"projectId": project_id, "settings": settings or {}, "timelines": []})
        atomic_write(store.manifest_path, (_json(_manifest(project_id, name.strip(), created, created)) + "\n").encode("utf-8"))
        store._checkpoint_if_due(0)
        return store

    @classmethod
    def open(cls, root: Path, *, recover: bool = True) -> "ProjectStore":
        root = root.expanduser().resolve()
        manifest_path = root / PROJECT_FILE
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CorruptProjectError(f"Cannot read project metadata: {exc}") from exc
        if not isinstance(manifest, dict) or manifest.get("format") != "localcut-project":
            raise CorruptProjectError("Project metadata has an invalid format.")
        format_version = manifest.get("formatVersion")
        if not isinstance(format_version, int) or format_version > PROJECT_FORMAT_VERSION:
            raise UnsupportedSchemaError(f"Project format version {format_version} is newer than supported version {PROJECT_FORMAT_VERSION}.")
        database = manifest.get("database")
        if not isinstance(database, str) or Path(database).name != database:
            raise CorruptProjectError("Project metadata database path is invalid.")
        store = cls(root)
        if database != DATABASE_FILE:
            store = cls(root, database_name=database)
        if not store.database_path.is_file():
            raise CorruptProjectError("Project database is missing.")
        if format_version == 0:
            backup = root / "recovery" / f"manifest-v0-{uuid.uuid4().hex}.json"
            backup.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(backup, manifest_path.read_bytes())
            manifest["formatVersion"] = PROJECT_FORMAT_VERSION
            atomic_write(manifest_path, (_json(manifest) + "\n").encode("utf-8"))
        try:
            connection = sqlite3.connect(store.database_path, timeout=10.0)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=10000")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                migrate_database(connection)
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise CorruptProjectError(f"Project database integrity check failed: {integrity}")
                project = connection.execute("SELECT id FROM projects WHERE id=?", (manifest.get("projectId"),)).fetchone()
                if project is None:
                    raise CorruptProjectError("Project metadata and database IDs do not match.")
                connection.execute("UPDATE projects SET last_opened_at=? WHERE id=?", (_now(), project["id"]))
                connection.commit()
            finally:
                connection.close()
        except (sqlite3.DatabaseError, CorruptProjectError) as exc:
            if not recover or not store._restore_checkpoint(str(manifest["projectId"])):
                if isinstance(exc, CorruptProjectError):
                    raise
                raise CorruptProjectError(f"Project database could not be opened: {exc}") from exc
            with _connect(store.database_path) as connection:
                migrate_database(connection)
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise CorruptProjectError("Recovered database failed integrity check.")
                project = connection.execute("SELECT id FROM projects WHERE id=?", (manifest["projectId"],)).fetchone()
                if project is None:
                    raise CorruptProjectError("Recovery checkpoint does not match this project.")
                checkpoint = connection.execute("SELECT state_revision,updated_at FROM projects WHERE id=?", (manifest["projectId"],)).fetchone()
                connection.execute("UPDATE projects SET last_opened_at=? WHERE id=?", (_now(), project["id"]))
                connection.commit()
                store.recovery_report = {
                    "checkpointRevision": int(checkpoint["state_revision"]),
                    "checkpointUpdatedAt": checkpoint["updated_at"],
                    "recoveredAt": _now(),
                    "message": "The project database was damaged. Local Cut restored the latest valid recovery checkpoint; changes after its timestamp may be missing.",
                }
        return store

    def _write_checkpoint_locked(self, connection: sqlite3.Connection, project_id: str, revision: int, state: dict[str, Any]) -> None:
        state_json = _json(state)
        digest = hashlib.sha256(state_json.encode("utf-8")).hexdigest()
        connection.execute(
            "INSERT OR REPLACE INTO autosaves(project_id,revision,state_json,state_sha256,created_at) VALUES (?,?,?,?,?)",
            (project_id, revision, state_json, digest, _now()),
        )
        connection.execute(
            "DELETE FROM autosaves WHERE project_id=? AND revision NOT IN (SELECT revision FROM autosaves WHERE project_id=? ORDER BY revision DESC LIMIT 20)",
            (project_id, project_id),
        )
        # Keep a short in-database journal; scheduled external backups protect
        # recovery when the primary database itself is damaged.

    def _record_recovery_state(self, connection: sqlite3.Connection) -> int:
        project = connection.execute("SELECT state_revision FROM projects WHERE id=?", (self.project_id,)).fetchone()
        if project is None:
            raise ProjectError("Project no longer exists.")
        revision = int(project["state_revision"]) + 1
        connection.execute("UPDATE projects SET state_revision=?,updated_at=? WHERE id=?", (revision, _now(), self.project_id))
        self._write_checkpoint_locked(connection, self.project_id, revision, self.load_state(connection))
        return revision

    def save_state(self, project_id: str, state: dict[str, Any], *, checkpoint: bool = True) -> int:
        """Atomically update editable project state and add a verified recovery point."""
        with _connect(self.database_path) as connection:
            migrate_database(connection)
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state_revision FROM projects WHERE id=?", (project_id,)).fetchone()
            if row is None:
                raise ProjectError("Project not found.")
            revision = int(row["state_revision"]) + 1
            self._validate_state(connection, project_id, state)
            self._replace_state(connection, project_id, state)
            connection.execute("UPDATE projects SET state_revision=?,updated_at=? WHERE id=?", (revision, _now(), project_id))
            if checkpoint:
                self._write_checkpoint_locked(connection, project_id, revision, state)
            connection.commit()
        if checkpoint:
            self._checkpoint_if_due(revision)
        return revision

    def autosave(self, project_id: str, state: dict[str, Any]) -> int:
        return self.save_state(project_id, state, checkpoint=True)

    def _validate_state(self, connection: sqlite3.Connection, project_id: str, state: dict[str, Any]) -> None:
        if not isinstance(state, dict):
            raise ValueError("Project state must be an object.")
        for item in state.get("timelineItems", []):
            required = {"id", "assetId", "itemType", "trackId", "timelineStartTicks", "timelineDurationTicks", "sourceInTicks", "sourceOutTicks"}
            if not isinstance(item, dict) or not required.issubset(item):
                raise ValueError("Every timeline item requires an ID, source asset, source in/out, and timeline range.")
            if item["sourceInTicks"] < 0 or item["sourceOutTicks"] <= item["sourceInTicks"]:
                raise ValueError("Timeline source range must satisfy 0 <= source in < source out.")
            if item["timelineStartTicks"] < 0 or item["timelineDurationTicks"] <= 0:
                raise ValueError("Timeline placement must have a nonnegative start and positive duration.")
            asset = connection.execute("SELECT project_id FROM assets WHERE id=?", (item["assetId"],)).fetchone()
            if asset is None or asset["project_id"] != project_id:
                raise ValueError("Timeline item references a missing or foreign source asset.")
            duration = connection.execute("SELECT duration_ticks FROM assets WHERE id=?", (item["assetId"],)).fetchone()["duration_ticks"]
            if duration is not None and item["sourceOutTicks"] > duration:
                raise ValueError("Timeline source range exceeds the source media duration.")
        for brief in state.get("briefs", []):
            if not isinstance(brief, dict) or not all(key in brief for key in ("id", "revision", "brief_text")):
                raise ValueError("Brief records require stable ID, revision, and brief_text.")
        for plan in state.get("editPlans", []):
            if not isinstance(plan, dict) or not all(key in plan for key in ("id", "revision", "status", "plan_json", "generator_id", "generator_version")):
                raise ValueError("Edit plans require stable ID, revision, status, payload, and generator provenance.")
        for job in state.get("jobs", []):
            if not isinstance(job, dict) or not all(key in job for key in ("id", "kind", "state")):
                raise ValueError("Jobs require stable ID, kind, and state.")
        for model in state.get("models", []):
            if not isinstance(model, dict) or not all(key in model for key in ("id", "role", "name", "version", "source")):
                raise ValueError("Model records require stable ID, role, name, version, and source.")

    def _replace_state(self, connection: sqlite3.Connection, project_id: str, state: dict[str, Any]) -> None:
        timeline = connection.execute("SELECT id,revision FROM timelines WHERE project_id=? AND is_current=1", (project_id,)).fetchone()
        timeline_id = timeline["id"] if timeline else str(uuid.uuid4())
        incoming_items = state.get("timelineItems")
        # The autosave journal/checkpoints preserve prior states. Keep stable
        # timeline item IDs in the current relational view and replace that
        # view atomically, rather than creating timeline versions that collide
        # with the globally unique timeline_item primary keys.
        if timeline and incoming_items is None:
            incoming_items = [{"id": row["id"], "assetId": row["asset_id"], "itemType": row["item_type"], "trackId": row["track_id"], "timelineStartTicks": row["timeline_start_ticks"], "timelineDurationTicks": row["timeline_duration_ticks"], "sourceInTicks": row["source_in_ticks"], "sourceOutTicks": row["source_out_ticks"], "properties": json.loads(row["properties_json"])} for row in connection.execute("SELECT * FROM timeline_items WHERE timeline_id=? ORDER BY track_id,timeline_start_ticks", (timeline_id,))]
        incoming_captions = state.get("captions")
        if timeline and incoming_captions is None:
            incoming_captions = [{"id": row["id"], "timelineItemId": row["timeline_item_id"], "text": row["text"], "language": row["language"], "style": json.loads(row["style_json"])} for row in connection.execute("SELECT c.* FROM captions c JOIN timeline_items i ON i.id=c.timeline_item_id WHERE i.timeline_id=?", (timeline_id,))]
        if timeline:
            connection.execute("DELETE FROM captions WHERE timeline_item_id IN (SELECT id FROM timeline_items WHERE timeline_id=?)", (timeline_id,))
            connection.execute("DELETE FROM timeline_items WHERE timeline_id=?", (timeline_id,))
        project = connection.execute("SELECT timebase_num,timebase_den FROM projects WHERE id=?", (project_id,)).fetchone()
        revision = int(timeline["revision"])+1 if timeline else 1
        if timeline:
            connection.execute("UPDATE timelines SET revision=?,created_at=? WHERE id=?", (revision, _now(), timeline_id))
        else:
            connection.execute("INSERT INTO timelines(id,project_id,name,revision,timebase_num,timebase_den,is_current,created_at) VALUES (?,?,?,?,?,?,1,?)", (timeline_id, project_id, "Main", revision, project["timebase_num"], project["timebase_den"], _now()))
        for item in incoming_items or []:
            item_id = str(item["id"])
            connection.execute(
                "INSERT INTO timeline_items(id,timeline_id,asset_id,item_type,track_id,timeline_start_ticks,timeline_duration_ticks,source_in_ticks,source_out_ticks,properties_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (item_id, timeline_id, item["assetId"], item.get("itemType", "clip"), item["trackId"], item["timelineStartTicks"], item["timelineDurationTicks"], item["sourceInTicks"], item["sourceOutTicks"], _json(item.get("properties", {}))),
            )
        self._upsert_records(connection, "briefs", project_id, state.get("briefs", []), ("revision", "brief_text", "constraints_json"))
        self._upsert_records(connection, "edit_plans", project_id, state.get("editPlans", []), ("revision", "status", "plan_json", "rationale_json"))
        self._upsert_records(connection, "jobs", project_id, state.get("jobs", []), ("kind", "state", "progress", "resumable", "checkpoint_json", "input_fingerprint", "error_code", "created_at", "updated_at"))
        self._upsert_records(connection, "models", project_id, state.get("models", []), ("role", "name", "version", "source", "executable_path", "license", "checksum_sha256", "created_at"))
        self._upsert_records(connection, "exports", project_id, state.get("exports", []), ("timeline_id", "status", "relative_path", "format", "settings_json", "started_at", "completed_at", "checksum_sha256", "error_code"))
        for record in incoming_captions or []:
            timeline_item_id = record["timelineItemId"]
            if connection.execute("SELECT 1 FROM timeline_items WHERE id=? AND timeline_id=?", (timeline_item_id, timeline_id)).fetchone() is None:
                raise ValueError("Caption must reference a current timeline item with a source range.")
            connection.execute("INSERT INTO captions(id,timeline_item_id,text,language,style_json) VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET timeline_item_id=excluded.timeline_item_id,text=excluded.text,language=excluded.language,style_json=excluded.style_json", (record.get("id", str(uuid.uuid4())), timeline_item_id, record["text"], record.get("language", "en"), _json(record.get("style", {}))))
        connection.execute("UPDATE projects SET settings_json=? WHERE id=?", (_json(state.get("settings", {})), project_id))

    def _upsert_records(self, connection: sqlite3.Connection, table: str, project_id: str, records: list[dict[str, Any]], fields: tuple[str, ...]) -> None:
        if not records:
            return
        columns = ("id", "project_id", *fields)
        placeholders = ",".join("?" for _ in columns)
        mutable = ",".join(f"{column}=excluded.{column}" for column in fields)
        sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {mutable}"
        for record in records:
            values = [record.get("id", str(uuid.uuid4())), project_id]
            for field in fields:
                value = record.get(field)
                if field.endswith("_json") and not isinstance(value, str):
                    value = _json(value if value is not None else ([] if "rationale" in field else {}))
                if field == "created_at" or field == "updated_at":
                    value = value or _now()
                if field == "resumable":
                    value = int(bool(value))
                if field == "timeline_id" and value is None:
                    timeline = connection.execute("SELECT id FROM timelines WHERE project_id=? AND is_current=1", (project_id,)).fetchone()
                    value = timeline["id"]
                values.append(value)
            existing = connection.execute(f"SELECT project_id FROM {table} WHERE id=?", (values[0],)).fetchone()
            if existing is not None and existing["project_id"] != project_id:
                raise ValueError(f"{table} record ID belongs to a different project.")
            connection.execute(sql, values)

    def add_asset(self, source_path: Path, metadata: dict[str, Any] | None = None, *, project_id: str | None = None) -> str:
        """Register a source without copying, renaming, or opening it for writing."""
        source = source_path.expanduser().resolve(strict=True)
        if not source.is_file():
            raise ValueError("Source media must be a file.")
        stat = source.stat()
        source_ref, path_mode = _portable_source_path(source, self.root)
        project_id = project_id or self.project_id
        asset_id = str(uuid.uuid4())
        now = _now()
        fingerprint = _sha256_file(source)
        metadata = {k:v for k,v in (metadata or {}).items() if not k.startswith('localcutManaged')}
        with _connect(self.database_path) as connection:
            connection.execute(
                "INSERT INTO assets(id,project_id,original_uri,path_mode,last_known_path,size_bytes,mtime_ns,sha256,metadata_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (asset_id, project_id, source_ref, path_mode, str(source), stat.st_size, stat.st_mtime_ns, fingerprint, _json(metadata or {}), now),
            )
            revision = self._record_recovery_state(connection)
            connection.commit()
        self._checkpoint_if_due(revision)
        return asset_id

    def find_duplicate_asset(self, source_path: Path, *, sha256: str | None = None, project_id: str | None = None) -> dict[str, Any] | None:
        """Match by normalized source path first, then by full content digest."""
        source = source_path.expanduser().resolve(strict=True)
        project_id = project_id or self.project_id
        normalized = os.path.normcase(str(source))
        digest = sha256 or _sha256_file(source)
        with _connect(self.database_path) as connection:
            rows = connection.execute("SELECT id,original_uri,path_mode,last_known_path,size_bytes,mtime_ns,sha256 FROM assets WHERE project_id=?", (project_id,)).fetchall()
        for row in rows:
            if os.path.normcase(str(Path(row["last_known_path"]).resolve(strict=False))) == normalized:
                return {"assetId": row["id"], "duplicateOf": row["id"], "match": "path", "pathMode": row["path_mode"], "contentChanged": row["sha256"] != digest}
        stat = source.stat()
        candidates = [row for row in rows if row["size_bytes"] == stat.st_size and row["mtime_ns"] == stat.st_mtime_ns]
        if not candidates:
            candidates = [row for row in rows if row["size_bytes"] == stat.st_size]
        if not candidates:
            return None
        for row in candidates:
            if row["sha256"] == digest:
                return {"assetId": row["id"], "duplicateOf": row["id"], "match": "content", "pathMode": row["path_mode"]}
        return None

    def import_asset_reference(self, source_path: Path, metadata: dict[str, Any], *, probe_tool: str, probe_version: str, duration_ticks: int | None = None, timebase_num: int = 1, timebase_den: int = 1_000_000, project_id: str | None = None) -> dict[str, Any]:
        """Import by reference; original media is only read and never modified."""
        source = source_path.expanduser().resolve(strict=True)
        digest = _sha256_file(source)
        duplicate = self.find_duplicate_asset(source, sha256=digest, project_id=project_id)
        if duplicate:
            if duplicate.get("contentChanged"):
                raise ProjectError("This path belongs to an imported asset but its contents changed. Use Relink source and confirm replacement so derived artifacts are invalidated.")
            return {**duplicate, "status": "duplicate"}
        asset_id = self.add_asset(source, metadata, project_id=project_id)
        self.set_media_metadata(asset_id, metadata, probe_tool=probe_tool, probe_version=probe_version, duration_ticks=duration_ticks, timebase_num=timebase_num, timebase_den=timebase_den)
        return {"assetId": asset_id, "status": "imported", "match": None}

    def _mark_managed_copy(self, asset_id: str, source: Path, digest: str) -> None:
        """Record ownership only after this app created and verified a new copy."""
        with _connect(self.database_path) as connection:
            connection.execute('BEGIN IMMEDIATE')
            row=connection.execute('SELECT original_uri,path_mode,last_known_path,metadata_json FROM assets WHERE id=? AND project_id=?',(asset_id,self.project_id)).fetchone()
            if row is None or row['path_mode']!='project-relative': raise ProjectError('Managed copy was not registered inside this project.')
            destination=(self.root/row['original_uri']).resolve()
            destination.relative_to((self.root/'media').resolve())
            if destination==source.resolve(): raise ProjectError('Managed storage requires a distinct verified copy.')
            metadata=json.loads(row['metadata_json'])
            metadata.update(localcutManagedCopy=True,localcutManagedOriginalUri=source.as_uri(),localcutManagedOriginalSha256=digest)
            connection.execute('UPDATE assets SET metadata_json=? WHERE id=?',(_json(metadata),asset_id))
            revision=self._record_recovery_state(connection); connection.commit()
        self._checkpoint_if_due(revision)

    def import_asset_managed(self, source_path: Path, metadata: dict[str, Any], *, probe_tool: str, probe_version: str, duration_ticks: int | None = None, timebase_num: int = 1, timebase_den: int = 1_000_000, project_id: str | None = None) -> dict[str, Any]:
        """Copy a source into the project atomically, then register only the verified copy."""
        source = source_path.expanduser().resolve(strict=True)
        if not source.is_file():
            raise ValueError("Source media must be a file.")
        project_id = project_id or self.project_id
        digest = _sha256_file(source)
        duplicate = self.find_duplicate_asset(source, sha256=digest, project_id=project_id)
        if duplicate:
            if duplicate.get("contentChanged"):
                raise ProjectError("This path belongs to an imported asset but its contents changed. Use Relink source and confirm replacement so derived artifacts are invalidated.")
            if duplicate.get("pathMode") == "project-relative":
                with _connect(self.database_path) as connection:
                    existing=connection.execute('SELECT metadata_json FROM assets WHERE id=?',(duplicate['assetId'],)).fetchone()
                if existing and json.loads(existing['metadata_json']).get('localcutManagedCopy') is True:
                    return {**duplicate, "status": "duplicate", "storageMode": "managed"}
            # Explicit managed import upgrades an existing linked asset in place.
            asset_id = str(duplicate["assetId"])
            media_dir = self.root / "media"; media_dir.mkdir(parents=True, exist_ok=True)
            destination = media_dir / f"{uuid.uuid4().hex}{source.suffix.lower()}"
            temporary = media_dir / f".{destination.name}.{uuid.uuid4().hex}.tmp"
            try:
                with source.open("rb") as incoming, temporary.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing, length=1024 * 1024); outgoing.flush(); os.fsync(outgoing.fileno())
                if _sha256_file(temporary) != digest or _sha256_file(source) != digest:
                    raise ProjectError("Source media changed while it was being copied. No managed copy was registered; retry the import.")
                os.replace(temporary, destination)
                try:
                    self.relink_asset(asset_id,destination,metadata=metadata,probe_tool=probe_tool,probe_version=probe_version,duration_ticks=duration_ticks,timebase_num=timebase_num,timebase_den=timebase_den)
                    self._mark_managed_copy(asset_id,source,digest)
                except Exception:
                    with _connect(self.database_path) as connection:
                        registered=connection.execute('SELECT last_known_path FROM assets WHERE id=?',(asset_id,)).fetchone()
                    if not registered or Path(registered['last_known_path']).resolve()!=destination.resolve(): destination.unlink(missing_ok=True)
                    raise
            finally:
                temporary.unlink(missing_ok=True)
            return {**duplicate,"status":"imported","storageMode":"managed","convertedExistingAsset":True}
        media_dir = self.root / "media"
        media_dir.mkdir(parents=True, exist_ok=True)
        destination = media_dir / f"{uuid.uuid4().hex}{source.suffix.lower()}"
        temporary = media_dir / f".{destination.name}.{uuid.uuid4().hex}.tmp"
        try:
            with source.open("rb") as incoming, temporary.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if _sha256_file(temporary) != digest or _sha256_file(source) != digest:
                raise ProjectError("Source media changed while it was being copied. No managed copy was registered; retry the import.")
            os.replace(temporary, destination)
            try:
                asset_id = self.add_asset(destination, metadata, project_id=project_id)
                self.set_media_metadata(asset_id, metadata, probe_tool=probe_tool, probe_version=probe_version, duration_ticks=duration_ticks, timebase_num=timebase_num, timebase_den=timebase_den)
                self._mark_managed_copy(asset_id,source,digest)
            except Exception:
                with _connect(self.database_path) as connection:
                    registered=connection.execute('SELECT 1 FROM assets WHERE last_known_path=?',(str(destination),)).fetchone()
                if not registered: destination.unlink(missing_ok=True)
                raise
            return {"assetId": asset_id, "status": "imported", "match": None, "storageMode": "managed"}
        finally:
            temporary.unlink(missing_ok=True)

    def relink_asset(self, asset_id: str, candidate_path: Path, *, metadata: dict[str, Any], probe_tool: str = "ffprobe", probe_version: str = "unknown", allow_changed: bool = False, duration_ticks: int | None = None, timebase_num: int = 1, timebase_den: int = 1_000_000) -> dict[str, Any]:
        """Relink by verified content, or explicitly replace changed media and stale its derivatives."""
        candidate = candidate_path.expanduser().resolve(strict=True)
        if not candidate.is_file():
            raise ValueError("Replacement media must be a file.")
        stat = candidate.stat()
        digest = _sha256_file(candidate)
        source_ref, path_mode = _portable_source_path(candidate, self.root)
        if duration_ticks is not None and duration_ticks <= 0:
            raise ValueError("Replacement duration must be positive.")
        state = self.load_state()
        with _connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM assets WHERE id=? AND project_id=?", (asset_id, self.project_id)).fetchone()
            if row is None:
                raise ProjectError("Imported asset was not found in this project.")
            if digest != row["sha256"] and not allow_changed:
                connection.rollback()
                return {"status": "confirmation_required", "assetId": asset_id, "oldFingerprint": row["sha256"], "newFingerprint": digest, "candidatePath": str(candidate)}
            used = connection.execute("SELECT MAX(source_out_ticks) FROM timeline_items WHERE asset_id=?", (asset_id,)).fetchone()[0]
            if duration_ticks is not None and used is not None and duration_ticks < int(used):
                raise ProjectError("Replacement media is shorter than a source range already used on the timeline. Keep the current link or edit those ranges before replacing it.")
            if digest != row["sha256"]:
                def references_asset(value: Any) -> bool:
                    if isinstance(value, dict):
                        return any((key in {"assetId", "asset_id"} and child == asset_id) or references_asset(child) for key, child in value.items())
                    if isinstance(value, list):
                        return any(references_asset(child) for child in value)
                    return False
                active_jobs = connection.execute("SELECT input_json FROM jobs WHERE project_id=? AND state IN ('queued','running','retrying')", (self.project_id,)).fetchall()
                if any(references_asset(json.loads(job["input_json"] or "{}")) for job in active_jobs):
                    raise ProjectError("Wait for or cancel this asset's active jobs before replacing its source.")
                connection.execute("UPDATE artifacts SET status='stale' WHERE asset_id=? AND status='ready'", (asset_id,))
                connection.execute("UPDATE transcripts SET status='stale' WHERE asset_id=?", (asset_id,))
                connection.execute("UPDATE analyses SET status='stale' WHERE asset_id=? AND status='ready'", (asset_id,))
                for plan in connection.execute("SELECT id,plan_json FROM edit_plans WHERE project_id=?", (self.project_id,)).fetchall():
                    if references_asset(json.loads(plan["plan_json"] or "{}")):
                        connection.execute("UPDATE edit_plans SET status='stale' WHERE id=?", (plan["id"],))
                connection.execute("UPDATE exports SET status='stale' WHERE timeline_id IN (SELECT DISTINCT timeline_id FROM timeline_items WHERE asset_id=?)", (asset_id,))
            asset_metadata={k:v for k,v in metadata.items() if not k.startswith('localcutManaged')}
            previous_metadata=json.loads(row['metadata_json'])
            previous_path=(self.root/row['original_uri'] if row['path_mode']=='project-relative' else Path(row['last_known_path'])).resolve()
            if candidate == previous_path and previous_metadata.get('localcutManagedCopy') is True:
                asset_metadata.update({k:v for k,v in previous_metadata.items() if k.startswith('localcutManaged')})
            connection.execute("UPDATE assets SET original_uri=?,path_mode=?,last_known_path=?,size_bytes=?,mtime_ns=?,sha256=?,duration_ticks=?,timebase_num=?,timebase_den=?,metadata_json=?,relink_status='available' WHERE id=? AND project_id=?", (source_ref,path_mode,str(candidate),stat.st_size,stat.st_mtime_ns,digest,duration_ticks,timebase_num,timebase_den,_json(asset_metadata),asset_id,self.project_id))
            connection.execute("INSERT OR REPLACE INTO media_metadata(asset_id,probe_tool,probe_version,metadata_json,created_at) VALUES (?,?,?,?,?)", (asset_id,probe_tool,probe_version,_json(metadata),_now()))
            project = connection.execute("SELECT state_revision FROM projects WHERE id=?", (self.project_id,)).fetchone()
            revision = int(project["state_revision"]) + 1
            connection.execute("UPDATE projects SET state_revision=?,updated_at=? WHERE id=?", (revision,_now(),self.project_id))
            state["revision"] = revision
            for asset in state.get("assets", []):
                if asset.get("id") == asset_id:
                    asset.update({"original_uri":source_ref,"path_mode":path_mode,"last_known_path":str(candidate),"size_bytes":stat.st_size,"mtime_ns":stat.st_mtime_ns,"sha256":digest,"duration_ticks":duration_ticks,"timebase_num":timebase_num,"timebase_den":timebase_den,"metadata_json":_json(asset_metadata),"relink_status":"available"})
            self._write_checkpoint_locked(connection,self.project_id,revision,state)
            connection.commit()
        self._checkpoint_if_due(revision)
        return {"status": "relinked" if digest == row["sha256"] else "replaced", "assetId": asset_id, "changedContent": digest != row["sha256"], "invalidatedArtifacts": digest != row["sha256"]}

    def set_media_metadata(self, asset_id: str, metadata: dict[str, Any], *, probe_tool: str, probe_version: str, duration_ticks: int | None = None, timebase_num: int = 1, timebase_den: int = 1_000_000) -> None:
        """Persist probe results and provenance without changing the source file."""
        with _connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE assets SET duration_ticks=?,timebase_num=?,timebase_den=? WHERE id=?",
                (duration_ticks, timebase_num, timebase_den, asset_id),
            )
            if connection.total_changes == 0:
                raise ProjectError("Asset not found.")
            connection.execute(
                "INSERT OR REPLACE INTO media_metadata(asset_id,probe_tool,probe_version,metadata_json,created_at) VALUES (?,?,?,?,?)",
                (asset_id, probe_tool, probe_version, _json(metadata), _now()),
            )
            revision = self._record_recovery_state(connection)
            connection.commit()
        self._checkpoint_if_due(revision)

    def register_artifact(self, *, asset_id: str | None, kind: str, relative_path: str, source_fingerprint: str, generator_id: str, generator_version: str, model_id: str | None = None, model_version: str | None = None, model_license: str | None = None, config: dict[str, Any] | None = None, checksum_sha256: str | None = None, status: str = "ready") -> str:
        artifact_id = str(uuid.uuid4())
        rel_path = Path(relative_path)
        if rel_path.is_absolute() or ".." in rel_path.parts:
            raise ValueError("Artifact path must stay inside the project directory.")
        with _connect(self.database_path) as connection:
            connection.execute(
                "INSERT INTO artifacts(id,project_id,asset_id,kind,relative_path,status,source_fingerprint,generator_id,generator_version,model_id,model_version,model_license,config_json,checksum_sha256,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (artifact_id, self.project_id, asset_id, kind, rel_path.as_posix(), status, source_fingerprint, generator_id, generator_version, model_id, model_version, model_license, _json(config or {}), checksum_sha256, _now()),
            )
            connection.commit()
        return artifact_id

    def job_records(self, limit: int = 100) -> list[dict[str, Any]]:
        with _connect(self.database_path) as connection:
            rows = connection.execute("SELECT * FROM jobs WHERE project_id=? ORDER BY created_at DESC LIMIT ?", (self.project_id, max(1, min(limit, 500)))).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                for field in ("input_json", "output_json", "error_json"):
                    key = field.removesuffix("_json")
                    item[key] = json.loads(item.pop(field)) if item.get(field) else None
                item["dependencies"] = [value[0] for value in connection.execute("SELECT depends_on_job_id FROM job_dependencies WHERE job_id=?", (row["id"],))]
                item.pop("checkpoint_json", None)
                item["cancel_requested"] = bool(item["cancel_requested"])
                result.append(item)
            return result

    def add_job_event(self, connection: sqlite3.Connection, job_id: str, level: str, event: str, details: dict[str, Any] | None = None) -> None:
        connection.execute("INSERT INTO job_events(job_id,timestamp,level,event,details_json) VALUES (?,?,?,?,?)", (job_id, _now(), level, event, _json(details or {})))

    def recent_job_events(self, job_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with _connect(self.database_path) as connection:
            rows = connection.execute("SELECT timestamp,level,event,details_json FROM job_events WHERE job_id=? ORDER BY id DESC LIMIT ?", (job_id, max(1, min(limit, 500)))).fetchall()
            return [{"timestamp": row["timestamp"], "level": row["level"], "event": row["event"], "details": json.loads(row["details_json"])} for row in reversed(rows)]

    def find_valid_artifact(self, dedupe_key: str) -> dict[str, Any] | None:
        with _connect(self.database_path) as connection:
            rows = connection.execute("SELECT a.* FROM jobs j JOIN artifacts a ON a.id=j.output_artifact_id WHERE j.project_id=? AND j.dedupe_key=? AND j.state='completed' AND a.status='ready' ORDER BY j.finished_at DESC", (self.project_id, dedupe_key)).fetchall()
        for row in rows:
            path = (self.root / row["relative_path"]).resolve(strict=False)
            try:
                path.relative_to(self.root.resolve())
                if path.exists() and row["checksum_sha256"] and _sha256_file(path) == row["checksum_sha256"]:
                    return dict(row)
            except (OSError, ValueError):
                pass
            with _connect(self.database_path) as connection:
                connection.execute("UPDATE artifacts SET status='stale' WHERE id=?", (row["id"],))
                connection.commit()
        return None

    def add_transcript(self, *, asset_id: str, language: str, words: list[dict[str, Any]], generator_id: str, generator_version: str, source_fingerprint: str, model_id: str | None = None, model_version: str | None = None, segments: list[dict[str, Any]] | None = None, artifact_id: str | None = None, analysis_start_ticks: int = 0, connection=None) -> str:
        if analysis_start_ticks < 0:
            raise ValueError("Analysis audio start must be a nonnegative source tick.")
        transcript_id = str(uuid.uuid4())
        from contextlib import nullcontext
        owns_connection = connection is None
        with (_connect(self.database_path) if owns_connection else nullcontext(connection)) as connection:
            if owns_connection:
                connection.execute("BEGIN IMMEDIATE")
            asset = connection.execute("SELECT sha256 FROM assets WHERE id=? AND project_id=?", (asset_id, self.project_id)).fetchone()
            if asset is None or asset["sha256"] != source_fingerprint:
                raise ValueError("Transcript source fingerprint does not match a project asset.")
            if artifact_id is not None:
                artifact = connection.execute("SELECT id FROM artifacts WHERE id=? AND project_id=? AND asset_id=? AND status='ready' AND source_fingerprint=?", (artifact_id, self.project_id, asset_id, source_fingerprint)).fetchone()
                if artifact is None:
                    raise ValueError("Transcript artifact must be a ready artifact for this source asset.")
            connection.execute(
                "INSERT INTO transcripts(id,project_id,asset_id,language,status,generator_id,generator_version,model_id,model_version,source_fingerprint,artifact_id,analysis_start_ticks,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (transcript_id, self.project_id, asset_id, language, "ready", generator_id, generator_version, model_id, model_version, source_fingerprint, artifact_id, analysis_start_ticks, _now()),
            )
            segment_ranges: list[tuple[str, int, int]] = []
            for ordinal, segment in enumerate(segments or []):
                start, end = self._valid_transcript_range(segment)
                segment_id = str(segment.get("id") or uuid.uuid4())
                connection.execute(
                    "INSERT INTO transcript_segments(id,transcript_id,ordinal,source_in_ticks,source_out_ticks,original_text,confidence,speaker_ordinal) VALUES (?,?,?,?,?,?,?,?)",
                    (segment_id, transcript_id, ordinal, start, end, str(segment.get("text", "")), segment.get("confidence"), segment.get("speakerOrdinal")),
                )
                segment_ranges.append((segment_id, start, end))
            for ordinal, word in enumerate(words):
                start, end = self._valid_transcript_range(word)
                segment_id = word.get("segmentId")
                if segment_id is None:
                    segment_id = next((sid for sid, sin, sout in segment_ranges if sin <= start and end <= sout), None)
                if segment_id is not None and not any(sid == segment_id and sin <= start and end <= sout for sid, sin, sout in segment_ranges):
                    raise ValueError("Transcript word must fit its own segment.")
                connection.execute(
                    "INSERT INTO transcript_words(id,transcript_id,ordinal,source_in_ticks,source_out_ticks,text,confidence,speaker_ordinal,is_user_corrected,is_protected,segment_id,timing_uncertain) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(word.get("id", uuid.uuid4())), transcript_id, ordinal, start, end, str(word["text"]), word.get("confidence"), word.get("speakerOrdinal"), 0, int(bool(word.get("protected"))), segment_id, int(bool(word.get("timingUncertain")))),
                )
            if owns_connection:
                connection.commit()
        return transcript_id

    @staticmethod
    def _valid_transcript_range(record: dict[str, Any]) -> tuple[int, int]:
        start, end = record.get("sourceInTicks"), record.get("sourceOutTicks")
        if type(start) is not int or type(end) is not int or start < 0 or end <= start:
            raise ValueError("Transcript source range must contain increasing integer ticks.")
        return start, end

    def get_transcript(self, transcript_id: str) -> dict[str, Any]:
        with _connect(self.database_path) as connection:
            row = connection.execute("SELECT * FROM transcripts WHERE id=? AND project_id=?", (transcript_id, self.project_id)).fetchone()
            if row is None:
                raise ProjectError("Transcript is unavailable in this project.")
            return self._transcript_view(connection, row)

    def list_transcripts(self, asset_id: str | None = None) -> list[dict[str, Any]]:
        with _connect(self.database_path) as connection:
            if asset_id is None:
                rows = connection.execute("SELECT * FROM transcripts WHERE project_id=? ORDER BY created_at,id", (self.project_id,)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM transcripts WHERE project_id=? AND asset_id=? ORDER BY created_at,id", (self.project_id, asset_id)).fetchall()
            return [self._transcript_view(connection, row) for row in rows]

    @staticmethod
    def _transcript_view(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        transcript_id = row["id"]
        segments = [
            {"id": segment["id"], "ordinal": segment["ordinal"], "sourceInTicks": segment["source_in_ticks"], "sourceOutTicks": segment["source_out_ticks"], "originalText": segment["original_text"], "editedText": segment["edited_text"], "text": segment["edited_text"] if segment["edited_text"] is not None else segment["original_text"], "confidence": segment["confidence"], "speakerOrdinal": segment["speaker_ordinal"]}
            for segment in connection.execute("SELECT * FROM transcript_segments WHERE transcript_id=? ORDER BY ordinal", (transcript_id,))
        ]
        words = [
            {"id": word["id"], "segmentId": word["segment_id"], "ordinal": word["ordinal"], "sourceInTicks": word["source_in_ticks"], "sourceOutTicks": word["source_out_ticks"], "originalText": word["text"], "editedText": word["edited_text"], "text": word["edited_text"] if word["edited_text"] is not None else word["text"], "confidence": word["confidence"], "speakerOrdinal": word["speaker_ordinal"], "isUserCorrected": bool(word["is_user_corrected"]), "isProtected": bool(word["is_protected"]), "timingUncertain": bool(word["timing_uncertain"])}
            for word in connection.execute("SELECT * FROM transcript_words WHERE transcript_id=? ORDER BY ordinal", (transcript_id,))
        ]
        markers = [
            {"id": marker["id"], "sourceInTicks": marker["source_in_ticks"], "sourceOutTicks": marker["source_out_ticks"], "marker": marker["marker"]}
            for marker in connection.execute("SELECT * FROM transcript_range_markers WHERE transcript_id=? ORDER BY source_in_ticks,id", (transcript_id,))
        ]
        return {"id": transcript_id, "assetId": row["asset_id"], "language": row["language"], "status": row["status"], "generatorId": row["generator_id"], "generatorVersion": row["generator_version"], "modelId": row["model_id"], "modelVersion": row["model_version"], "sourceFingerprint": row["source_fingerprint"], "artifactId": row["artifact_id"], "analysisStartTicks": row["analysis_start_ticks"], "segments": segments, "words": words, "markers": markers}

    def update_transcript_segment(self, transcript_id: str, segment_id: str, edited_text: str | None) -> dict[str, Any]:
        if edited_text is not None and (not isinstance(edited_text, str) or len(edited_text) > 100_000):
            raise ValueError("Edited transcript text must be at most 100,000 characters.")
        with _connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute("UPDATE transcript_segments SET edited_text=? WHERE id=? AND transcript_id=? AND EXISTS(SELECT 1 FROM transcripts WHERE id=? AND project_id=?)", (edited_text, segment_id, transcript_id, transcript_id, self.project_id))
            if result.rowcount != 1:
                raise ProjectError("Transcript segment is unavailable in this project.")
            revision = self._record_recovery_state(connection)
            connection.commit()
        self._checkpoint_if_due(revision)
        return self.get_transcript(transcript_id)

    def update_transcript_word(self, transcript_id: str, word_id: str, edited_text: str | None) -> dict[str, Any]:
        if edited_text is not None and (not isinstance(edited_text, str) or len(edited_text) > 1000):
            raise ValueError("Edited word must be at most 1,000 characters.")
        with _connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute("UPDATE transcript_words SET edited_text=?,is_user_corrected=? WHERE id=? AND transcript_id=? AND EXISTS(SELECT 1 FROM transcripts WHERE id=? AND project_id=?)", (edited_text, int(edited_text is not None), word_id, transcript_id, transcript_id, self.project_id))
            if result.rowcount != 1:
                raise ProjectError("Transcript word is unavailable in this project.")
            revision = self._record_recovery_state(connection)
            connection.commit()
        self._checkpoint_if_due(revision)
        return self.get_transcript(transcript_id)

    def set_transcript_range_marker(self, transcript_id: str, source_in_ticks: int, source_out_ticks: int, marker: str, marker_id: str | None = None) -> dict[str, Any]:
        start, end = self._valid_transcript_range({"sourceInTicks": source_in_ticks, "sourceOutTicks": source_out_ticks})
        if marker not in {"required", "excluded", "uncertain"}:
            raise ValueError("Transcript marker must be required, excluded, or uncertain.")
        with _connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            transcript = connection.execute("SELECT asset_id FROM transcripts WHERE id=? AND project_id=?", (transcript_id, self.project_id)).fetchone()
            if transcript is None:
                raise ProjectError("Transcript is unavailable in this project.")
            duration = connection.execute("SELECT duration_ticks,timebase_num,timebase_den FROM assets WHERE id=?", (transcript["asset_id"],)).fetchone()
            if duration and duration["duration_ticks"] is not None:
                duration_us = duration["duration_ticks"]
                origin = connection.execute("SELECT analysis_start_ticks FROM transcripts WHERE id=?", (transcript_id,)).fetchone()[0]
                duration_us += origin
                if end > duration_us + 1_000_000:
                    raise ValueError("Transcript marker extends past source duration.")
            timestamp = _now()
            if marker_id is None:
                marker_id = str(uuid.uuid4())
                connection.execute("INSERT INTO transcript_range_markers(id,transcript_id,source_in_ticks,source_out_ticks,marker,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", (marker_id, transcript_id, start, end, marker, timestamp, timestamp))
            else:
                result = connection.execute("UPDATE transcript_range_markers SET source_in_ticks=?,source_out_ticks=?,marker=?,updated_at=? WHERE id=? AND transcript_id=?", (start, end, marker, timestamp, marker_id, transcript_id))
                if result.rowcount != 1:
                    raise ProjectError("Transcript marker is unavailable in this transcript.")
            revision = self._record_recovery_state(connection)
            connection.commit()
        self._checkpoint_if_due(revision)
        return self.get_transcript(transcript_id)

    def delete_transcript_range_marker(self, transcript_id: str, marker_id: str) -> dict[str, Any]:
        with _connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute("DELETE FROM transcript_range_markers WHERE id=? AND transcript_id=? AND EXISTS(SELECT 1 FROM transcripts WHERE id=? AND project_id=?)", (marker_id, transcript_id, transcript_id, self.project_id))
            if result.rowcount != 1:
                raise ProjectError("Transcript marker is unavailable in this transcript.")
            revision = self._record_recovery_state(connection)
            connection.commit()
        self._checkpoint_if_due(revision)
        return self.get_transcript(transcript_id)

    def add_analysis(self, *, asset_id: str, kind: str, source_in_ticks: int, source_out_ticks: int, result: dict[str, Any], generator_id: str, generator_version: str, source_fingerprint: str, model_id: str | None = None, model_version: str | None = None, confidence: float | None = None, artifact_id: str | None = None, config: dict[str, Any] | None = None, connection=None) -> str:
        if source_in_ticks < 0 or source_out_ticks <= source_in_ticks:
            raise ValueError("Analysis source range is invalid.")
        from contextlib import nullcontext
        owns_connection = connection is None
        with (_connect(self.database_path) if owns_connection else nullcontext(connection)) as connection:
            if owns_connection:
                connection.execute("BEGIN IMMEDIATE")
            asset = connection.execute("SELECT sha256 FROM assets WHERE id=? AND project_id=?", (asset_id, self.project_id)).fetchone()
            if asset is None or asset["sha256"] != source_fingerprint:
                raise ValueError("Analysis source fingerprint does not match a project asset.")
            analysis_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO analyses(id,project_id,asset_id,kind,source_in_ticks,source_out_ticks,result_json,confidence,generator_id,generator_version,model_id,model_version,source_fingerprint,status,created_at,artifact_id,config_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'ready',?,?,?)",
                (analysis_id, self.project_id, asset_id, kind, source_in_ticks, source_out_ticks, _json(result), confidence, generator_id, generator_version, model_id, model_version, source_fingerprint, _now(), artifact_id, _json(config or {})),
            )
            if owns_connection:
                connection.commit()
        return analysis_id

    def list_analyses(self, asset_id: str | None = None, detector_id: str | None = None) -> list[dict[str, Any]]:
        with _connect(self.database_path) as connection:
            sql = "SELECT n.*,a.status AS _artifact_status,a.relative_path AS _artifact_path,a.checksum_sha256 AS _artifact_checksum,a.config_json AS _artifact_config FROM analyses n LEFT JOIN artifacts a ON a.id=n.artifact_id WHERE n.project_id=? AND n.status='ready'"
            values: list[Any] = [self.project_id]
            if asset_id is not None:
                sql += " AND n.asset_id=?"; values.append(asset_id)
            if detector_id is not None:
                sql += " AND n.kind=?"; values.append(detector_id)
            rows = connection.execute(sql + " ORDER BY n.created_at,n.source_in_ticks,n.id", values).fetchall()
            return [{"id": row["id"], "assetId": row["asset_id"], "detectorId": row["kind"], "sourceInTicks": row["source_in_ticks"], "sourceOutTicks": row["source_out_ticks"], "confidence": row["confidence"], "detectorName": row["generator_id"], "detectorVersion": row["generator_version"], "modelId": row["model_id"], "modelVersion": row["model_version"], "sourceFingerprint": row["source_fingerprint"], "artifactId": row["artifact_id"], "config": json.loads(row["config_json"]), "createdAt": row["created_at"], "result": json.loads(row["result_json"]), "_artifactStatus": row["_artifact_status"], "_artifactPath": row["_artifact_path"], "_artifactChecksum": row["_artifact_checksum"], "_artifactConfig": json.loads(row["_artifact_config"] or "{}") if row["_artifact_config"] else {}} for row in rows]

    def add_brief(self, brief_text: str, *, constraints: dict[str, Any] | None = None, traits: dict[str, Any] | None = None, trait_origin: str = "user", generator_id: str = "user", generator_version: str = "1", model_id: str | None = None, model_version: str | None = None, connection=None) -> str:
        brief_id = str(uuid.uuid4())
        from contextlib import nullcontext
        owns_connection = connection is None
        recovery_revision = None
        with (_connect(self.database_path) if owns_connection else nullcontext(connection)) as connection:
            if owns_connection: connection.execute("BEGIN IMMEDIATE")
            revision = int(connection.execute("SELECT COALESCE(MAX(revision),0)+1 FROM briefs WHERE project_id=?", (self.project_id,)).fetchone()[0])
            connection.execute("INSERT INTO briefs(id,project_id,revision,brief_text,constraints_json,created_at) VALUES (?,?,?,?,?,?)", (brief_id, self.project_id, revision, brief_text, _json(constraints or {}), _now()))
            for key, value in (traits or {}).items():
                connection.execute("INSERT INTO style_traits(id,brief_id,trait_key,trait_value_json,origin,generator_id,generator_version,model_id,model_version,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), brief_id, key, _json(value), trait_origin, generator_id, generator_version, model_id, model_version, _now()))
            if owns_connection:
                recovery_revision = self._record_recovery_state(connection)
                connection.commit()
        if recovery_revision is not None:
            self._checkpoint_if_due(recovery_revision)
        return brief_id

    def list_briefs(self) -> list[dict[str, Any]]:
        with _connect(self.database_path) as connection:
            rows = connection.execute("SELECT * FROM briefs WHERE project_id=? ORDER BY revision DESC", (self.project_id,)).fetchall()
            result=[]
            for row in rows:
                traits={item["trait_key"]: {"value": json.loads(item["trait_value_json"]), "origin": item["origin"], "generatorId": item["generator_id"], "generatorVersion": item["generator_version"]} for item in connection.execute("SELECT * FROM style_traits WHERE brief_id=?", (row["id"],))}
                result.append({"id":row["id"],"revision":row["revision"],"briefText":row["brief_text"],"constraints":json.loads(row["constraints_json"]),"traits":traits,"createdAt":row["created_at"]})
            return result

    def add_edit_plan(self, plan: dict[str, Any], *, status: str, rationale: list[dict[str, Any]], generator_id: str, generator_version: str, model_id: str | None = None, model_version: str | None = None, brief_id: str | None = None, connection=None) -> str:
        plan_id = str(uuid.uuid4())
        from contextlib import nullcontext
        owns_connection = connection is None
        with (_connect(self.database_path) if owns_connection else nullcontext(connection)) as connection:
            if owns_connection: connection.execute("BEGIN IMMEDIATE")
            revision = int(connection.execute("SELECT COALESCE(MAX(revision),0)+1 FROM edit_plans WHERE project_id=?", (self.project_id,)).fetchone()[0])
            connection.execute("INSERT INTO edit_plans(id,project_id,brief_id,revision,status,plan_json,rationale_json,generator_id,generator_version,model_id,model_version,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (plan_id, self.project_id, brief_id, revision, status, _json(plan), _json(rationale), generator_id, generator_version, model_id, model_version, _now()))
            if owns_connection: connection.commit()
        return plan_id

    def list_edit_plans(self, brief_id: str | None = None) -> list[dict[str, Any]]:
        with _connect(self.database_path) as connection:
            sql="SELECT * FROM edit_plans WHERE project_id=?"; values=[self.project_id]
            if brief_id is not None: sql += " AND brief_id=?"; values.append(brief_id)
            rows=connection.execute(sql+" ORDER BY revision DESC",values).fetchall()
            return [{"id":row["id"],"briefId":row["brief_id"],"revision":row["revision"],"status":row["status"],"plan":json.loads(row["plan_json"]),"rationale":json.loads(row["rationale_json"]),"generatorId":row["generator_id"],"generatorVersion":row["generator_version"],"modelId":row["model_id"],"modelVersion":row["model_version"],"createdAt":row["created_at"]} for row in rows]

    def decide_edit_plan_suggestion(self, plan_id: str, suggestion_id: str, decision: str, *, expected_revision: int | None = None, create_captions: bool = False) -> dict[str, Any]:
        if decision not in {"accepted", "rejected", "pending"}: raise ValueError("Suggestion decision must be accepted, rejected, or pending.")
        if decision == "accepted" and expected_revision is None: raise ValueError("Accepting a proposal requires the project revision that was reviewed.")
        if not isinstance(create_captions, bool): raise ValueError("Caption creation choice must be a boolean.")
        with _connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row=connection.execute("SELECT * FROM edit_plans WHERE id=? AND project_id=?",(plan_id,self.project_id)).fetchone()
            if row is None: raise ProjectError("Edit proposal was not found.")
            plan=json.loads(row["plan_json"])
            if plan.get("planType") in {"written-rough-cut", "timeline-revision"}:
                raise ProjectError("Written plans cannot execute timeline operations. Use written-plan review.")
            operations=plan.get("operations",[])
            op=next((item for item in operations if item.get("id")==suggestion_id),None)
            if op is None: raise ProjectError("Proposal operation was not found.")
            old_status=op.get("status","pending")
            if old_status=="accepted" and decision=="rejected": raise ProjectError("Undo the applied suggestion before rejecting it.")
            if old_status=="accepted" and decision=="pending":
                accepted_reorder=next((item for item in operations if item.get("type")=="reorder_passages" and item.get("status")=="accepted" and suggestion_id in item.get("requiresSuggestionIds",[])),None)
                if accepted_reorder is not None: raise ProjectError("Undo the accepted passage reordering before undoing or rejecting one of its kept passages.")
            if old_status==decision:
                connection.rollback(); return {"planId":plan_id,"suggestionId":suggestion_id,"status":decision,"revision":None,"deduplicated":True}
            revision=None
            if decision=="accepted":
                project=connection.execute("SELECT state_revision FROM projects WHERE id=?",(self.project_id,)).fetchone()
                if expected_revision is not None and int(project["state_revision"])!=expected_revision:
                    raise ProjectError("The timeline changed since this proposal was loaded. Refresh the project and retry.")
                if op.get("type")=="reorder_passages":
                    required=op.get("requiresSuggestionIds",[])
                    if not isinstance(required,list) or any(not any(candidate.get("id")==required_id and candidate.get("type")=="include_source_range" and candidate.get("status")=="accepted" for candidate in operations) for required_id in required):
                        raise ProjectError("Accept every kept passage listed by this reorder before applying it.")
                state=self.load_state(connection)
                before={"timelineItems":state["timelineItems"],"captions":state.get("captions",[])}
                state["timelineItems"],state["captions"]=self._apply_edit_suggestion(state["timelineItems"],state.get("captions",[]),op)
                if create_captions and op.get("type")=="include_source_range":
                    clip=next((item for item in state["timelineItems"] if item.get("properties",{}).get("originSuggestionId")==suggestion_id and item.get("itemType")=="clip"),None)
                    if clip is not None:
                        transcript_row=connection.execute("SELECT * FROM transcripts WHERE id=? AND project_id=? AND asset_id=?",(op.get("transcriptId"),self.project_id,op.get("assetId"))).fetchone()
                        if transcript_row is None: raise ProjectError("The reviewed transcript for this caption proposal is unavailable.")
                        transcript=self._transcript_view(connection,transcript_row)
                        new_items,new_records=self._captions_for_clip(clip,op,transcript)
                        state["timelineItems"].extend(new_items); state["captions"].extend(new_records)
                self._validate_state(connection,self.project_id,state)
                self._replace_state(connection,self.project_id,state)
                revision=int(project["state_revision"])+1
                connection.execute("UPDATE projects SET state_revision=?,updated_at=? WHERE id=?",(revision,_now(),self.project_id))
                self._write_checkpoint_locked(connection,self.project_id,revision,state)
                op["application"]={"before":before,"revision":revision}
                op["appliedTimelineItemIds"]=self._operation_item_ids(state["timelineItems"],suggestion_id)
            elif old_status=="accepted" and decision=="pending":
                project=connection.execute("SELECT state_revision FROM projects WHERE id=?",(self.project_id,)).fetchone()
                application=op.get("application") or {}; applied_revision=application.get("revision")
                if applied_revision is None or int(project["state_revision"])!=int(applied_revision):
                    raise ProjectError("Undo is available only before later timeline edits. Use the timeline history after making another change.")
                state=self.load_state(connection); before=application.get("before",{})
                state["timelineItems"]=before.get("timelineItems",[]); state["captions"]=before.get("captions",[])
                self._validate_state(connection,self.project_id,state); self._replace_state(connection,self.project_id,state)
                revision=int(project["state_revision"])+1
                connection.execute("UPDATE projects SET state_revision=?,updated_at=? WHERE id=?",(revision,_now(),self.project_id))
                self._write_checkpoint_locked(connection,self.project_id,revision,state)
                op.pop("application",None); op.pop("appliedTimelineItemIds",None)
            op["status"]=decision; op["decisionUpdatedAt"]=_now()
            statuses=[item.get("status","pending") for item in operations]
            overall="pending" if "pending" in statuses else "reviewed"
            connection.execute("UPDATE edit_plans SET plan_json=?,status=? WHERE id=?",(_json(plan),overall,plan_id))
            connection.commit()
        if revision is not None:
            self._checkpoint_if_due(revision)
        return {"planId":plan_id,"suggestionId":suggestion_id,"status":decision,"revision":revision,"deduplicated":False}

    @staticmethod
    def _operation_item_ids(items: list[dict[str, Any]], suggestion_id: str) -> list[str]:
        return [item["id"] for item in items if item.get("properties",{}).get("originSuggestionId")==suggestion_id or item.get("properties",{}).get("orderSuggestionId")==suggestion_id or suggestion_id in item.get("properties",{}).get("punchInSuggestionIds",[])]

    @staticmethod
    def _captions_for_clip(clip: dict[str, Any], operation: dict[str, Any], transcript: dict[str, Any]) -> tuple[list[dict[str, Any]],list[dict[str, Any]]]:
        """Create editable caption cues from reviewed segment text on the source-time axis."""
        import textwrap
        start=int(operation["sourceInTicks"]); end=int(operation["sourceOutTicks"])
        source_start=int(clip["sourceInTicks"]); timeline_start=int(clip["timelineStartTicks"])
        ratio=int(clip["timelineDurationTicks"])/(int(clip["sourceOutTicks"])-source_start)
        segments=[segment for segment in transcript.get("segments",[]) if int(segment["sourceInTicks"])<end and int(segment["sourceOutTicks"])>start]
        items=[]; records=[]
        for segment in segments:
            source_in=max(start,int(segment["sourceInTicks"])); source_out=min(end,int(segment["sourceOutTicks"]))
            if source_out<=source_in: continue
            # If the operation only includes part of a segment, use its intersecting
            # word text rather than showing corrected text from outside the kept range.
            if source_in!=int(segment["sourceInTicks"]) or source_out!=int(segment["sourceOutTicks"]):
                parts=[str(word["text"]).strip() for word in transcript.get("words",[]) if word.get("segmentId")==segment["id"] and int(word["sourceInTicks"])<source_out and int(word["sourceOutTicks"])>source_in]
                text=" ".join(part for part in parts if part)
            else:
                text=str(segment.get("editedText") if segment.get("editedText") is not None else segment.get("originalText", "")).strip()
            if not text: continue
            lines=textwrap.wrap(text,width=42,break_long_words=False,break_on_hyphens=False) or [text]
            display_text="\n".join(lines)
            cue_id=str(uuid.uuid4())
            item={"id":cue_id,"assetId":clip["assetId"],"itemType":"caption","trackId":"captions","timelineStartTicks":timeline_start+round((source_in-source_start)*ratio),"timelineDurationTicks":max(1,round((source_out-source_in)*ratio)),"sourceInTicks":source_in,"sourceOutTicks":source_out,"properties":{"text":display_text,"linkedClipId":clip["id"],"originSuggestionId":operation["id"],"generatedBy":"localcut-transcript-captioner","generatorVersion":"1","transcriptId":transcript["id"]}}
            record={"id":str(uuid.uuid4()),"timelineItemId":cue_id,"text":display_text,"language":transcript.get("language") or "und","style":{"generatedBy":"localcut-transcript-captioner","generatorVersion":"1","transcriptId":transcript["id"]}}
            items.append(item); records.append(record)
        return items,records

    def _apply_edit_suggestion(self, items: list[dict[str, Any]], captions: list[dict[str, Any]], operation: dict[str, Any]) -> tuple[list[dict[str, Any]],list[dict[str, Any]]]:
        kind=operation.get("type"); asset_id=operation.get("assetId"); start=int(operation.get("sourceInTicks",-1)); end=int(operation.get("sourceOutTicks",-1))
        if start<0 or end<=start: raise ValueError("Proposal contains an invalid source range.")
        if kind=="reorder_passages":
            ordered=operation.get("orderedSuggestionIds")
            if not isinstance(ordered,list) or len(ordered)<2 or any(not isinstance(value,str) for value in ordered) or len(set(ordered))!=len(ordered):
                raise ProjectError("Passage order is invalid; regenerate the proposal.")
            target_by_suggestion={}
            for item in items:
                props=item.get("properties",{})
                origin=props.get("originSuggestionId") if isinstance(props,dict) else None
                if item.get("itemType")=="clip" and origin in ordered:
                    if origin in target_by_suggestion: raise ProjectError("A kept passage has multiple timeline clips; resolve the duplicate before reordering.")
                    target_by_suggestion[origin]=item
            if set(target_by_suggestion)!=set(ordered) or any(target_by_suggestion[key].get("assetId")!=asset_id for key in ordered):
                raise ProjectError("Accept every kept passage onto this timeline before reordering.")
            target_ids={item["id"] for item in target_by_suggestion.values()}
            sorted_targets=sorted(target_by_suggestion.values(),key=lambda item:int(item["timelineStartTicks"]))
            first_start=int(sorted_targets[0]["timelineStartTicks"])
            if any(item.get("itemType")=="clip" and item.get("id") not in target_ids and int(item["timelineStartTicks"])+int(item["timelineDurationTicks"])>first_start for item in items):
                raise ProjectError("The kept passages are no longer an uninterrupted block at the end of the video track. Move them to the end before applying this order.")
            for left,right in zip(sorted_targets,sorted_targets[1:]):
                if int(left["timelineStartTicks"])+int(left["timelineDurationTicks"])!=int(right["timelineStartTicks"]):
                    raise ProjectError("The kept passages are no longer contiguous. Restore their block before applying this order.")
            placements={}; cursor=first_start
            for suggestion_id in ordered:
                clip=target_by_suggestion[suggestion_id]
                placements[clip["id"]]=cursor
                cursor+=int(clip["timelineDurationTicks"])
            old_starts={clip["id"]:int(clip["timelineStartTicks"]) for clip in target_by_suggestion.values()}
            deltas={item_id:new_start-old_starts[item_id] for item_id,new_start in placements.items()}
            updated=[]
            for item in items:
                item_id=item.get("id")
                if item_id in placements:
                    properties=dict(item.get("properties",{})); properties["orderSuggestionId"]=operation["id"]
                    updated.append({**item,"timelineStartTicks":placements[item_id],"properties":properties})
                    continue
                properties=item.get("properties",{})
                linked=properties.get("linkedClipId") if isinstance(properties,dict) else None
                if linked in deltas:
                    updated.append({**item,"timelineStartTicks":int(item["timelineStartTicks"])+deltas[linked]})
                else:
                    updated.append(item)
            return updated,captions
        if kind=="punch_in":
            if operation.get("cropPreset")!="punch_in_110": raise ValueError("Only the supported 110% punch-in preset can be suggested.")
            related_id=operation.get("relatedIncludeSuggestionId")
            if not isinstance(related_id,str) or not related_id: raise ValueError("Punch-in suggestion is missing its kept-passage relationship.")
            updated=[]; matched=False
            for item in items:
                overlaps=item.get("itemType")=="clip" and item.get("assetId")==asset_id and item.get("properties",{}).get("originSuggestionId")==related_id
                if not overlaps:
                    updated.append(item); continue
                matched=True; properties=dict(item.get("properties",{})); ids=list(properties.get("punchInSuggestionIds",[]))
                if operation["id"] not in ids: ids.append(operation["id"])
                properties.update({"cropPreset":operation["cropPreset"],"punchInSuggestionIds":ids})
                updated.append({**item,"properties":properties})
            if not matched: raise ProjectError("Accept the related kept passage onto the timeline before applying its punch-in suggestion.")
            return updated,captions
        if kind=="include_source_range":
            if any(item.get("properties",{}).get("originSuggestionId")==operation["id"] for item in items): return items,captions
            source_duration=end-start; timeline_start=max((item["timelineStartTicks"]+item["timelineDurationTicks"] for item in items if item["itemType"]=="clip"),default=0)
            item={"id":str(uuid.uuid4()),"assetId":asset_id,"itemType":"clip","trackId":"video","timelineStartTicks":timeline_start,"timelineDurationTicks":source_duration,"sourceInTicks":start,"sourceOutTicks":end,"properties":{"linkedAudio":True,"audioGainDb":0,"fadeInTicks":0,"fadeOutTicks":0,"cropPreset":"full_frame","originSuggestionId":operation["id"],"proposalRationale":operation.get("rationale"),"proposalConfidence":operation.get("confidence")}}
            return [*items,item],captions
        if kind!="exclude_source_range": raise ValueError("Proposal operation type is unsupported.")
        new_items=[]; caption_map={};
        for item in items:
            if item.get("itemType")=="caption": continue
            overlaps=item.get("itemType")=="clip" and item.get("assetId")==asset_id and item["sourceInTicks"]<end and item["sourceOutTicks"]>start
            if not overlaps: new_items.append(item); continue
            source_in=item["sourceInTicks"]; source_out=item["sourceOutTicks"]; ratio=item["timelineDurationTicks"]/(source_out-source_in)
            kept=[]
            if source_in<start: kept.append((source_in,min(start,source_out)))
            if source_out>end: kept.append((max(end,source_in),source_out))
            pieces=[]
            for index,(a,b) in enumerate(kept):
                piece={**item,"id":item["id"] if index==0 else str(uuid.uuid4()),"sourceInTicks":a,"sourceOutTicks":b,"timelineStartTicks":item["timelineStartTicks"]+round((a-source_in)*ratio),"timelineDurationTicks":max(1,round((b-a)*ratio))}
                new_items.append(piece); pieces.append(piece)
            caption_map[item["id"]]=pieces
        new_captions=[]
        caption_items={item["id"]:item for item in items if item.get("itemType")=="caption"}
        caption_records: dict[str,list[dict[str,Any]]]={}
        for caption in captions: caption_records.setdefault(caption["timelineItemId"],[]).append(caption)
        for caption_item in caption_items.values():
            parent_id=caption_item.get("properties",{}).get("linkedClipId")
            pieces=caption_map.get(parent_id) if parent_id else None
            records=caption_records.get(caption_item["id"],[])
            if pieces is None:
                new_items.append(caption_item); new_captions.extend(records); continue
            for index,piece in enumerate(pieces):
                start=max(caption_item["sourceInTicks"],piece["sourceInTicks"]); finish=min(caption_item["sourceOutTicks"],piece["sourceOutTicks"])
                if finish<=start: continue
                ratio=piece["timelineDurationTicks"]/(piece["sourceOutTicks"]-piece["sourceInTicks"])
                item_id=caption_item["id"] if index==0 else str(uuid.uuid4())
                placed={**caption_item,"id":item_id,"sourceInTicks":start,"sourceOutTicks":finish,"timelineStartTicks":piece["timelineStartTicks"]+round((start-piece["sourceInTicks"])*ratio),"timelineDurationTicks":max(1,round((finish-start)*ratio)),"properties":{**caption_item.get("properties",{}),"linkedClipId":piece["id"]}}
                new_items.append(placed)
                for record_index,record in enumerate(records): new_captions.append({**record,"id":record["id"] if index==0 and record_index==0 else str(uuid.uuid4()),"timelineItemId":item_id})
        return new_items,new_captions

    def record_last_opened(self) -> None:
        with _connect(self.database_path) as connection:
            connection.execute("UPDATE projects SET last_opened_at=? WHERE id=?", (_now(), self.project_id))
            connection.commit()

    @property
    def project_id(self) -> str:
        try:
            return str(json.loads(self.manifest_path.read_text(encoding="utf-8"))["projectId"])
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            raise CorruptProjectError(f"Cannot read project ID: {exc}") from exc

    def _current_revision(self) -> int:
        with _connect(self.database_path) as connection:
            row = connection.execute("SELECT state_revision FROM projects WHERE id=?", (self.project_id,)).fetchone()
        if row is None:
            raise ProjectError("Project no longer exists.")
        return int(row["state_revision"])

    def timeline_history(self, limit: int = 20) -> dict[str, Any]:
        """Return distinct prior timeline snapshots from the checksummed autosave journal."""
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("Timeline history limit must be between 1 and 20.")
        current = self.load_state()
        previous = {"items": current.get("timelineItems", []), "captions": current.get("captions", [])}
        with _connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT state_json,state_sha256 FROM autosaves WHERE project_id=? ORDER BY revision DESC LIMIT ?",
                (self.project_id, limit + 1),
            ).fetchall()
        truncated = len(rows) > limit
        snapshots: list[dict[str, Any]] = []
        for row in rows[:limit]:
            encoded = row["state_json"]
            if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != row["state_sha256"]:
                continue
            try:
                state = json.loads(encoded)
            except json.JSONDecodeError:
                continue
            if not isinstance(state, dict) or not isinstance(state.get("timelineItems", []), list) or not isinstance(state.get("captions", []), list):
                continue
            snapshot = {"items": state.get("timelineItems", []), "captions": state.get("captions", [])}
            if snapshot != previous:
                snapshots.append(snapshot)
                previous = snapshot
        return {"snapshots": list(reversed(snapshots)), "truncated": truncated}

    def load_state(self, connection=None) -> dict[str, Any]:
        owns_connection = connection is None
        with (_connect(self.database_path) if owns_connection else nullcontext(connection)) as connection:
            migrate_database(connection)
            project_id = self.project_id
            project = connection.execute("SELECT settings_json,state_revision FROM projects WHERE id=?", (project_id,)).fetchone()
            if project is None:
                raise CorruptProjectError("Project database has no matching project row.")
            timeline = connection.execute("SELECT * FROM timelines WHERE project_id=? AND is_current=1", (project_id,)).fetchone()
            items: list[dict[str, Any]] = []
            captions: list[dict[str, Any]] = []
            if timeline:
                for row in connection.execute("SELECT * FROM timeline_items WHERE timeline_id=? ORDER BY track_id,timeline_start_ticks", (timeline["id"],)):
                    items.append({"id": row["id"], "assetId": row["asset_id"], "itemType": row["item_type"], "trackId": row["track_id"], "timelineStartTicks": row["timeline_start_ticks"], "timelineDurationTicks": row["timeline_duration_ticks"], "sourceInTicks": row["source_in_ticks"], "sourceOutTicks": row["source_out_ticks"], "properties": json.loads(row["properties_json"])})
                captions = [{"id": row["id"], "timelineItemId": row["timeline_item_id"], "text": row["text"], "language": row["language"], "style": json.loads(row["style_json"])} for row in connection.execute("SELECT c.* FROM captions c JOIN timeline_items i ON i.id=c.timeline_item_id WHERE i.timeline_id=? ORDER BY i.timeline_start_ticks,c.id", (timeline["id"],))]
            transcripts: list[dict[str, Any]] = []
            for transcript in connection.execute("SELECT * FROM transcripts WHERE project_id=? ORDER BY created_at", (project_id,)):
                words = [dict(row) for row in connection.execute("SELECT id,ordinal,source_in_ticks,source_out_ticks,text,confidence,speaker_ordinal,is_user_corrected,is_protected FROM transcript_words WHERE transcript_id=? ORDER BY ordinal", (transcript["id"],))]
                transcripts.append({"id": transcript["id"], "assetId": transcript["asset_id"], "language": transcript["language"], "status": transcript["status"], "generatorId": transcript["generator_id"], "generatorVersion": transcript["generator_version"], "modelId": transcript["model_id"], "modelVersion": transcript["model_version"], "sourceFingerprint": transcript["source_fingerprint"], "words": words})
            analyses = [dict(row) for row in connection.execute("SELECT id,asset_id,kind,source_in_ticks,source_out_ticks,result_json,confidence,generator_id,generator_version,model_id,model_version,source_fingerprint,status,artifact_id,config_json FROM analyses WHERE project_id=? ORDER BY created_at", (project_id,))]
            assets = [dict(row) for row in connection.execute("SELECT id,original_uri,path_mode,last_known_path,size_bytes,mtime_ns,sha256,duration_ticks,metadata_json,relink_status FROM assets WHERE project_id=? ORDER BY created_at", (project_id,))]
            return {"projectId": project_id, "revision": project["state_revision"], "settings": json.loads(project["settings_json"]), "assets": assets, "timelineItems": items, "captions": captions, "transcripts": transcripts, "analyses": analyses}

    def recover_latest_autosave(self) -> bool:
        """Restore the latest verified snapshot if the canonical rows are corrupt/missing."""
        with _connect(self.database_path) as connection:
            migrate_database(connection)
            project_id = self.project_id
            rows = connection.execute("SELECT * FROM autosaves WHERE project_id=? ORDER BY revision DESC", (project_id,)).fetchall()
            for row in rows:
                raw = row["state_json"]
                if hashlib.sha256(raw.encode("utf-8")).hexdigest() != row["state_sha256"]:
                    continue
                try:
                    state = json.loads(raw)
                    self._validate_state(connection, project_id, state)
                    connection.execute("BEGIN IMMEDIATE")
                    self._replace_state(connection, project_id, state)
                    connection.execute("UPDATE projects SET state_revision=?,updated_at=? WHERE id=?", (row["revision"], _now(), project_id))
                    connection.commit()
                    return True
                except (ValueError, sqlite3.DatabaseError):
                    connection.rollback()
            return False

    def _restore_checkpoint(self, project_id: str) -> bool:
        checkpoints = sorted((self.root / "recovery").glob(f"project-{project_id}-checkpoint-*.sqlite3"), reverse=True)
        for checkpoint in checkpoints:
            temp_path = self.root / "recovery" / f"restore-{uuid.uuid4().hex}.sqlite3"
            try:
                backup = sqlite3.connect(checkpoint)
                try:
                    if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        continue
                    if backup.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
                        continue
                finally:
                    backup.close()
                source = sqlite3.connect(checkpoint)
                destination = sqlite3.connect(temp_path)
                try:
                    source.backup(destination)
                finally:
                    destination.close()
                    source.close()
                if self.database_path.exists():
                    os.replace(self.database_path, self.root / "recovery" / f"project-corrupt-{uuid.uuid4().hex}.sqlite3")
                os.replace(temp_path, self.database_path)
                return True
            except sqlite3.DatabaseError:
                continue
            finally:
                for suffix in ("", "-wal", "-shm"):
                    Path(str(temp_path) + suffix).unlink(missing_ok=True)
        return False

    def _checkpoint_files(self) -> list[tuple[int, Path]]:
        prefix = f"project-{self.project_id}-checkpoint-"
        checkpoints = []
        for path in (self.root / "recovery").glob(f"{prefix}*.sqlite3"):
            try:
                revision = int(path.name[len(prefix):-len(".sqlite3")])
            except ValueError:
                continue
            if not path.is_symlink() and path.is_file():
                checkpoints.append((revision, path))
        return sorted(checkpoints, key=lambda item: item[0], reverse=True)

    def _checkpoint_if_due(self, revision: int) -> Path | None:
        """Schedule bounded external backups after edits, without slowing each autosave."""
        checkpoints = self._checkpoint_files()
        due = not checkpoints
        if checkpoints:
            latest_revision, latest_path = checkpoints[0]
            try:
                age_seconds = max(0.0, time.time() - latest_path.stat().st_mtime)
            except OSError:
                age_seconds = RECOVERY_CHECKPOINT_MAX_AGE_SECONDS
            due = (revision - latest_revision >= RECOVERY_CHECKPOINT_MAX_REVISIONS
                   or age_seconds >= RECOVERY_CHECKPOINT_MAX_AGE_SECONDS)
        if not due:
            return None
        try:
            return self.checkpoint()
        except (OSError, sqlite3.DatabaseError, ProjectError):
            # The committed SQLite transaction remains durable; a backup failure
            # is surfaced in the structured local log and retried after a later edit.
            logger.exception("Could not write project recovery checkpoint", extra={"project_id": self.project_id, "revision": revision})
            return None

    def checkpoint(self) -> Path:
        """Create and validate a revision-consistent SQLite backup, never copy a live WAL DB."""
        target_root = self.root / "recovery"
        target_root.mkdir(parents=True, exist_ok=True)
        with _connect(self.database_path) as source:
            source.execute("BEGIN")
            revision_row = source.execute("SELECT state_revision FROM projects WHERE id=?", (self.project_id,)).fetchone()
            if revision_row is None:
                raise ProjectError("Cannot checkpoint a project that no longer exists.")
            revision = int(revision_row["state_revision"])
            target = target_root / f"project-{self.project_id}-checkpoint-{revision:08d}.sqlite3"
            fd, name = tempfile.mkstemp(prefix=".checkpoint-", suffix=".sqlite3", dir=target_root)
            os.close(fd)
            temporary = Path(name)
            try:
                destination = sqlite3.connect(temporary)
                try:
                    source.backup(destination)
                    if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ProjectError("SQLite rejected the recovery checkpoint integrity check.")
                    if destination.execute("SELECT 1 FROM projects WHERE id=?", (self.project_id,)).fetchone() is None:
                        raise ProjectError("Recovery checkpoint project identity did not match.")
                finally:
                    destination.close()
                with temporary.open("rb+") as stream:
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
                source.commit()
            finally:
                temporary.unlink(missing_ok=True)
        for _old_revision, old_path in self._checkpoint_files()[RECOVERY_CHECKPOINT_RETENTION:]:
            try:
                old_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not prune an old project recovery checkpoint", extra={"project_id": self.project_id, "path": str(old_path)})
        return target


@dataclass
class ProjectCatalog:
    data_dir: Path

    @property
    def pointer_path(self) -> Path:
        return self.data_dir / "last-project.json"

    def remember(self, root: Path) -> None:
        store = ProjectStore.open(root)
        payload = {"format": "localcut-last-project", "formatVersion": 1, "projectId": store.project_id, "path": Path(os.path.relpath(store.root, self.data_dir)).as_posix()}
        atomic_write(self.pointer_path, (_json(payload) + "\n").encode("utf-8"))

    def reopen_last(self) -> ProjectStore | None:
        try:
            payload = json.loads(self.pointer_path.read_text(encoding="utf-8"))
            if payload.get("format") != "localcut-last-project" or payload.get("formatVersion") != 1:
                return None
            root = (self.data_dir / payload["path"]).resolve()
            store = ProjectStore.open(root)
            if store.project_id != payload.get("projectId"):
                return None
            return store
        except (OSError, json.JSONDecodeError, KeyError, ProjectError, ValueError):
            return None


def save_manifest(root: Path) -> None:
    """Refresh the metadata-only manifest using an atomic replacement."""
    store = ProjectStore.open(root)
    with _connect(store.database_path) as connection:
        row = connection.execute("SELECT id,name,created_at,updated_at FROM projects WHERE id=?", (store.project_id,)).fetchone()
    atomic_write(store.manifest_path, (_json(_manifest(row["id"], row["name"], row["created_at"], row["updated_at"])) + "\n").encode("utf-8"))

