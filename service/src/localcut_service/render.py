"""Nondestructive, local H.264 rendering from a validated timeline snapshot."""
from __future__ import annotations

from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
from typing import Any
import uuid

from .jobs import JobContext, JobInput, JobOutput
from .persistence import ProjectStore, _connect, _json, _now
from .proxies import MediaProcess, rotation, source_digest, source_path, video_stream

GENERATOR_ID = "localcut-ffmpeg-renderer"
GENERATOR_VERSION = "8"
PIPELINE_VERSION = "h264-mp4-8"
TICKS_PER_SECOND = 1_000_000


def _render_fingerprint(snapshot: dict[str, Any]) -> str:
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _tool_identity(path: str) -> dict[str, Any]:
    executable = Path(path).resolve(strict=True)
    stat = executable.stat()
    return {"path": str(executable), "size": stat.st_size, "modifiedNs": stat.st_mtime_ns}


def _validated_captions(captions: list[dict[str, Any]], duration_ticks: int) -> list[dict[str, Any]]:
    clean = []
    for caption in captions:
        start, length = caption.get("timelineStartTicks"), caption.get("timelineDurationTicks")
        text = caption.get("text")
        if type(start) is not int or type(length) is not int or start < 0 or length <= 0 or start + length > duration_ticks:
            raise ValueError("A caption has an invalid or out-of-timeline time range. Correct the caption timing before export.")
        if not isinstance(text, str):
            raise ValueError("A caption must contain 1–4,000 characters before export.")
        text = text.replace("\x00", "").strip()
        if not text or len(text) > 4000:
            raise ValueError("A caption must contain 1–4,000 characters before export.")
        clean.append({**caption, "text": text})
    return sorted(clean, key=lambda row: (row["timelineStartTicks"], row["timelineDurationTicks"]))


def _bounded_ticks(value: Any, name: str, duration: int) -> int:
    """Validate editable timing before converting it to an FFmpeg expression."""
    if type(value) is not int or value < 0 or value > duration:
        raise ValueError(f"{name} must be a nonnegative integer time within the clip.")
    return value


