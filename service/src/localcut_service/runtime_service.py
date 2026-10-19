"""Application-owned model tasks and content-free local execution audit."""
from __future__ import annotations

import hashlib
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import threading
import time

from .capabilities import _memory_bytes
from .config import AppConfig
from .logging_setup import JsonFormatter
from .model_runtime import ModelRuntime, ModelSpec, OllamaProvider, RuntimeFailure
from .persistence import ProjectStore, _connect, _sha256_file

SUMMARY_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object", "additionalProperties": False,
    "required": ["summary", "topics"],
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
        "topics": {"type": "array", "maxItems": 12, "items": {"type": "string", "minLength": 1, "maxLength": 100}},
    },
}
_AUDIT_LOCK = threading.Lock()


def _audit_logger(data_dir: Path):
    key = hashlib.sha256(str(data_dir.resolve()).encode()).hexdigest()
    logger = logging.getLogger(f"localcut-model-audit-{key}")
    with _AUDIT_LOCK:
        if not logger.handlers:
            directory = data_dir / "logs"
            directory.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(directory / "model-executions.jsonl", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
            handler.setFormatter(JsonFormatter())
            logger.addHandler(handler)
            logger.propagate = False
            logger.setLevel(logging.INFO)
    return logger


class AuditedRuntime(ModelRuntime):
    def __init__(self, provider, profile, models, data_dir):
        super().__init__(provider, profile, models)
        self.data_dir = data_dir

    def _recorded(self, operation, invoke):
        started = time.monotonic()
        try:
            result = invoke()
            # Explicit allowlist: never persist content, outputs, paths or prompts.
            fields = {key: value for key, value in result["provenance"].items() if key in {"provider", "providerVersion", "modelName", "modelVersion", "settings", "executionMs", "task"}}
            _audit_logger(self.data_dir).info("model_execution", extra={"component": "model_runtime", "event": "model_execution", "details": {"operation": operation, "state": "completed", **fields}})
            return result
        except Exception as exc:
            _audit_logger(self.data_dir).info("model_execution", extra={"component": "model_runtime", "event": "model_execution", "details": {"operation": operation, "state": "failed", "code": getattr(exc, "code", "cancelled_or_failed"), "executionMs": round((time.monotonic() - started) * 1000, 3)}})
            raise

    def embed(self, texts, check_cancelled=None):
        return self._recorded("embedding", lambda: super(AuditedRuntime, self).embed(texts, check_cancelled))

    def generate_json(self, content, schema, check_cancelled=None):
        return self._recorded("generation", lambda: super(AuditedRuntime, self).generate_json(content, schema, check_cancelled))


def configured_runtime(config: AppConfig) -> ModelRuntime:
    total, available, source = _memory_bytes()
    # Same memory capability probe used by Settings and ASR selection. CPU is the
    # conservative execution path; reported GPU inventory is not proof of support.
    profile = {"memory": {"totalBytes": total, "availableBytes": available, "source": source}, "cpu": {"logicalCores": os.cpu_count()}, "gpus": []}
    models = [ModelSpec(item["name"], item["task"], item["minimumRamBytes"], item.get("settings", {})) for item in config.runtime_models]
    return AuditedRuntime(OllamaProvider(config.runtime_base_url, config.runtime_timeout_seconds), profile, models, config.data_dir)


def runtime_status(config: AppConfig) -> dict:
    runtime = configured_runtime(config)
    result = runtime.status()
    result["endpoint"] = config.runtime_base_url
    result["memory"] = runtime.profile["memory"]
    result["selections"] = {}
    if result["available"]:
        for task in ("generation", "embedding"):
            try:
                result["selections"][task] = {"available": True, **runtime.select(task)}
            except RuntimeFailure as exc:
                result["selections"][task] = {"available": False, "code": exc.code, "detail": str(exc)}
    return result


def summarize_asset(store: ProjectStore, asset_id: str, runtime: ModelRuntime) -> dict:
    with _connect(store.database_path) as db:
        asset = db.execute("SELECT * FROM assets WHERE id=? AND project_id=?", (asset_id, store.project_id)).fetchone()
        if asset is None:
            raise ValueError("Choose an imported asset in this project.")
        transcript = db.execute("SELECT t.id,a.relative_path,a.checksum_sha256 FROM transcripts t JOIN artifacts a ON a.id=t.artifact_id WHERE t.asset_id=? AND t.source_fingerprint=? AND a.status='ready' ORDER BY t.created_at DESC LIMIT 1", (asset_id, asset["sha256"])).fetchone()
    if transcript is None:
        raise ValueError("Create a current local transcript before requesting a summary.")
    artifact_path = (store.root / transcript["relative_path"]).resolve()
    artifact_path.relative_to(store.root.resolve())
    if _sha256_file(artifact_path) != transcript["checksum_sha256"]:
        raise ValueError("The transcript failed integrity validation. Transcribe again before summarizing.")
    reviewed = store.get_transcript(transcript["id"])
    texts = [item["text"] for item in reviewed["segments"]] or [item["text"] for item in reviewed["words"]]
    if sum(len(value) for value in texts) > 20_000:
        raise ValueError("This transcript exceeds the summary input limit. Use footage search to review shorter passages.")
    response = runtime.generate_json({"untrustedContent": {"filename": str(asset["original_uri"]), "transcript": texts}}, SUMMARY_SCHEMA)
    return {"assetId": asset_id, "transcriptId": transcript["id"], "suggestionOnly": True, "schemaId": "footage-summary-v1", **response}
