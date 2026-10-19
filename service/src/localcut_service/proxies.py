"""Local, validated preview bundles. All process options are application-owned."""
from __future__ import annotations

from array import array
from contextlib import contextmanager
from fractions import Fraction
import json
import hashlib
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from typing import Any, TypedDict

from .jobs import JobContext, JobInput, JobOutput, TransientJobError
from .persistence import ProjectStore, _connect, _sha256_file
from .process_guard import ProcessGuard

# Bump when mapping.json changes, so cached bundles without the explicit source
# PTS origin are regenerated instead of interpreted with the new time contract.
PIPELINE_VERSION = "preview-2"


def source_digest(path: Path, context: JobContext | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            if context:
                context.check_cancelled()
            digest.update(block)
    return digest.hexdigest()


def source_path(store: ProjectStore, asset: Any) -> Path:
    return (store.root / asset["original_uri"] if asset["path_mode"] == "project-relative" else Path(asset["last_known_path"])).resolve(strict=True)


class ProxyPayload(TypedDict):
    assetId: str
    preset: str
    threads: int
    ffmpegVersion: str
    ffprobeVersion: str


class ProcessFailure(RuntimeError):
    pass


@contextmanager
def process_scratch():
    root = tempfile.mkdtemp(prefix="localcut-process-")
    try:
        yield root
    finally:
        # Windows may signal process exit just before releasing inherited files.
        for attempt in range(40):
            try:
                shutil.rmtree(root)
                break
            except PermissionError:
                if attempt == 39:
                    raise
                time.sleep(0.05)


class MediaProcess:
    """File-backed pipes cannot deadlock on stderr or a silent child process."""
    def run(self, args: list[str], context: JobContext, *, duration: float = 1,
            stage: str = "probing", base: float = 0, span: float = 0,
            timeout: float = 3600, stderr_capture: Path | None = None,
            stdout_file: Path | None = None, stdout_progress_only: bool = False, cwd: Path | None = None) -> bytes:
        with process_scratch() as scratch, open(Path(scratch) / "stdout", "w+b") as stdout, tempfile.TemporaryFile() as stderr:
            try:
                process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=stdout,
                    stderr=stderr, shell=False,
                    cwd=str(cwd) if cwd is not None else None,
                    start_new_session=os.name != "nt",
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            except OSError as exc:
                if exc.errno in {11, 16}:
                    raise TransientJobError("Media tool temporarily unavailable.") from exc
                raise ProcessFailure("Media tool could not start. Check FFmpeg/FFprobe configuration.") from exc
            start, last_report = time.monotonic(), 0.0
            guard = None
            try:
                guard = ProcessGuard(process)
                while process.poll() is None:
                    context.check_cancelled()
                    now = time.monotonic()
                    if now - start > timeout:
                        raise ProcessFailure(f"Media tool timed out during {stage}.")
                    if os.fstat(stdout.fileno()).st_size > 128 * 1024 * 1024 or os.fstat(stderr.fileno()).st_size > 16 * 1024 * 1024:
                        raise ProcessFailure("Media tool output exceeded the local safety limit.")
                    if span and now - last_report >= 0.5:
                        # A separate file handle leaves the child's write offset alone.
                        with open(Path(scratch) / "stdout", "rb") as reader:
                            reader.seek(0, 2)
                            reader.seek(max(0, reader.tell() - 4096))
                            tail = reader.read().decode("utf-8", "replace")
                        for line in reversed(tail.splitlines()):
                            if line.startswith("out_time_us="):
                                try:
                                    ratio = min(1, max(0, int(line.split("=", 1)[1]) / 1e6 / duration))
                                    context.report(base + span * ratio, stage)
                                except ValueError:
                                    pass
                                break
                        last_report = now
                    time.sleep(0.05)
                context.check_cancelled()
                if process.returncode:
                    stderr.seek(0, 2)
                    size = stderr.tell()
                    stderr.seek(max(0, size - 3000))
                    detail = stderr.read().decode("utf-8", "replace").strip()
                    raise ProcessFailure(f"{stage} failed (exit {process.returncode}): {detail}")
                stdout.seek(0, 2)
                if stdout.tell() > 128 * 1024 * 1024:
                    raise ProcessFailure("Media tool output exceeded the 128 MiB safety limit. Lower analysis sampling or inspect shorter footage.")
                stdout.seek(0)
                if stdout_file is not None:
                    if not stdout_progress_only:
                        stdout_file.parent.mkdir(parents=True, exist_ok=True)
                        with stdout_file.open("wb") as target:
                            shutil.copyfileobj(stdout, target, length=1024 * 1024)
                    result = b""
                else:
                    result = stdout.read()
                if stderr_capture is not None:
                    stderr.seek(0)
                    stderr_capture.write_bytes(stderr.read())
                return result
            finally:
                if guard:
                    guard.close()
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                process.wait()


def settings_for(profile: dict[str, Any]) -> dict[str, Any]:
    preset = profile.get("recommendations", {}).get("proxyPreset", "low")
    if preset not in {"low", "balanced", "high"}:
        preset = "low"
    cores = profile.get("cpu", {}).get("logicalCores") or 2
    return {"preset": preset, "threads": max(1, min(int(cores) // 2, 8))}


def estimate_bytes(duration: float, preset: str, audio: bool) -> int:
    # Peak VBV bitrate, mono PCM, frame indexes, thumbnails, muxer headroom.
    bitrate = {"low": 2_000_000, "balanced": 4_000_000, "high": 8_000_000}[preset]
    return math.ceil(duration * (bitrate / 8 + (32000 if audio else 0) + 128000) * 1.5 + 64 * 1024**2)


def video_stream(data: dict[str, Any]) -> dict[str, Any]:
    return next(s for s in data["streams"] if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic"))


def rotation(stream: dict[str, Any]) -> float:
    value = next((s["rotation"] for s in stream.get("side_data_list", []) if "rotation" in s), stream.get("tags", {}).get("rotate", 0))
    angle = float(value)
    if not math.isfinite(angle) or abs(angle / 90 - round(angle / 90)) > 0.001:
        raise ValueError("Only right-angle display rotations are currently supported.")
    return angle % 360


def frame_mapping(source: dict[str, Any], proxy: dict[str, Any]) -> dict[str, Any]:
    a, b = video_stream(source), video_stream(proxy)
    at, bt = Fraction(a["time_base"]), Fraction(b["time_base"])
    if at <= 0 or bt <= 0:
        raise ValueError("Invalid video timebase.")
    af = [f for f in source["frames"] if f.get("media_type", "video") == "video"]
    bf = [f for f in proxy["frames"] if f.get("media_type", "video") == "video"]
    if not af or len(af) != len(bf):
        raise ValueError("Proxy dropped or duplicated frames; source mapping is unsafe.")
    pairs = []
    for x, y in zip(af, bf):
        pairs.append({"sourcePts": int(x["best_effort_timestamp"]), "proxyPts": int(y["best_effort_timestamp"]),
                      "sourceDuration": x.get("duration", x.get("pkt_duration")), "proxyDuration": y.get("duration", y.get("pkt_duration")),
                      "sourceKeyframe": bool(x.get("key_frame")), "proxyKeyframe": bool(y.get("key_frame"))})
    offset = pairs[0]["sourcePts"] * at - pairs[0]["proxyPts"] * bt
    for i, pair in enumerate(pairs):
        if i and (pair["sourcePts"] <= pairs[i-1]["sourcePts"] or pair["proxyPts"] <= pairs[i-1]["proxyPts"]):
            raise ValueError("Missing or non-monotonic presentation timestamps.")
        if abs(pair["sourcePts"] * at - pair["proxyPts"] * bt - offset) > max(at, bt) * 2:
            raise ValueError("Proxy timestamps drift from source presentation times.")
    # Analysis/timeline times are media-relative, while FFprobe PTS may have a
    # non-zero container/stream origin (common in MOV and camera footage).
    source_start = Fraction(pairs[0]["sourcePts"]) * at
    return {"sourceTimebase": str(at), "proxyTimebase": str(bt), "sourceMinusProxySeconds": str(offset),
            "sourceStartSeconds": str(source_start), "frames": pairs}


class ProxyHandler:
    kind = "proxy"
    retry_safe = True

    def __init__(self, ffmpeg: str | None = None, ffprobe: str | None = None, process: MediaProcess | None = None):
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg")
        sibling = Path(self.ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe") if self.ffmpeg else None
        self.ffprobe = ffprobe or (str(sibling) if sibling and sibling.is_file() else shutil.which("ffprobe"))
        self.process = process or MediaProcess()

    def validate_input(self, payload: dict[str, Any]) -> ProxyPayload:
        if not isinstance(payload.get("assetId"), str) or not payload["assetId"]:
            raise ValueError("Proxy requires an imported assetId.")
        if payload.get("preset") not in {"low", "balanced", "high"}:
            raise ValueError("Invalid proxy preset.")
        threads = payload.get("threads", 2)
        if type(threads) is not int or not 1 <= threads <= 8:
            raise ValueError("Proxy threads must be between 1 and 8.")
        return {"assetId": payload["assetId"], "preset": payload["preset"], "threads": threads,
                "ffmpegVersion": str(payload.get("ffmpegVersion", "unknown")), "ffprobeVersion": str(payload.get("ffprobeVersion", "unknown"))}

    def validate_job(self, job: JobInput) -> None:
        # Also runs before deduplication so changed originals cannot reuse stale previews.
        store = ProjectStore.open(Path(job.project_path))
        with _connect(store.database_path) as db:
            asset = db.execute("SELECT * FROM assets WHERE id=? AND project_id=?", (job.payload["assetId"], store.project_id)).fetchone()
        if asset is None or asset["sha256"] != job.source_fingerprint:
            raise ValueError("Proxy source fingerprint does not match the imported asset.")
        if source_digest(source_path(store, asset)) != job.source_fingerprint:
            raise ValueError("Source changed since import; reimport before generating a proxy.")

    def probe(self, path: Path, context: JobContext, frames: bool = False) -> dict[str, Any]:
        if not self.ffprobe:
            raise ProcessFailure("FFprobe is unavailable.")
        args = [self.ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe", "-show_streams", "-show_format"]
        if frames:
            args += ["-select_streams", "v:0", "-show_frames", "-show_entries", "frame=media_type,best_effort_timestamp,duration,pkt_duration,key_frame"]
        return json.loads(self.process.run(args + ["-of", "json", str(path)], context, stage="indexing timestamps"))

    def ff(self, source: Path, options: list[str], context: JobContext, **progress: Any) -> None:
        if not self.ffmpeg:
            raise ProcessFailure("FFmpeg is unavailable.")
        self.process.run([self.ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-v", "error", "-xerror", "-n",
            "-progress", "pipe:1", "-protocol_whitelist", "file,pipe", "-copyts", "-start_at_zero", "-i", str(source)] + options, context, **progress)

    def run(self, context: JobContext, payload: ProxyPayload) -> JobOutput:
        store = ProjectStore.open(Path(context.input.project_path))
        with _connect(store.database_path) as db:
            asset = db.execute("SELECT * FROM assets WHERE id=? AND project_id=?", (payload["assetId"], store.project_id)).fetchone()
        if asset is None:
            raise ValueError("Imported source asset was not found.")
        source = source_path(store, asset)
        context.report(0.01, "checking source")
        if source_digest(source, context) != context.input.source_fingerprint:
            raise ValueError("Source changed since import; reimport before generating a proxy.")
        original = self.probe(source, context)
        video = video_stream(original)
        angle = rotation(video)
        duration = float(original["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Source has no usable duration.")
        audios = [s for s in original["streams"] if s["codec_type"] == "audio"]
        audio = next((s for s in audios if s.get("disposition", {}).get("default")), audios[0] if audios else None)
        required = estimate_bytes(duration, payload["preset"], audio is not None)
        if shutil.disk_usage(store.root).free < required + 256 * 1024**2:
            raise ValueError(f"Insufficient project disk space; approximately {required} bytes plus 256 MiB reserve required.")
        root = context.staging_file(".bundle")
        root.mkdir()
        proxy = root / "proxy.mp4"
        max_side, rate = {"low": (720, "2M"), "balanced": (1280, "4M"), "high": (1920, "8M")}[payload["preset"]]
        # Autorotate occurs before scale. scale preserves DAR by adjusting SAR.
        scale = f"scale=w='min({max_side},iw)':h='min({max_side},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"
        options = ["-map", f"0:{video['index']}"]
        if audio:
            options += ["-map", f"0:{audio['index']}", "-c:a", "aac", "-b:a", "128k"]
        else:
            options += ["-an"]
        options += ["-vf", scale, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-maxrate", rate, "-bufsize", rate,
            "-threads", str(payload["threads"]), "-pix_fmt", "yuv420p", "-fps_mode", "passthrough", "-enc_time_base:v", video["time_base"],
            "-video_track_timescale", str(Fraction(video["time_base"]).denominator), "-map_metadata", "-1", "-metadata:s:v:0", "rotate=0",
            "-avoid_negative_ts", "disabled", "-movflags", "+faststart", "-f", "mp4", str(proxy)]
        self.ff(source, options, context, duration=duration, stage="encoding proxy", base=0.05, span=0.55)
        context.report(0.61, "validating timing")
        encoded = self.probe(proxy, context)
        pv = video_stream(encoded)
        if rotation(pv) != 0:
            raise ValueError("Proxy retains a display rotation; refusing double-rotated preview.")
        sar = Fraction(str(video.get("sample_aspect_ratio", "1:1")).replace(":", "/"))
        psar = Fraction(str(pv.get("sample_aspect_ratio", "1:1")).replace(":", "/"))
        dar = Fraction(video["width"], video["height"]) * sar
        if angle in {90, 270}:
            dar = 1 / dar
        pdar = Fraction(pv["width"], pv["height"]) * psar
        if abs(float(pdar / dar) - 1) > 0.01:
            raise ValueError("Proxy display aspect ratio does not match source.")
        mapping = frame_mapping(self.probe(source, context, True), self.probe(proxy, context, True))
        pa = next((s for s in encoded["streams"] if s["codec_type"] == "audio"), None)
        if audio:
            if pa is None:
                raise ValueError("Proxy lost the selected audio track.")
            before = float(audio.get("start_time", 0)) - float(video.get("start_time", 0))
            after = float(pa.get("start_time", 0)) - float(pv.get("start_time", 0))
            if abs(before - after) > 0.05 or abs(float(audio["duration"]) - float(pa["duration"])) > 0.1:
                raise ValueError("Proxy audio synchronization failed validation.")
        # Decode entire output: container metadata alone cannot prove decodability.
        self.ff(proxy, ["-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"], context, stage="validating decode")
        analysis = None
        if audio:
            context.report(0.75, "analysis audio")
            wav = root / "analysis.wav"
            self.ff(source, ["-map", f"0:{audio['index']}", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "wav", str(wav)], context, stage="analysis audio")
            peaks = []
            with wave.open(str(wav), "rb") as reader:
                if reader.getnchannels() != 1 or reader.getframerate() != 16000 or reader.getsampwidth() != 2 or reader.getnframes() == 0:
                    raise ValueError("Invalid analysis audio.")
                samples = reader.getnframes()
                if abs(samples / 16000 - float(audio["duration"])) > 0.1:
                    raise ValueError("Analysis audio duration does not match source audio.")
                while block := reader.readframes(160):
                    context.check_cancelled()
                    values = array("h", block)
                    if sys.byteorder != "little":
                        values.byteswap()
                    peaks.append([min(values) / 32768, max(values) / 32768])
            analysis = {"path": "analysis.wav", "sampleRate": 16000, "samples": samples,
                        "sourceStartSeconds": audio.get("start_time", "0"), "sourceTimebase": audio["time_base"], "sourceStreamIndex": audio["index"]}
            (root / "waveform.json").write_text(json.dumps({"schemaVersion": 1, "samplesPerBin": 160, "audio": analysis, "peaks": peaks}), encoding="utf-8")
        thumbnails = []
        frames = mapping["frames"]
        for number, index in enumerate(sorted({0, len(frames)//4, len(frames)//2, 3*len(frames)//4, len(frames)-1})):
            context.report(0.8 + number * 0.025, "thumbnails")
            thumb = root / f"thumbnail-{number}.png"
            self.ff(proxy, ["-vf", f"select=eq(n\\,{index}),scale=320:trunc(320/dar/2)*2,setsar=1", "-frames:v", "1", "-an", "-fps_mode", "vfr", "-f", "image2", "-update", "1", str(thumb)], context, stage="thumbnail")
            if not thumb.is_file() or thumb.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
                raise ValueError("Invalid thumbnail output.")
            self.ff(thumb, ["-f", "null", "-"], context, stage="validating thumbnail")
            thumbnails.append({"path": thumb.name, "frameIndex": index, "sourcePts": frames[index]["sourcePts"]})
        (root / "mapping.json").write_text(json.dumps(mapping), encoding="utf-8")
        if source_digest(source, context) != context.input.source_fingerprint:
            raise ValueError("Source changed during generation; discarding output.")
        manifest = {"schemaVersion": 1, "pipelineVersion": PIPELINE_VERSION, "assetId": payload["assetId"],
            "sourceFingerprint": context.input.source_fingerprint, "ffmpegVersion": payload["ffmpegVersion"], "settings": payload,
            "generators": {"media": {"id": "ffmpeg", "version": payload["ffmpegVersion"]}, "timestamps": {"id": "ffprobe", "version": payload["ffprobeVersion"]}, "waveform": {"id": "localcut-pcm-peaks", "version": PIPELINE_VERSION}},
            "sourceStreams": original["streams"], "proxyStreams": encoded["streams"], "sourceRotation": angle,
            "proxy": "proxy.mp4", "mapping": "mapping.json", "analysis": analysis, "waveform": "waveform.json" if audio else None,
            "thumbnails": thumbnails, "estimatedBytes": required,
            "files": {p.name: _sha256_file(p) for p in root.iterdir()}}
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        context.report(0.97, "committing preview bundle")
        return JobOutput(str(root), f"cache/proxies/{context.job_id}", "proxy", {"manifest": "manifest.json", "pipelineVersion": PIPELINE_VERSION})

    def validate_output(self, output: JobOutput) -> None:
        root = Path(output.staging_path)
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        for name, digest in manifest["files"].items():
            path = root / name
            if path.parent != root or not path.is_file() or path.stat().st_size == 0 or _sha256_file(path) != digest:
                raise ValueError("Preview bundle checksum validation failed.")


def proxy_job(store: ProjectStore, asset_id: str, profile: dict[str, Any]) -> JobInput:
    with _connect(store.database_path) as db:
        asset = db.execute("SELECT sha256 FROM assets WHERE id=? AND project_id=?", (asset_id, store.project_id)).fetchone()
    if asset is None:
        raise ValueError("Imported asset not found.")
    version = profile.get("mediaTools", {}).get("ffmpeg", {}).get("version") or "unknown"
    probe_version = profile.get("mediaTools", {}).get("ffprobe", {}).get("version") or "unknown"
    return JobInput(str(store.root), "proxy", {"assetId": asset_id, **settings_for(profile), "ffmpegVersion": version, "ffprobeVersion": probe_version},
        asset["sha256"], pipeline_version=PIPELINE_VERSION, asset_id=asset_id, generator_id="ffmpeg", generator_version=version)