def _validated_gain(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite audio gain in dB.")
    gain = float(value)
    if not math.isfinite(gain) or not -60 <= gain <= 24:
        raise ValueError(f"{name} must be between -60 and 24 dB.")
    return gain


def _validate_av_start_alignment(probe: dict[str, Any], frames_per_second: int) -> float:
    """Return audio-minus-video start PTS, rejecting outputs misaligned by a frame."""
    if frames_per_second <= 0:
        raise ValueError("The selected output frame rate is invalid; A/V synchronization cannot be validated.")
    streams = probe.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if video is None or audio is None:
        raise ValueError("Rendered MP4 must contain both video and audio streams to validate synchronization.")
    try:
        video_start = float(video["start_time"])
        audio_start = float(audio["start_time"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("FFprobe did not report both rendered stream start times; synchronization could not be validated.") from exc
    if not math.isfinite(video_start) or not math.isfinite(audio_start):
        raise ValueError("FFprobe returned non-finite rendered stream start times; synchronization could not be validated.")
    offset = audio_start - video_start
    if abs(offset) > 1 / frames_per_second:
        raise ValueError(f"Rendered audio/video start times differ by {offset * 1000:.1f} ms, more than one {frames_per_second} fps frame. The export was discarded.")
    return offset


def _validate_av_end_alignment(probe: dict[str, Any], frames_per_second: int) -> float:
    """Return audio-minus-video end PTS, rejecting cumulative drift over a frame."""
    if frames_per_second <= 0:
        raise ValueError("The selected output frame rate is invalid; A/V synchronization cannot be validated.")
    streams = probe.get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if video is None or audio is None:
        raise ValueError("Rendered MP4 must contain both video and audio streams to validate synchronization.")
    try:
        video_end = float(video["start_time"]) + float(video["duration"])
        audio_end = float(audio["start_time"]) + float(audio["duration"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("FFprobe did not report both rendered stream durations; end synchronization could not be validated.") from exc
    if not math.isfinite(video_end) or not math.isfinite(audio_end):
        raise ValueError("FFprobe returned non-finite rendered stream end times; synchronization could not be validated.")
    offset = audio_end - video_end
    if abs(offset) > 1 / frames_per_second:
        raise ValueError(f"Rendered audio/video end times differ by {offset * 1000:.1f} ms, more than one {frames_per_second} fps frame. The export was discarded.")
    return offset


def render_job(store: ProjectStore, ffmpeg: str | None, ffprobe: str | None, *, burn_captions: bool = False) -> JobInput:
    with _connect(store.database_path) as db:
        db.execute("BEGIN")
        project = db.execute("SELECT state_revision FROM projects WHERE id=?", (store.project_id,)).fetchone()
        timeline = db.execute("SELECT * FROM timelines WHERE project_id=? AND is_current=1", (store.project_id,)).fetchone()
        if project is None or timeline is None:
            raise ValueError("Create a timeline with at least one video clip before exporting.")
        state = store.load_state(db)
        brief = db.execute("SELECT id,revision,constraints_json FROM briefs WHERE project_id=? ORDER BY revision DESC LIMIT 1", (store.project_id,)).fetchone()
        constraints = json.loads(brief["constraints_json"]) if brief else {}
        aspect_ratio = constraints.get("aspectRatio", "source")
        if aspect_ratio not in {"source", "16:9", "9:16", "1:1", "4:3"}:
            raise ValueError("The saved brief has an unsupported aspect ratio. Review and save the brief before exporting.")
        assets = {row["id"]: row for row in db.execute("SELECT * FROM assets WHERE project_id=?", (store.project_id,))}
        clips = sorted((row for row in state["timelineItems"] if row["itemType"] == "clip" and row["trackId"] in {"video", "main_video"}), key=lambda row: (row["timelineStartTicks"], row["id"]))
        audio_items = sorted((row for row in state["timelineItems"] if row["itemType"] == "audio" and row["trackId"] in {"audio", "music"}), key=lambda row: (row["timelineStartTicks"], row["id"]))
        if not clips:
            raise ValueError("Add at least one video clip to the timeline before exporting.")
        total_ticks = max(int(clip["timelineStartTicks"]) + int(clip["timelineDurationTicks"]) for clip in clips)
        for item in audio_items:
            start, length = int(item["timelineStartTicks"]), int(item["timelineDurationTicks"])
            source_in, source_out = int(item["sourceInTicks"]), int(item["sourceOutTicks"])
            if source_out <= source_in or source_out - source_in != length or start + length > total_ticks:
                raise ValueError("Audio bed source and timeline ranges must match and fit inside the video timeline.")
        asset_snapshots = {}
        for item in [*clips, *audio_items]:
            asset = assets.get(item["assetId"])
            if asset is None:
                raise ValueError("The timeline references a missing video or audio source asset.")
            asset_snapshots[asset["id"]] = {"id": asset["id"], "sha256": asset["sha256"]}
        captions = []
        for caption in state.get("captions", []):
            timeline_item = next((item for item in state["timelineItems"] if item["id"] == caption["timelineItemId"]), None)
            if timeline_item and timeline_item["itemType"] == "caption":
                captions.append({"timelineStartTicks": timeline_item["timelineStartTicks"], "timelineDurationTicks": timeline_item["timelineDurationTicks"], "text": caption["text"], "language": caption["language"]})
        if burn_captions:
            captions = _validated_captions(captions, total_ticks)
        if burn_captions and not captions:
            raise ValueError("There are no accepted timeline captions to burn into this export. Add captions or export without burned-in captions.")
        snapshot = {"projectRevision": int(project["state_revision"]), "timelineId": timeline["id"], "timelineRevision": int(timeline["revision"]), "briefId": brief["id"] if brief else None, "briefRevision": int(brief["revision"]) if brief else None, "aspectRatio": aspect_ratio, "clips": clips, "audioItems": audio_items, "assets": sorted(asset_snapshots.values(), key=lambda item: item["id"]), "captions": captions, "burnCaptions": bool(burn_captions)}
    if not (ffmpeg if ffmpeg and Path(ffmpeg).is_file() else shutil.which("ffmpeg")):
        raise ValueError("FFmpeg is unavailable. Configure FFmpeg in the local service settings before exporting.")
    if not (ffprobe if ffprobe and Path(ffprobe).is_file() else shutil.which("ffprobe")):
        raise ValueError("FFprobe is unavailable. Export requires FFprobe to validate its output.")
    ffmpeg_executable = ffmpeg if ffmpeg and Path(ffmpeg).is_file() else shutil.which("ffmpeg")
    ffprobe_executable = ffprobe if ffprobe and Path(ffprobe).is_file() else shutil.which("ffprobe")
    enriched = {**snapshot, "ffmpegIdentity": _tool_identity(ffmpeg_executable), "ffprobeIdentity": _tool_identity(ffprobe_executable)}
    fingerprint = _render_fingerprint(enriched)
    export_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"localcut-export:{fingerprint}"))
    duration_ticks = max(int(clip["timelineStartTicks"]) + int(clip["timelineDurationTicks"]) for clip in clips)
    config = {**enriched, "exportId": export_id, "ffmpeg": ffmpeg_executable, "ffprobe": ffprobe_executable, "format": "mp4-h264-aac", "durationSeconds": duration_ticks / TICKS_PER_SECOND}
    return JobInput(str(store.root), "rendering", {"exportId": export_id, "burnCaptions": bool(burn_captions)}, fingerprint, pipeline_version=PIPELINE_VERSION, generator_id=GENERATOR_ID, generator_version=GENERATOR_VERSION, config=config)


class RenderHandler:
    kind = "rendering"
    retry_safe = True

    def __init__(self, ffmpeg: str | None = None, ffprobe: str | None = None, process: MediaProcess | None = None):
        self.ffmpeg = ffmpeg if ffmpeg and Path(ffmpeg).is_file() else shutil.which("ffmpeg")
        self.ffprobe = ffprobe if ffprobe and Path(ffprobe).is_file() else shutil.which("ffprobe")
        self.process = process or MediaProcess()

    def validate_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict) or not isinstance(payload.get("exportId"), str) or not isinstance(payload.get("burnCaptions", False), bool):
            raise ValueError("Export input requires an export ID and a caption mode.")
        try:
            uuid.UUID(payload["exportId"])
        except ValueError as exc:
            raise ValueError("Export ID must be a UUID.") from exc
        return {"exportId": payload["exportId"], "burnCaptions": payload.get("burnCaptions", False)}

    def _snapshot(self, store: ProjectStore, job: JobInput) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        config = job.config
        with _connect(store.database_path) as db:
            db.execute("BEGIN")
            project = db.execute("SELECT state_revision FROM projects WHERE id=?", (store.project_id,)).fetchone()
            timeline = db.execute("SELECT * FROM timelines WHERE id=? AND project_id=? AND is_current=1", (config.get("timelineId"), store.project_id)).fetchone()
            brief = db.execute("SELECT id,revision,constraints_json FROM briefs WHERE project_id=? ORDER BY revision DESC LIMIT 1", (store.project_id,)).fetchone()
            if project is None or int(project["state_revision"]) != config.get("projectRevision") or timeline is None or int(timeline["revision"]) != config.get("timelineRevision"):
                raise ValueError("The project changed after this export was queued. Refresh and queue a new export.")
            if (brief["id"] if brief else None) != config.get("briefId") or (int(brief["revision"]) if brief else None) != config.get("briefRevision"):
                raise ValueError("The saved brief changed after this export was queued. Queue a fresh export to use its current aspect ratio.")
            assets = {}
            for expected in config.get("assets", []):
                row = db.execute("SELECT * FROM assets WHERE id=? AND project_id=?", (expected.get("id"), store.project_id)).fetchone()
                if row is None or row["sha256"] != expected.get("sha256"):
                    raise ValueError("A source asset changed since this export was queued.")
                assets[row["id"]] = dict(row)
        return list(config.get("clips", [])), list(config.get("audioItems", [])), assets

    def validate_job(self, job: JobInput) -> None:
        if job.generator_id != GENERATOR_ID or job.generator_version != GENERATOR_VERSION or job.source_fingerprint != _render_fingerprint({key: job.config[key] for key in ("projectRevision", "timelineId", "timelineRevision", "briefId", "briefRevision", "aspectRatio", "clips", "audioItems", "assets", "captions", "burnCaptions", "ffmpegIdentity", "ffprobeIdentity")}):
            raise ValueError("Export input provenance is invalid.")
        if _tool_identity(str(job.config.get("ffmpeg", ""))) != job.config["ffmpegIdentity"] or _tool_identity(str(job.config.get("ffprobe", ""))) != job.config["ffprobeIdentity"]:
            raise ValueError("FFmpeg or FFprobe changed after this export was queued. Queue it again to validate with the installed tools.")
        if job.payload.get("exportId") != job.config.get("exportId") or job.payload.get("burnCaptions") != job.config.get("burnCaptions"):
            raise ValueError("Export settings do not match the queued job.")
        if not job.config.get("clips") or not job.config.get("assets"):
            raise ValueError("Export snapshot contains no video or source assets.")
        store = ProjectStore.open(Path(job.project_path))
        self._snapshot(store, job)

    @staticmethod
    def _seconds(ticks: int) -> str:
        return f"{ticks / TICKS_PER_SECOND:.6f}"

    def _probe(self, path: Path, context: JobContext) -> dict[str, Any]:
        if not self.ffprobe:
            raise ValueError("FFprobe is unavailable.")
        args = [self.ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe", "-show_streams", "-show_format", "-of", "json", str(path)]
        return json.loads(self.process.run(args, context, stage="validating export"))

    def run(self, context: JobContext, payload: dict[str, Any]) -> JobOutput:
        self.validate_job(context.input)
        store = ProjectStore.open(Path(context.input.project_path))
        clips, audio_items, assets = self._snapshot(store, context.input)
        rows: list[dict[str, Any]] = []
        verified_assets: set[str] = set()
        for clip in clips:
            context.check_cancelled()
            asset = assets.get(clip["assetId"])
            if asset is None:
                raise ValueError("The export snapshot references a missing source asset.")
            path = source_path(store, asset)
            if asset["id"] not in verified_assets and source_digest(path, context) != asset["sha256"]:
                raise ValueError(f"Source changed since import: {Path(asset['last_known_path']).name}. Reimport or relink it before exporting.")
            verified_assets.add(asset["id"])
            probe = self._probe(path, context)
            video = video_stream(probe)
            audio = next((stream for stream in probe["streams"] if stream["codec_type"] == "audio" and stream.get("disposition", {}).get("default")), None)
            if audio is None:
                audio = next((stream for stream in probe["streams"] if stream["codec_type"] == "audio"), None)
            rows.append({"clip": clip, "asset": asset, "path": path, "probe": probe, "video": video, "audio": audio})
        bed_rows: list[dict[str, Any]] = []
        verified_bed_assets: set[str] = set()
        for item in audio_items:
            context.check_cancelled()
            asset = assets.get(item["assetId"])
            if asset is None:
                raise ValueError("The audio bed references a missing source asset.")
            path = source_path(store, asset)
            if asset["id"] not in verified_assets and asset["id"] not in verified_bed_assets and source_digest(path, context) != asset["sha256"]:
                raise ValueError(f"Audio bed source changed since import: {Path(asset['last_known_path']).name}. Reimport it before exporting.")
            verified_bed_assets.add(asset["id"])
            probe = self._probe(path, context)
            audio = next((stream for stream in probe["streams"] if stream["codec_type"] == "audio" and stream.get("disposition", {}).get("default")), None)
            if audio is None:
                audio = next((stream for stream in probe["streams"] if stream["codec_type"] == "audio"), None)
            if audio is None:
                raise ValueError(f"Audio bed source has no audio track: {Path(asset['last_known_path']).name}.")
            bed_rows.append({"item": item, "asset": asset, "path": path, "probe": probe, "audio": audio, "inputIndex": len(rows) + len(bed_rows)})
        for previous, current in zip(rows, rows[1:]):
            previous_end = int(previous["clip"]["timelineStartTicks"]) + int(previous["clip"]["timelineDurationTicks"])
            if int(current["clip"]["timelineStartTicks"]) < previous_end:
                raise ValueError("Overlapping video clips are not supported by this export profile. Adjust the timeline and retry.")
        width, height, fps = self._output_profile(rows[0]["video"], str(context.input.config["aspectRatio"]))
        inputs: list[str] = [self.ffmpeg or "ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-v", "error", "-xerror", "-n", "-progress", "pipe:1", "-protocol_whitelist", "file,pipe", "-copyts"]
        for row in rows:
            inputs += ["-i", str(row["path"])]
        for row in bed_rows:
            inputs += ["-i", str(row["path"])]
        filters: list[str] = []
        video_labels: list[str] = []
        audio_labels: list[str] = []
        cursor = 0
        segment_count = 0
        total_ticks = max(int(row["clip"]["timelineStartTicks"]) + int(row["clip"]["timelineDurationTicks"]) for row in rows)
        # Explicit timeline gaps become black video and silence; no timeline time is silently discarded.
        for index, row in enumerate(rows):
            clip = row["clip"]
            start_at = int(clip["timelineStartTicks"])
            if start_at > cursor:
                gap = (start_at - cursor) / TICKS_PER_SECOND
                filters.append(f"color=c=black:s={width}x{height}:r={fps}:d={gap:.6f},setpts=PTS-STARTPTS[vg{segment_count}]")
                filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={gap:.6f},asetpts=PTS-STARTPTS[ag{segment_count}]")
                video_labels.append(f"[vg{segment_count}]"); audio_labels.append(f"[ag{segment_count}]")
                segment_count += 1
            source_in = int(clip["sourceInTicks"]); source_out = int(clip["sourceOutTicks"])
            duration_ticks = int(clip["timelineDurationTicks"])
            if source_out <= source_in or abs((source_out - source_in) - duration_ticks) > 1:
                raise ValueError("This export profile supports normal-speed clips whose source and timeline durations match.")
            fade_in_ticks = _bounded_ticks(clip.get("properties", {}).get("fadeInTicks", 0), "Clip audio fade in", duration_ticks)
            fade_out_ticks = _bounded_ticks(clip.get("properties", {}).get("fadeOutTicks", 0), "Clip audio fade out", duration_ticks)
            video_fade_in_ticks = _bounded_ticks(clip.get("properties", {}).get("videoFadeInTicks", 0), "Clip video fade in", duration_ticks)
            video_fade_out_ticks = _bounded_ticks(clip.get("properties", {}).get("videoFadeOutTicks", 0), "Clip video fade out", duration_ticks)
            gain = _validated_gain(clip.get("properties", {}).get("audioGainDb", 0), "Clip audio gain")
            clip_seconds = duration_ticks / TICKS_PER_SECOND
            video_index = int(row["video"]["index"])
            video_chain = f"[{index}:{video_index}]trim=start={self._seconds(source_in)}:end={self._seconds(source_out)},setpts=PTS-STARTPTS"
            crop = {"punch_in_110": 1.10, "punch_in_125": 1.25}.get(str(clip.get("properties", {}).get("cropPreset", "full_frame")))
            if crop:
                video_chain += f",crop=iw/{crop:.2f}:ih/{crop:.2f}"
            video_chain += f",scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},trim=duration={clip_seconds:.6f},setpts=PTS-STARTPTS"
            video_fade_in = min(clip_seconds / 2, video_fade_in_ticks / TICKS_PER_SECOND)
            video_fade_out = min(clip_seconds / 2, video_fade_out_ticks / TICKS_PER_SECOND)
            if video_fade_in:
                video_chain += f",fade=t=in:st=0:d={video_fade_in:.6f}:color=black"
            if video_fade_out:
                video_chain += f",fade=t=out:st={max(0, clip_seconds-video_fade_out):.6f}:d={video_fade_out:.6f}:color=black"
            video_chain += f"[v{segment_count}]"
            filters.append(video_chain)
            video_labels.append(f"[v{segment_count}]")
            audio = row["audio"]
            audio_label = f"a{segment_count}"
            if audio:
                audio_index = int(audio["index"])
                audio_chain = f"[{index}:{audio_index}]atrim=start={self._seconds(source_in)}:end={self._seconds(source_out)},asetpts=PTS-STARTPTS"
                try:
                    audio_start = float(audio.get("start_time", 0) or 0)
                except (TypeError, ValueError):
                    audio_start = 0
                audio_gap = max(0.0, audio_start - source_in / TICKS_PER_SECOND)
                if audio_gap > 0:
                    audio_chain += f",asetpts=PTS+{audio_gap:.6f}/TB,aresample=48000:async=1:first_pts=0"
                else:
                    audio_chain += ",aresample=48000"
                audio_chain += f",volume={gain:.3f}dB"
                fade_in = min(clip_seconds / 2, fade_in_ticks / TICKS_PER_SECOND)
                fade_out = min(clip_seconds / 2, fade_out_ticks / TICKS_PER_SECOND)
                if fade_in:
                    audio_chain += f",afade=t=in:d={fade_in:.6f}"
                if fade_out:
                    audio_chain += f",afade=t=out:st={max(0, clip_seconds-fade_out):.6f}:d={fade_out:.6f}"
                audio_chain += f",apad=pad_dur={clip_seconds:.6f},atrim=duration={clip_seconds:.6f},asetpts=PTS-STARTPTS[{audio_label}]"
            else:
                audio_chain = f"anullsrc=r=48000:cl=stereo,atrim=duration={clip_seconds:.6f},asetpts=PTS-STARTPTS[{audio_label}]"
            filters.append(audio_chain)
            audio_labels.append(f"[{audio_label}]")
            cursor = start_at + duration_ticks
            segment_count += 1
        filters.append("".join(video_labels + audio_labels) + f"concat=n={segment_count}:v=1:a=1[vout][aout]")
        duration = total_ticks / TICKS_PER_SECOND
        audio_map = "[aout]"
        if bed_rows:
            bed_labels: list[str] = []
            for index, row in enumerate(bed_rows):
                item = row["item"]
                source_in, source_out = int(item["sourceInTicks"]), int(item["sourceOutTicks"])
                bed_duration = int(item["timelineDurationTicks"]) / TICKS_PER_SECOND
                start = int(item["timelineStartTicks"])
                props = item.get("properties", {})
                fade_in_ticks = _bounded_ticks(props.get("fadeInTicks", 0), "Audio bed fade in", int(item["timelineDurationTicks"]))
                fade_out_ticks = _bounded_ticks(props.get("fadeOutTicks", 0), "Audio bed fade out", int(item["timelineDurationTicks"]))
                gain = _validated_gain(props.get("audioGainDb", 0), "Audio bed gain")
                bed = f"[{row['inputIndex']}:{int(row['audio']['index'])}]atrim=start={self._seconds(source_in)}:end={self._seconds(source_out)},asetpts=PTS-STARTPTS,aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,volume={gain:.3f}dB"
                fade_in = min(bed_duration / 2, fade_in_ticks / TICKS_PER_SECOND)
                fade_out = min(bed_duration / 2, fade_out_ticks / TICKS_PER_SECOND)
                if fade_in:
                    bed += f",afade=t=in:d={fade_in:.6f}"
                if fade_out:
                    bed += f",afade=t=out:st={max(0, bed_duration-fade_out):.6f}:d={fade_out:.6f}"
                if start:
                    bed += f",adelay=delays={round(start / 1000)}:all=1"
                bed += f",apad=pad_dur={duration:.6f},atrim=duration={duration:.6f}[bed{index}]"
                filters.append(bed)
                bed_labels.append(f"[bed{index}]")
            filters.append("[aout]" + "".join(bed_labels) + f"amix=inputs={len(bed_labels)+1}:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.95[amixed]")
            audio_map = "[amixed]"
        estimated = int(duration * 1_100_000 + 64 * 1024**2)
        if shutil.disk_usage(store.root).free < estimated + 256 * 1024**2:
            raise ValueError(f"Insufficient project disk space; approximately {estimated} bytes plus 256 MiB reserve required.")
        output = context.staging_file(".mp4")
        video_map = "[vout]"
        if payload["burnCaptions"]:
            caption_rows = _validated_captions(context.input.config["captions"], int(total_ticks))
            listing = context.staging_file(".ffmpeg-capabilities.txt")
            listing_bytes = self.process.run([self.ffmpeg or "ffmpeg", "-hide_banner", "-filters"], context, stage="checking subtitle renderer", stderr_capture=listing)
            available = listing_bytes.decode("utf-8", "replace") + (listing.read_text(encoding="utf-8", errors="replace") if listing.exists() else "")
            has_ass = bool(re.search(r"\bass\s+V->V\s+Render ASS subtitles", available))
            has_subtitles = bool(re.search(r"\bsubtitles\s+V->V\s+Render text subtitles", available))
            if not has_ass and not has_subtitles:
                raise ValueError("This FFmpeg build has no ASS/subtitles video filter. Install an FFmpeg build with libass support, or export without burned-in captions.")
            subtitle_file = context.stage_root / "captions.ass"
            subtitle_file.write_text(self._ass_document(caption_rows, width, height), encoding="utf-8-sig")
            filter_name = "ass" if has_ass else "subtitles"
            filters.append(f"[vout]{filter_name}=filename=captions.ass[vburn]")
            video_map = "[vburn]"
        args = inputs + ["-filter_complex", ";".join(filters), "-map", video_map, "-map", audio_map, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-t", f"{duration:.6f}", "-movflags", "+faststart", "-f", "mp4", str(output)]
        self.process.run(args, context, duration=duration, stage="rendering H.264 MP4", base=0.05, span=0.75, timeout=max(3600, duration * 20), cwd=context.stage_root if payload["burnCaptions"] else None)
        context.report(.82, "validating rendered file")
        if not output.is_file() or output.stat().st_size < 1024:
            raise ValueError("FFmpeg did not produce a usable MP4 output.")
        probe = self._probe(output, context)
        out_video = video_stream(probe)
        if out_video.get("codec_name") != "h264":
            raise ValueError("Rendered file is not H.264 video.")
        if int(out_video.get("width", 0)) != width or int(out_video.get("height", 0)) != height:
            raise ValueError("Rendered dimensions do not match the selected project aspect ratio.")
        av_start_offset = _validate_av_start_alignment(probe, fps)
        av_end_offset = _validate_av_end_alignment(probe, fps)
        out_duration = float(probe.get("format", {}).get("duration", 0) or 0)
        if not math.isfinite(out_duration) or abs(out_duration - duration) > max(1 / fps, .05):
            raise ValueError("Rendered duration does not match the accepted timeline within one output frame.")
        self.process.run([self.ffmpeg or "ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe", "-i", str(output), "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"], context, stage="checking playback decode")
        self.validate_job(context.input)
        for asset in assets.values():
            if source_digest(source_path(store, asset), context) != asset["sha256"]:
                raise ValueError("A source changed while rendering; the export was discarded.")
        context.report(.97, "committing export")
        return JobOutput(str(output), f"exports/{context.input.config['exportId']}.mp4", "export:mp4", {"exportId": context.input.config["exportId"], "timelineId": context.input.config["timelineId"], "timelineRevision": context.input.config["timelineRevision"], "projectRevision": context.input.config["projectRevision"], "briefId": context.input.config["briefId"], "briefRevision": context.input.config["briefRevision"], "aspectRatio": context.input.config["aspectRatio"], "format": "mp4-h264-aac", "durationSeconds": duration, "audioVideoStartOffsetSeconds": av_start_offset, "audioVideoStartAlignmentValidated": True, "audioVideoEndOffsetSeconds": av_end_offset, "audioVideoEndAlignmentValidated": True, "burnCaptions": bool(payload["burnCaptions"]), "startedAt": context.input.config.get("startedAt", _now())})

    @staticmethod
    def _ass_document(captions: list[dict[str, Any]], width: int, height: int) -> str:
        def stamp(ticks: int) -> str:
            centiseconds = max(0, round(ticks / 10_000)); hours, centiseconds = divmod(centiseconds, 360_000); minutes, centiseconds = divmod(centiseconds, 6_000); seconds, centiseconds = divmod(centiseconds, 100)
            return f"{hours}:{minutes:02}:{seconds:02}.{centiseconds:02}"
        lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}", "WrapStyle: 2", "ScaledBorderAndShadow: yes", "", "[V4+ Styles]", "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding", f"Style: Default,Arial,{max(24,round(height*0.052))},&H00FFFFFF,&H0000FFFF,&H80000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,40,40,40,1", "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
        for caption in sorted(captions, key=lambda row: int(row["timelineStartTicks"])):
            start = int(caption["timelineStartTicks"]); end = start + int(caption["timelineDurationTicks"])
            text = str(caption["text"]).replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\r\n", "\n").replace("\r", "\n").replace("\n", r"\N")
            lines.append(f"Dialogue: 0,{stamp(start)},{stamp(end)},Default,,0,0,0,,{text}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _output_profile(stream: dict[str, Any], aspect_ratio: str = "source") -> tuple[int, int, int]:
        width, height = int(stream["width"]), int(stream["height"])
        angle = rotation(stream)
        if aspect_ratio == "source":
            try:
                sar = Fraction(str(stream.get("sample_aspect_ratio", "1:1")).replace(":", "/"))
                display = Fraction(width, height) * sar
                if angle in {90, 270}:
                    display = 1 / display
            except (ValueError, ZeroDivisionError):
                display = Fraction(width, height)
        else:
            left, right = (int(value) for value in aspect_ratio.split(":"))
            display = Fraction(left, right)
        unit_width, unit_height = display.numerator, display.denominator
        scale = max(1, 1920 // max(unit_width, unit_height))
        width, height = unit_width * scale, unit_height * scale
        if width % 2:
            width -= 1
        if height % 2:
            height -= 1
        if width < 2 or height < 2:
            raise ValueError("The selected aspect ratio cannot be represented by a valid H.264 frame size.")
        try:
            rate = float(Fraction(str(stream.get("avg_frame_rate", "30/1"))))
        except (ValueError, ZeroDivisionError):
            rate = 30
        fps = max(1, min(60, round(rate))) if math.isfinite(rate) and rate > 0 else 30
        return width, height, fps

    def validate_output(self, output: JobOutput) -> None:
        path = Path(output.staging_path)
        if not path.is_file() or path.stat().st_size < 1024 or output.artifact_kind != "export:mp4":
            raise ValueError("Rendered MP4 output is missing or invalid.")

    def commit_output(self, connection, store: ProjectStore, job_input: JobInput, artifact_id: str, target: Path, metadata: dict[str, Any] | None = None) -> None:
        from .persistence import _sha256_file
        project = connection.execute("SELECT state_revision FROM projects WHERE id=?", (store.project_id,)).fetchone()
        timeline = connection.execute("SELECT revision FROM timelines WHERE id=? AND project_id=? AND is_current=1", (job_input.config["timelineId"], store.project_id)).fetchone()
        brief = connection.execute("SELECT id,revision FROM briefs WHERE project_id=? ORDER BY revision DESC LIMIT 1", (store.project_id,)).fetchone()
        if project is None or int(project["state_revision"]) != int(job_input.config["projectRevision"]) or timeline is None or int(timeline["revision"]) != int(job_input.config["timelineRevision"]) or (brief["id"] if brief else None) != job_input.config["briefId"] or (int(brief["revision"]) if brief else None) != job_input.config["briefRevision"]:
            raise ValueError("The project changed during export publication. The rendered file was discarded; queue a fresh export.")
        metadata = metadata or {}
        measured_offset = metadata.get("audioVideoStartOffsetSeconds")
        alignment_validated = metadata.get("audioVideoStartAlignmentValidated") is True
        if isinstance(measured_offset, bool) or not isinstance(measured_offset, (int, float)) or not math.isfinite(float(measured_offset)):
            measured_offset = None
            alignment_validated = False
        measured_end_offset = metadata.get("audioVideoEndOffsetSeconds")
        end_alignment_validated = metadata.get("audioVideoEndAlignmentValidated") is True
        if isinstance(measured_end_offset, bool) or not isinstance(measured_end_offset, (int, float)) or not math.isfinite(float(measured_end_offset)):
            measured_end_offset = None
            end_alignment_validated = False
        settings = {
            "timelineRevision": job_input.config["timelineRevision"],
            "projectRevision": job_input.config["projectRevision"],
            "briefId": job_input.config["briefId"],
            "briefRevision": job_input.config["briefRevision"],
            "aspectRatio": job_input.config["aspectRatio"],
            "burnCaptions": bool(job_input.config["burnCaptions"]),
            "durationSeconds": job_input.config.get("durationSeconds"),
            "audioVideoStartOffsetSeconds": measured_offset,
            "audioVideoStartAlignmentValidated": alignment_validated,
            "audioVideoEndOffsetSeconds": measured_end_offset,
            "audioVideoEndAlignmentValidated": end_alignment_validated,
        }
        connection.execute("INSERT INTO exports(id,project_id,timeline_id,status,relative_path,format,settings_json,started_at,completed_at,checksum_sha256,error_code) VALUES (?,?,?,'completed',?,?,?,?,?,?,NULL)", (job_input.config["exportId"], store.project_id, job_input.config["timelineId"], target.relative_to(store.root).as_posix(), "mp4-h264-aac", _json(settings), job_input.config.get("startedAt", _now()), _now(), _sha256_file(target)))


def list_exports(store: ProjectStore) -> list[dict[str, Any]]:
    with _connect(store.database_path) as db:
        return [{"id": row["id"], "timelineId": row["timeline_id"], "status": row["status"], "relativePath": row["relative_path"], "format": row["format"], "settings": json.loads(row["settings_json"]), "startedAt": row["started_at"], "completedAt": row["completed_at"], "checksumSha256": row["checksum_sha256"], "errorCode": row["error_code"]} for row in db.execute("SELECT * FROM exports WHERE project_id=? ORDER BY started_at DESC", (store.project_id,))]


def export_srt(store: ProjectStore) -> dict[str, Any]:
    """Write accepted timeline captions as a standalone, atomic SRT sidecar."""
    with _connect(store.database_path) as db:
        db.execute("BEGIN")
        state = store.load_state(db)
        timeline = db.execute("SELECT * FROM timelines WHERE project_id=? AND is_current=1", (store.project_id,)).fetchone()
        captions = []
        items = {item["id"]: item for item in state["timelineItems"]}
        for caption in state.get("captions", []):
            item = items.get(caption["timelineItemId"])
            if item is not None and item["itemType"] == "caption":
                captions.append((int(item["timelineStartTicks"]), int(item["timelineDurationTicks"]), str(caption["text"])))
        db.rollback()
    if timeline is None:
        raise ValueError("The current project has no timeline.")
    if not captions:
        raise ValueError("There are no accepted timeline captions to export.")
    def stamp(ticks: int) -> str:
        millis = max(0, round(ticks / 1000)); hours, millis = divmod(millis, 3_600_000); minutes, millis = divmod(millis, 60_000); seconds, millis = divmod(millis, 1000)
        return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"
    records = []
    for index, (start, length, text) in enumerate(sorted(captions), 1):
        safe_text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        records.append(f"{index}\n{stamp(start)} --> {stamp(start + length)}\n{safe_text}")
    from .persistence import atomic_write
    relative = f"exports/timeline-r{timeline['revision']}.srt"
    target = (store.root / relative).resolve()
    target.relative_to(store.root.resolve())
    if target.exists():
        relative = f"exports/timeline-r{timeline['revision']}-{uuid.uuid4().hex[:8]}.srt"
        target = store.root / relative
    atomic_write(target, ("\ufeff" + "\n\n".join(records) + "\n").encode("utf-8"))
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    export_id = str(uuid.uuid4())
    try:
        with _connect(store.database_path) as db:
            db.execute("INSERT INTO exports(id,project_id,timeline_id,status,relative_path,format,settings_json,started_at,completed_at,checksum_sha256,error_code) VALUES (?,?,?,'completed',?,?,?,?,?,?,NULL)", (export_id, store.project_id, timeline["id"], relative, "srt", _json({"timelineRevision": timeline["revision"], "captionCount": len(records)}), _now(), _now(), digest))
            db.commit()
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return {"relativePath": relative, "captionCount": len(records), "timelineRevision": int(timeline["revision"])}
