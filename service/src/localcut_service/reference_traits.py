"""Ephemeral, local extraction of neutral creator-reference editing traits."""
from __future__ import annotations

import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .media import FFprobe, ProbeError

GENERATOR_ID = "localcut-neutral-reference-traits"
GENERATOR_VERSION = "2"
MAX_ANALYSIS_SECONDS = 300
SAMPLE_FPS = 1
FRAME_SIZE = 160 * 90
NARRATIVE_STRUCTURES = {
    "unknown", "chronological", "problem-solution", "question-answer", "step-by-step",
    "demonstration-first", "hook-explanation-summary", "story-led-lesson", "before-after",
}


def validate_traits(values: object) -> dict:
    if not isinstance(values, dict):
        raise ValueError("Reference traits must be an object.")
    allowed = {"pacing", "averageShotLengthSeconds", "jumpCutFrequencyPerMinute", "captionDensityPerMinute", "punchInFrequencyPerMinute", "musicEnergy", "humorLevel", "narrativeStructure"}
    if set(values) - allowed:
        raise ValueError("Reference profile contains an unsupported trait.")
    result = {}
    for key in values:
        value = values.get(key)
        if key in {"averageShotLengthSeconds", "jumpCutFrequencyPerMinute", "captionDensityPerMinute", "punchInFrequencyPerMinute"}:
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or value > 10000):
                raise ValueError(f"{key} must be a non-negative number or unknown.")
            result[key] = round(value, 2) if value is not None else None
        elif key == "narrativeStructure":
            if not isinstance(value, str) or value not in NARRATIVE_STRUCTURES: raise ValueError("Narrative structure must use a supported neutral structure category.")
            result[key] = value
        elif key in {"pacing", "musicEnergy", "humorLevel"}:
            choices = {"slow", "moderate", "fast", "unknown"} if key == "pacing" else {"low", "moderate", "high", "unknown"}
            if value not in choices: raise ValueError(f"{key} is not a supported neutral trait value.")
            result[key] = value
    return result


def _traits_from_changes(duration: float, timestamps: list[float], frames: bytes) -> dict:
    if len(timestamps) * FRAME_SIZE != len(frames):
        raise ValueError("Could not align reference sample timestamps with decoded frames.")
    changes = []
    for index in range(1, len(timestamps)):
        left = frames[(index - 1) * FRAME_SIZE:index * FRAME_SIZE]
        right = frames[index * FRAME_SIZE:(index + 1) * FRAME_SIZE]
        difference = sum(abs(a - b) for a, b in zip(left, right)) / (255 * FRAME_SIZE)
        if difference >= 0.24:
            changes.append((timestamps[index - 1] + timestamps[index]) / 2)
    analyzed_duration = min(duration, MAX_ANALYSIS_SECONDS)
    per_minute = len(changes) / max(analyzed_duration / 60, 1 / 60)
    average_shot = analyzed_duration / (len(changes) + 1) if analyzed_duration > 0 else None
    pacing = "fast" if average_shot is not None and average_shot < 4 else "moderate" if average_shot is not None and average_shot < 10 else "slow" if average_shot is not None else "unknown"
    return {
        "pacing": pacing,
        "averageShotLengthSeconds": round(average_shot, 2) if average_shot is not None else None,
        "jumpCutFrequencyPerMinute": round(per_minute, 2),
        "captionDensityPerMinute": None,
        "punchInFrequencyPerMinute": None,
        "musicEnergy": "unknown",
        "humorLevel": "unknown",
        "narrativeStructure": "unknown",
    }


def analyze_reference(path_value: str, *, ffmpeg: str | None = None, ffprobe: str | None = None) -> dict:
    try:
        path = Path(path_value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError("The selected reference file is unavailable. Choose an existing local file.") from exc
    if not path.is_file() or path.suffix.lower() not in {".mp4", ".mov"}:
        raise ValueError("Choose a local MP4 or MOV reference video.")
    executable = ffmpeg or shutil.which("ffmpeg")
    if not executable:
        raise ValueError("FFmpeg is unavailable. Configure FFmpeg in Settings to analyze a local reference.")
    try:
        metadata = FFprobe(ffprobe).probe(path)
    except ProbeError as exc:
        raise ValueError(str(exc)) from exc
    duration = metadata.get("durationSeconds")
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("The reference video has no usable duration.")
    log_pattern = re.compile(r"pts_time:([-+0-9.eE]+)")
    with tempfile.TemporaryDirectory(prefix="localcut-reference-") as temporary:
        raw_path = Path(temporary) / "samples.gray"
        vf = f"fps={SAMPLE_FPS},scale=160:90:force_original_aspect_ratio=decrease,pad=160:90:(ow-iw)/2:(oh-ih)/2,format=gray,showinfo"
        args = [executable, "-hide_banner", "-nostdin", "-v", "info", "-protocol_whitelist", "file,pipe", "-t", str(MAX_ANALYSIS_SECONDS), "-i", str(path), "-map", "0:v:0", "-an", "-vf", vf, "-fps_mode", "vfr", "-f", "rawvideo", "-pix_fmt", "gray", str(raw_path)]
        try:
            result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("Reference sampling exceeded two minutes. Choose a shorter reference clip.") from exc
        except OSError as exc:
            raise ValueError("FFmpeg could not start to sample the reference.") from exc
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace")[-1000:]
            final_line = detail.strip().splitlines()[-1] if detail.strip() else "No decoder details were provided."
            raise ValueError("FFmpeg could not decode the reference video: " + final_line)
        stderr = result.stderr.decode("utf-8", "replace")
        timestamps = [float(value) for value in log_pattern.findall(stderr)]
        if not timestamps:
            raise ValueError("No timestamped video frames could be sampled from the reference.")
        raw = raw_path.read_bytes()
        traits = _traits_from_changes(float(duration), timestamps, raw)
    return {
        "generatorId": GENERATOR_ID,
        "generatorVersion": GENERATOR_VERSION,
        "traits": traits,
        "analyzedSeconds": min(float(duration), MAX_ANALYSIS_SECONDS),
        "sampleFrames": len(timestamps),
        "explanation": "Pacing and cut-like change rate are estimated from one grayscale frame per second in the first five minutes. Camera movement, screen changes, flashes, and deliberate pauses can affect these measurements. Other traits are left unknown for you to set; narrative structure uses a fixed neutral category list. No identity, face, voice, branding, asset, or music profile is created.",
        "ephemeral": True,
    }
