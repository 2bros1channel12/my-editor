"""Safe MP4/MOV discovery, FFprobe inspection and reference-only project import."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Callable, Iterable

from .persistence import ProjectError

SUPPORTED_EXTENSIONS = {".mp4", ".mov"}


@dataclass(frozen=True)
class MediaImportError:
    path: str
    code: str
    message: str


class ProbeError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def discover_files(inputs: Iterable[str | Path]) -> tuple[list[Path], list[MediaImportError]]:
    """Find candidate files, sorted and deduplicated; never traverse symlink dirs."""
    found: dict[str, Path] = {}
    errors: list[MediaImportError] = []
    for raw in inputs:
        item = Path(raw).expanduser()
        try:
            if item.is_symlink() and item.is_dir():
                errors.append(MediaImportError(str(item), "symlink_directory_skipped", "Symbolic-link directories are skipped to avoid cycles."))
                continue
            if item.is_file():
                candidates = [item]
            elif item.is_dir():
                candidates = []
                for current, dirs, files in os.walk(item, followlinks=False):
                    dirs[:] = sorted(name for name in dirs if not name.startswith(".") and not (Path(current) / name).is_symlink())
                    candidates.extend(Path(current) / name for name in files if not name.startswith("."))
            else:
                errors.append(MediaImportError(str(item), "unavailable", "Path does not exist or is not accessible."))
                continue
            for candidate in candidates:
                if candidate.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue
                try:
                    canonical = candidate.resolve(strict=True)
                    if canonical.is_file():
                        found.setdefault(os.path.normcase(str(canonical)), canonical)
                except OSError as exc:
                    errors.append(MediaImportError(str(candidate), "unavailable", f"Cannot access file: {exc.__class__.__name__}."))
        except OSError as exc:
            errors.append(MediaImportError(str(item), "unavailable", f"Cannot scan path: {exc.__class__.__name__}."))
    return [found[key] for key in sorted(found)], errors


def _rate(value: Any) -> dict[str, Any]:
    raw = str(value or "")
    if not raw or raw in {"N/A", "0/0", "0"}:
        return {"raw": raw or None, "numerator": None, "denominator": None, "value": None}
    try:
        fraction = Fraction(raw)
        if fraction.denominator == 0 or fraction.numerator <= 0:
            raise ValueError
        return {"raw": raw, "numerator": fraction.numerator, "denominator": fraction.denominator, "value": float(fraction)}
    except (ValueError, ZeroDivisionError):
        return {"raw": raw, "numerator": None, "denominator": None, "value": None}


def _duration_ticks(*values: Any) -> int | None:
    for value in values:
        try:
            fraction = Fraction(str(value))
            if fraction >= 0:
                return int(fraction * 1_000_000)
        except (ValueError, ZeroDivisionError, TypeError):
            continue
    return None


def _duration_seconds(*values: Any) -> float | None:
    for value in values:
        try:
            fraction = Fraction(str(value))
            if fraction >= 0:
                return float(fraction)
        except (ValueError, ZeroDivisionError, TypeError):
            continue
    return None


def _rotation(stream: dict[str, Any]) -> dict[str, Any]:
    raw: Any = None
    source = None
    for side in stream.get("side_data_list", []):
        if "rotation" in side:
            raw, source = side["rotation"], "display_matrix"
            break
    if raw is None:
        raw = stream.get("tags", {}).get("rotate")
        source = "tag" if raw is not None else None
    if raw is None:
        return {"degrees": 0, "raw": None, "source": None}
    try:
        normalized = float(raw) % 360
        return {"degrees": normalized, "raw": raw, "source": source}
    except (TypeError, ValueError):
        return {"degrees": None, "raw": str(raw), "source": source}


def normalize_probe(data: dict[str, Any]) -> dict[str, Any]:
    streams = data.get("streams")
    if not isinstance(streams, list):
        raise ProbeError("invalid_probe", "FFprobe returned no stream list.")
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    if not video_streams:
        raise ProbeError("no_video", "No video stream was found in this file.")
    video = video_streams[0]
    format_info = data.get("format") if isinstance(data.get("format"), dict) else {}
    format_names = {item.lower() for item in str(format_info.get("format_name", "")).split(",")}
    if not (format_names & {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}):
        raise ProbeError("unsupported_container", "File content is not a supported MP4 or MOV container.")

    avg = _rate(video.get("avg_frame_rate"))
    nominal = _rate(video.get("r_frame_rate"))
    # Small rational rounding differences are common, while a measurable
    # divergence is useful evidence (but still not a definitive VFR claim).
    vfr_evidence = bool(avg["value"] and nominal["value"] and abs(avg["value"] - nominal["value"]) > max(0.005, nominal["value"] * 0.0005))
    return {
        "container": format_info.get("format_name"),
        "formatLongName": format_info.get("format_long_name"),
        "durationSeconds": _duration_seconds(format_info.get("duration"), video.get("duration")),
        "durationTicks": _duration_ticks(format_info.get("duration"), video.get("duration")),
        "timebase": _rate(video.get("time_base")),
        "video": {
            "codec": video.get("codec_name"),
            "profile": video.get("profile"),
            "width": video.get("width"),
            "height": video.get("height"),
            "avgFrameRate": avg,
            "nominalFrameRate": nominal,
            "timebase": _rate(video.get("time_base")),
            "startTime": video.get("start_time"),
            "rotation": _rotation(video),
            "sampleAspectRatio": video.get("sample_aspect_ratio"),
            "displayAspectRatio": video.get("display_aspect_ratio"),
            "displaySideData": video.get("side_data_list", []),
            "variableFrameRate": {"likely": vfr_evidence, "confidence": "low" if not vfr_evidence else "medium", "evidence": "average and nominal frame rates differ" if vfr_evidence else "no distinguishing rate evidence"},
        },
        "audio": [
            {"index": stream.get("index"), "codec": stream.get("codec_name"), "channels": stream.get("channels"), "channelLayout": stream.get("channel_layout"), "sampleRate": stream.get("sample_rate"), "startTime": stream.get("start_time"), "timebase": _rate(stream.get("time_base")), "durationSeconds": _duration_seconds(stream.get("duration")), "default": bool(stream.get("disposition", {}).get("default"))}
            for stream in streams if stream.get("codec_type") == "audio"
        ],
        "streamCount": len(streams),
        "formatStartTime": format_info.get("start_time"),
    }


class FFprobe:
    def __init__(self, executable: str | None = None, *, runner: Callable[..., Any] = subprocess.run, timeout: float = 12.0):
        self.executable = executable or shutil.which("ffprobe")
        self.runner = runner
        self.timeout = timeout

    def version(self) -> str | None:
        if not self.executable:
            return None
        try:
            result = self.runner([self.executable, "-version"], capture_output=True, text=True, timeout=3, check=False)
            return result.stdout.splitlines()[0] if result.returncode == 0 and result.stdout else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    def probe(self, path: Path) -> dict[str, Any]:
        if not self.executable:
            raise ProbeError("ffprobe_unavailable", "FFprobe was not found. Set the FFprobe path in Settings or install FFmpeg.")
        try:
            result = self.runner([self.executable, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], capture_output=True, text=True, timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ProbeError("probe_timeout", "FFprobe timed out while inspecting this file.") from exc
        except OSError as exc:
            raise ProbeError("ffprobe_unavailable", f"FFprobe could not be started: {exc.__class__.__name__}.") from exc
        if result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()
            raise ProbeError("corrupt_or_unsupported", detail[-1] if detail else "FFprobe could not read this media file.")
        try:
            decoded = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ProbeError("invalid_probe", "FFprobe returned malformed metadata.") from exc
        return normalize_probe(decoded)


def import_media(store: Any, inputs: Iterable[str | Path], *, probe: FFprobe | None = None, storage_mode: str = "linked") -> dict[str, Any]:
    if storage_mode not in {"linked", "managed"}:
        raise ValueError("Storage mode must be linked or managed.")
    ffprobe = probe or FFprobe()
    candidates, errors = discover_files(inputs)
    imported: list[dict[str, Any]] = []
    version = ffprobe.version() or "unknown"
    for path in candidates:
        try:
            metadata = ffprobe.probe(path)
            importer = store.import_asset_managed if storage_mode == "managed" else store.import_asset_reference
            result = importer(path, metadata, probe_tool="ffprobe", probe_version=version, duration_ticks=metadata["durationTicks"], timebase_num=metadata["video"]["timebase"]["numerator"] or 1, timebase_den=metadata["video"]["timebase"]["denominator"] or 1_000_000)
            imported.append({"path": str(path), **result, "metadata": metadata})
        except ProbeError as exc:
            errors.append(MediaImportError(str(path), exc.code, str(exc)))
        except (OSError, PermissionError) as exc:
            errors.append(MediaImportError(str(path), "unavailable", f"File became unavailable during import: {exc.__class__.__name__}."))
        except ProjectError as exc:
            errors.append(MediaImportError(str(path), "source_changed", str(exc)))
        except Exception as exc:
            errors.append(MediaImportError(str(path), "import_failed", f"Could not register this media: {exc.__class__.__name__}."))
    return {"imported": imported, "errors": [error.__dict__ for error in errors], "candidateCount": len(candidates)}


def relink_media(store: Any, asset_id: str, candidate_path: str | Path, *, probe: FFprobe | None = None, allow_changed: bool = False) -> dict[str, Any]:
    ffprobe = probe or FFprobe()
    path = Path(candidate_path).expanduser().resolve(strict=True)
    metadata = ffprobe.probe(path)
    return store.relink_asset(
        asset_id, path, metadata=metadata, probe_tool="ffprobe", probe_version=ffprobe.version() or "unknown",
        allow_changed=allow_changed, duration_ticks=metadata["durationTicks"],
        timebase_num=metadata["video"]["timebase"]["numerator"] or 1,
        timebase_den=metadata["video"]["timebase"]["denominator"] or 1_000_000,
    )
