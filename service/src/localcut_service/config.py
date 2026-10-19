"""Validated local configuration with defaults < user JSON < environment overrides."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class AppConfig:
    data_dir: Path
    log_level: str = "INFO"
    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None
    job_concurrency: int = 2
    runtime_base_url: str = "http://127.0.0.1:11434"
    runtime_timeout_seconds: int = 30
    runtime_models: tuple[dict, ...] = ()


def default_data_dir() -> Path:
    override = os.environ.get("LOCAL_CUT_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "LocalCut"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "localcut"


def load_config(path: Path | None = None) -> AppConfig:
    file_path = path or Path(os.environ.get("LOCAL_CUT_CONFIG", default_data_dir() / "config.json"))
    values: dict[str, object] = {}
    if file_path.exists():
        try:
            parsed = json.loads(file_path.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("configuration root must be an object")
            values = parsed
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"Invalid local configuration at {file_path}: {exc}") from exc
    data_dir = Path(os.environ.get("LOCAL_CUT_DATA_DIR", str(values.get("dataDir", default_data_dir())))).expanduser()
    log_level = os.environ.get("LOCAL_CUT_LOG_LEVEL", str(values.get("logLevel", "INFO"))).upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ValueError("logLevel must be DEBUG, INFO, WARNING, or ERROR")
    ffmpeg_path = os.environ.get("LOCAL_CUT_FFMPEG", values.get("ffmpegPath"))
    if ffmpeg_path is not None and not isinstance(ffmpeg_path, str):
        raise ValueError("ffmpegPath must be a string")
    ffprobe_path = os.environ.get("LOCAL_CUT_FFPROBE", values.get("ffprobePath"))
    if ffprobe_path is not None and not isinstance(ffprobe_path, str):
        raise ValueError("ffprobePath must be a string")
    try:
        job_concurrency = int(os.environ.get("LOCAL_CUT_JOB_CONCURRENCY", values.get("jobConcurrency", 2)))
    except (TypeError, ValueError) as exc:
        raise ValueError("jobConcurrency must be an integer from 1 to 16") from exc
    if not 1 <= job_concurrency <= 16:
        raise ValueError("jobConcurrency must be between 1 and 16")
    runtime_url = os.environ.get("LOCAL_CUT_MODEL_URL", values.get("modelRuntimeUrl", "http://127.0.0.1:11434"))
    try:
        endpoint = urlsplit(runtime_url)
        valid_url = endpoint.scheme == "http" and endpoint.hostname in {"127.0.0.1", "::1"} and not endpoint.username and not endpoint.password and not endpoint.query and not endpoint.fragment and endpoint.path in {"", "/"} and (endpoint.port is None or 1 <= endpoint.port <= 65535)
    except (TypeError, ValueError):
        valid_url = False
    if not valid_url:
        raise ValueError("modelRuntimeUrl must be a numeric loopback HTTP endpoint with no credentials, query or extra path.")
    raw_timeout = os.environ.get("LOCAL_CUT_MODEL_TIMEOUT", values.get("modelRuntimeTimeoutSeconds", 30))
    try:
        timeout = int(raw_timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("modelRuntimeTimeoutSeconds must be an integer from 1 to 120.") from exc
    if isinstance(raw_timeout, bool) or str(raw_timeout) != str(timeout) or not 1 <= timeout <= 120:
        raise ValueError("modelRuntimeTimeoutSeconds must be an integer from 1 to 120.")
    runtime_models = values.get("modelRuntimeModels", [])
    if not isinstance(runtime_models, list) or len(runtime_models) > 16:
        raise ValueError("modelRuntimeModels must be a list of at most 16 configured local models.")
    for model in runtime_models:
        if not isinstance(model, dict) or set(model) - {"name", "task", "minimumRamBytes", "settings"}:
            raise ValueError("Each runtime model requires only name, task, minimumRamBytes and optional settings.")
        if not isinstance(model.get("name"), str) or not 1 <= len(model["name"]) <= 200 or any(ord(c) < 32 for c in model["name"]):
            raise ValueError("Runtime model names must be bounded printable strings.")
        if model.get("task") not in {"generation", "embedding"} or type(model.get("minimumRamBytes")) is not int or not 1 <= model["minimumRamBytes"] <= 1024**4 or not isinstance(model.get("settings", {}), dict):
            raise ValueError("Runtime models require generation/embedding task, positive minimumRamBytes, and object settings.")
    return AppConfig(data_dir=data_dir, log_level=log_level, ffmpeg_path=ffmpeg_path, ffprobe_path=ffprobe_path, job_concurrency=job_concurrency, runtime_base_url=runtime_url, runtime_timeout_seconds=timeout, runtime_models=tuple(runtime_models))
