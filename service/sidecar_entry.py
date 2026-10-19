"""Packaged-service entry point for the Tauri Windows sidecar."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from localcut_service.server import serve  # noqa: E402

raise SystemExit(serve())
