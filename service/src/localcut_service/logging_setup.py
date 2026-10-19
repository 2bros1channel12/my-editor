"""Structured rotating local logs. Request payloads and user content are never logged."""

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "component": getattr(record, "component", "service"),
            "event": getattr(record, "event", record.getMessage()),
            "requestId": getattr(record, "request_id", None),
            "jobId": getattr(record, "job_id", None),
            "details": getattr(record, "details", None),
            "error": getattr(record, "error", None),
        }
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(log_dir: Path, level: str = "INFO") -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("localcut")
    logger.setLevel(level)
    logger.handlers.clear()
    handler = RotatingFileHandler(log_dir / "service.jsonl", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    return logger
