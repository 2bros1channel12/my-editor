"""Independent health checks for the local service, FFmpeg and storage."""

import shutil
import subprocess
from pathlib import Path
from .config import AppConfig


def check_ffmpeg(config: AppConfig) -> dict[str, object]:
    executable = config.ffmpeg_path or shutil.which("ffmpeg")
    if not executable:
        return {"available": False, "detail": "FFmpeg not found; media features are not enabled yet."}
    try:
        result = subprocess.run([executable, "-version"], capture_output=True, text=True, timeout=3, check=False)
        if result.returncode != 0:
            return {"available": False, "detail": "FFmpeg could not report its version."}
        version = result.stdout.splitlines()[0] if result.stdout else "FFmpeg available"
        return {"available": True, "detail": version, "version": version}
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "detail": "FFmpeg could not be started."}


def check_storage(data_dir: Path) -> dict[str, object]:
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".health-check"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return {"available": True, "detail": "Local storage is writable.", "path": str(data_dir)}
    except OSError as exc:
        return {"available": False, "detail": f"Local storage is not writable: {exc.__class__.__name__}"}
