"""Independent, advisory local footage detectors backed by validated proxy jobs."""
from __future__ import annotations

from array import array
from difflib import SequenceMatcher
from fractions import Fraction
import hashlib
from itertools import chain, islice
import json
import math
import re
import shutil
import struct
from pathlib import Path
from statistics import median
from typing import Any
import wave

from .jobs import JobContext, JobInput, JobOutput
from .persistence import ProjectStore, _connect, _sha256_file
from .proxies import PIPELINE_VERSION as PROXY_PIPELINE_VERSION, MediaProcess, ProcessFailure, source_digest, source_path
from .transcription import ready_proxy

PIPELINE_VERSION = "analysis-2"
FRAME_SIZE = 160 * 90
ANALYSIS_STAGING_RESERVE = 16 * 1024 * 1024

# Defaults and bounds are intentionally conservative and adjustable per run.
DETECTORS: dict[str, dict[str, Any]] = {
    "silence": {"name": "Silence candidates", "version": "4", "settings": {"thresholdDbfs": [-38, -80, -12], "hysteresisDb": [4, 0, 12], "minimumMs": [400, 100, 5000], "mergeGapMs": [120, 0, 1000]}, "needsAudio": True},
    "audio_levels": {"name": "Audio levels", "version": "2", "settings": {"windowMs": [250, 50, 2000]}, "needsAudio": True},
    "speech_regions": {"name": "Transcript speech regions", "version": "1", "settings": {"mergeGapMs": [700, 0, 3000]}, "needsTranscript": True},
    "filler_words": {"name": "Possible filler words", "version": "1", "settings": {"mergeGapMs": [250, 0, 1500]}, "needsTranscript": True},
    "shot_boundaries": {"name": "Shot boundary candidates", "version": "4", "settings": {"sampleFps": [2, 1, 4], "changeThreshold": [0.22, 0.05, 0.8], "minimumSeparationSeconds": [1.0, 0.25, 10]}, "needsVideo": True},
    "near_black": {"name": "Near-black segments", "version": "3", "settings": {"lumaThreshold": [20, 1, 80], "darkFraction": [0.96, 0.5, 1], "minimumDurationSeconds": [0.8, 0.2, 10], "sampleFps": [1, 0.5, 4]}, "needsVideo": True},
    "blur": {"name": "Blur signals", "version": "3", "settings": {"laplacianVarianceThreshold": [28, 1, 300], "minimumDurationSeconds": [1.5, 0.2, 10], "sampleFps": [1, 0.5, 4]}, "needsVideo": True},
    "thumbnails": {"name": "Representative thumbnails", "version": "2", "settings": {"count": [5, 1, 12]}, "needsVideo": True},
    "motion": {"name": "Motion and activity", "version": "2", "settings": {"sampleFps": [1, 0.25, 4], "windowSeconds": [5, 1, 30]}, "needsVideo": True},
    "repeated_takes": {"name": "Possible repeated takes", "version": "5", "settings": {"similarityThreshold": [0.78, 0.5, 1], "minimumSeparationSeconds": [3, 0.5, 120]}, "needsTranscript": True},
}


def _settings(detector: str, values: Any) -> dict[str, float | int]:
    if detector not in DETECTORS or not isinstance(values, dict):
        raise ValueError("Choose a supported detector and provide an object of settings.")
    unknown = set(values) - set(DETECTORS[detector]["settings"])
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise ValueError(f"Unknown setting(s) for {detector}: {names}.")
    result: dict[str, float | int] = {}
    for name, (default, low, high) in DETECTORS[detector]["settings"].items():
        value = values.get(name, default)
        if isinstance(default, int) and isinstance(low, int) and isinstance(high, int):
            if type(value) is not int: raise ValueError(f"{name} must be an integer.")
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number.")
        if not low <= value <= high: raise ValueError(f"{name} must be from {low} to {high}.")
        result[name] = value
    return result


def catalog() -> list[dict[str, Any]]:
    return [{"id": key, **value} for key, value in DETECTORS.items()]


def _transcript_review_fingerprint(transcript: dict[str, Any]) -> str:
    review=json.dumps({"segments":transcript.get("segments",[]),"words":transcript.get("words",[]),"markers":transcript.get("markers",[])},ensure_ascii=False,sort_keys=True,separators=(",",":"))
    return hashlib.sha256(review.encode("utf-8")).hexdigest()


def analysis_job(store: ProjectStore, asset_id: str, detector: str, settings: dict[str, Any]) -> JobInput:
    clean = _settings(detector, settings)
    with _connect(store.database_path) as db:
        asset = db.execute("SELECT * FROM assets WHERE id=? AND project_id=?", (asset_id, store.project_id)).fetchone()
        if asset is None: raise ValueError("Imported source asset was not found.")
        proxy = db.execute("SELECT * FROM artifacts WHERE asset_id=? AND kind='proxy' AND status='ready' AND source_fingerprint=? ORDER BY created_at DESC LIMIT 1", (asset_id, asset["sha256"])).fetchone()
        if proxy is None: raise ValueError("Generate preview assets before running footage analysis.")
        transcript = db.execute("SELECT t.id,a.id artifact_id,a.checksum_sha256 FROM transcripts t JOIN artifacts a ON a.id=t.artifact_id WHERE t.asset_id=? AND t.source_fingerprint=? AND a.status='ready' ORDER BY t.created_at DESC LIMIT 1", (asset_id, asset["sha256"])).fetchone() if DETECTORS[detector].get("needsTranscript") else None
        if DETECTORS[detector].get("needsTranscript") and transcript is None: raise ValueError("Create a local transcript before running this detector.")
    extra = {"proxyArtifactId": proxy["id"], "proxyChecksum": proxy["checksum_sha256"]}
    proxy_bundle = (store.root / proxy["relative_path"]).resolve(); proxy_bundle.relative_to(store.root.resolve())
    try:
        proxy_manifest = json.loads((proxy_bundle / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Preview assets are missing or invalid; regenerate them before analysis.") from exc
    if proxy_manifest.get("pipelineVersion") != PROXY_PIPELINE_VERSION:
        raise ValueError("Preview assets use an older timestamp map; regenerate them before analysis.")
    if transcript:
        extra.update({"transcriptId": transcript["id"], "transcriptArtifactId": transcript["artifact_id"], "transcriptChecksum": transcript["checksum_sha256"], "transcriptReviewFingerprint": _transcript_review_fingerprint(store.get_transcript(transcript["id"]))})
    config = {"detector": detector, "settings": clean, **extra}
    return JobInput(str(store.root), "analysis", {"assetId": asset_id, "detector": detector, "settings": clean}, asset["sha256"], pipeline_version=PIPELINE_VERSION, asset_id=asset_id, generator_id=f"localcut-{detector}", generator_version=DETECTORS[detector]["version"], config=config)


def _source_ticks(proxy_seconds: Fraction, offset: Fraction, duration: int) -> int:
    return min(duration, max(0, round((proxy_seconds + offset) * 1_000_000)))


def _bounded_text_similarity(left_tokens: list[str], right_tokens: list[str], minimum: float) -> float | None:
    """Skip full sequence matching when a cheap upper bound cannot pass."""
    matcher=SequenceMatcher(None,left_tokens,right_tokens,autojunk=False)
    if matcher.quick_ratio()<minimum:
        return None
    return matcher.ratio()


_FILLER_TOKENS = {"um", "uh", "erm", "er"}


def _filler_word(token: str) -> str | None:
    normalized = re.sub(r"^\W+|\W+$", "", token.casefold(), flags=re.UNICODE)
    return normalized if normalized in _FILLER_TOKENS else None


def _preflight_frame_staging(context: JobContext, duration_ticks: int, sample_fps: float) -> tuple[int, int]:
    """Estimate the bounded-size grayscale frame file before decoding begins."""
    samples = math.ceil((duration_ticks / 1_000_000) * sample_fps) + 2
    frame_bytes = samples * FRAME_SIZE
    required = frame_bytes + max(ANALYSIS_STAGING_RESERVE, frame_bytes // 4)
    staging = context.staging_file(".space-check").parent
    free = shutil.disk_usage(staging).free
    if free < required:
        needed_gb = required / (1024 ** 3)
        free_gb = free / (1024 ** 3)
        raise ValueError(f"This video detector needs about {needed_gb:.2f} GB of free staging space; only {free_gb:.2f} GB is available. Free space or choose a lower sample rate.")
    return required, free


def _item(asset_id: str, detector: str, start: int, end: int, explanation: str, *, confidence: float | None = None, signal: float | None = None, **details: Any) -> dict[str, Any]:
    return {"assetId": asset_id, "sourceInTicks": start, "sourceOutTicks": end, "detectorId": detector, "detectorName": DETECTORS[detector]["name"], "detectorVersion": DETECTORS[detector]["version"], "confidence": confidence, "signalStrength": signal, "explanation": explanation, "suggestionOnly": True, **details}


def _frames(proxy: Path, ffmpeg: str, context: JobContext, rate: float, duration_seconds: float, process: MediaProcess | None = None):
    # Analyze a full-image normalized grid without synthetic aspect-ratio bars.
    # showinfo describes the sampled grid PTS, not exact originating VFR PTS.
    log = context.staging_file(".showinfo.log")
    raw_path = context.staging_file(".frames.gray")
    vf = f"fps={rate:g},scale=160:90,setsar=1,format=gray,showinfo"
    args = [ffmpeg, "-hide_banner", "-nostdin", "-v", "info", "-protocol_whitelist", "file,pipe", "-i", str(proxy), "-map", "0:v:0", "-an", "-vf", vf, "-fps_mode", "vfr", "-progress", "pipe:1", "-nostats", "-f", "rawvideo", "-pix_fmt", "gray", str(raw_path)]
    runner = process or MediaProcess()
    try:
        runner.run(args, context, duration=max(duration_seconds, 0.001), stage="sampling proxy frames", base=0.12, span=0.38, stderr_capture=log, stdout_file=raw_path, stdout_progress_only=True)
    except TypeError as exc:
        # Compatibility for small injected test/process adapters that implement
        # the original byte-returning interface; production always streams.
        if not any(option in str(exc) for option in ("stdout_file", "stdout_progress_only")): raise
        fallback_args=args.copy()
        progress_index=fallback_args.index("-progress")
        del fallback_args[progress_index:progress_index+2]
        fallback_args[-1]="pipe:1"
        raw = runner.run(fallback_args, context, stage="sampling proxy frames", stderr_capture=log)
        raw_path.write_bytes(raw)
    text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    times = [Fraction(value) for value in re.findall(r"pts_time:([-+0-9.eE]+)", text)]
    if raw_path.stat().st_size != len(times) * FRAME_SIZE: raise ProcessFailure("FFmpeg returned incomplete or untimestamped analysis frames.")
    with raw_path.open("rb") as frames:
        for timestamp in times:
            context.check_cancelled()
            frame = frames.read(FRAME_SIZE)
            if len(frame) != FRAME_SIZE: raise ProcessFailure("FFmpeg returned an incomplete analysis frame.")
            yield timestamp, frame


def _audio_windows(wav_path: Path, context: JobContext, window_ms: int):
    with wave.open(str(wav_path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() <= 0: raise ValueError("Proxy analysis audio has an unsupported PCM format.")
        rate, samples = wav.getframerate(), wav.getnframes()
        size = max(1, rate * window_ms // 1000)
        idx = 0
        while block := wav.readframes(size):
            context.check_cancelled()
            vals = array("h"); vals.frombytes(block)
            if struct.pack("=h", 1) != struct.pack("<h", 1): vals.byteswap()
            peak = max((abs(x) for x in vals), default=0) / 32768
            rms = math.sqrt(sum((x / 32768) ** 2 for x in vals) / max(1, len(vals)))
            yield idx / rate, min(samples, idx + len(vals)) / rate, (20 * math.log10(max(rms, 1e-9))), (20 * math.log10(max(peak, 1e-9)))
            idx += len(vals)


class AnalysisHandler:
    kind = "analysis"
    retry_safe = True

    def __init__(self, ffmpeg: str | None = None, process: MediaProcess | None = None):
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg")
        self.process = process or MediaProcess()

    def validate_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict) or not isinstance(payload.get("assetId"), str): raise ValueError("Analysis requires an assetId.")
        detector = payload.get("detector")
        return {"assetId": payload["assetId"], "detector": detector, "settings": _settings(detector, payload.get("settings", {}))}

    def validate_job(self, job: JobInput, context: JobContext | None = None) -> None:
        check_cancelled = context.check_cancelled if context else None
        if job.asset_id != job.payload.get("assetId") or job.generator_id != f"localcut-{job.payload.get('detector')}" or job.generator_version != DETECTORS.get(job.payload.get("detector"), {}).get("version"):
            raise ValueError("Analysis detector provenance does not match its input.")
        if job.config.get("detector") != job.payload.get("detector") or job.config.get("settings") != job.payload.get("settings"):
            raise ValueError("Analysis cache configuration does not match its detector settings.")
        store = ProjectStore.open(Path(job.project_path))
        with _connect(store.database_path) as db:
            row = db.execute("SELECT * FROM assets WHERE id=? AND project_id=?", (job.payload["assetId"], store.project_id)).fetchone()
            proxy = db.execute("SELECT * FROM artifacts WHERE id=? AND project_id=? AND asset_id=? AND status='ready'", (job.config.get("proxyArtifactId"), store.project_id, job.payload["assetId"])).fetchone()
            transcript = db.execute("SELECT a.* FROM artifacts a JOIN transcripts t ON t.artifact_id=a.id WHERE a.id=? AND a.project_id=? AND a.asset_id=? AND a.status='ready' AND t.id=? AND t.source_fingerprint=?", (job.config.get("transcriptArtifactId"), store.project_id, job.payload["assetId"], job.config.get("transcriptId"), job.source_fingerprint)).fetchone() if job.config.get("transcriptArtifactId") else None
        if row is None or row["sha256"] != job.source_fingerprint: raise ValueError("Analysis source fingerprint no longer matches this asset.")
        if source_digest(source_path(store, row), context) != job.source_fingerprint: raise ValueError("Source changed since import; reimport before analysis.")
        if proxy is None or proxy["checksum_sha256"] != job.config.get("proxyChecksum"): raise ValueError("The preview asset changed; resubmit this detector.")
        proxy_path = (store.root / proxy["relative_path"]).resolve(); proxy_path.relative_to(store.root.resolve())
        if _sha256_file(proxy_path, check_cancelled) != proxy["checksum_sha256"]: raise ValueError("Preview assets failed checksum validation; regenerate them before analysis.")
        if job.config.get("transcriptArtifactId"):
            if transcript is None or transcript["checksum_sha256"] != job.config.get("transcriptChecksum"): raise ValueError("The transcript changed; resubmit this detector.")
            transcript_path = (store.root / transcript["relative_path"]).resolve(); transcript_path.relative_to(store.root.resolve())
            if _sha256_file(transcript_path, check_cancelled) != transcript["checksum_sha256"]: raise ValueError("Transcript artifact failed checksum validation; transcribe again before analysis.")
            if _transcript_review_fingerprint(store.get_transcript(str(job.config["transcriptId"]))) != job.config.get("transcriptReviewFingerprint"): raise ValueError("Transcript review text or markers changed; resubmit this detector.")

    def run(self, context: JobContext, payload: dict[str, Any]) -> JobOutput:
        context.check_cancelled()
        self.validate_job(context.input, context)
        store = ProjectStore.open(Path(context.input.project_path)); detector = payload["detector"]; settings = payload["settings"]
        context.report(0.02, "preparing detector", f"Preparing {DETECTORS[detector]['name']}.")
        with _connect(store.database_path) as db:
            asset = db.execute("SELECT * FROM assets WHERE id=? AND project_id=?", (payload["assetId"], store.project_id)).fetchone()
            proxy_row = db.execute("SELECT * FROM artifacts WHERE id=?", (context.input.config["proxyArtifactId"],)).fetchone()
        bundle = (store.root / proxy_row["relative_path"]).resolve(); bundle.relative_to(store.root.resolve())
        if _sha256_file(bundle, context.check_cancelled) != proxy_row["checksum_sha256"]: raise ValueError("Preview assets failed checksum validation; regenerate them before analysis.")
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8")); mapping = json.loads((bundle / "mapping.json").read_text(encoding="utf-8"))
        duration = int(asset["duration_ticks"] or 0)
        if duration <= 0: raise ValueError("Asset duration is unavailable; regenerate preview metadata.")
        offset = Fraction(mapping["sourceMinusProxySeconds"])
        source_start = Fraction(mapping.get("sourceStartSeconds", "0"))
        results: list[dict[str, Any]] = []
        warnings: list[dict[str, str]] = []
        audio = manifest.get("analysis")
        thumbnails = manifest.get("thumbnails", [])

        def add(start: Fraction, end: Fraction, explanation: str, **kwargs):
            a, b = _source_ticks(start, offset - source_start, duration), _source_ticks(end, offset - source_start, duration)
            if b > a: results.append(_item(payload["assetId"], detector, a, b, explanation, **kwargs))

        def add_source(start: Fraction, end: Fraction, explanation: str, **kwargs):
            a, b = _source_ticks(start, Fraction(0), duration), _source_ticks(end, Fraction(0), duration)
            if b > a: results.append(_item(payload["assetId"], detector, a, b, explanation, **kwargs))

        if detector in {"silence", "audio_levels"}:
            if not audio: raise ValueError("This asset has no analysis audio track.")
            context.report(0.12, "measuring audio", "Reading validated proxy PCM windows.")
            # Audio PCM begins at the audio stream's absolute PTS origin. Store
            # source-relative time from the first video frame, as the timeline does.
            start_origin = Fraction(audio.get("sourceStartSeconds", "0")) - source_start
            wav_path = (bundle / audio["path"]).resolve(); wav_path.relative_to(bundle)
            threshold = float(settings.get("thresholdDbfs", -38)); hysteresis = float(settings.get("hysteresisDb", 4)); exit_threshold = threshold + hysteresis; minimum = int(settings.get("minimumMs", 400)) / 1000; merge = int(settings.get("mergeGapMs", 120)) / 1000
            # Keep audio analysis bounded in memory for long recordings. Video
            # samples and PCM windows are both streamed from disk.
            windows = _audio_windows(wav_path, context, int(settings.get("windowMs", 250)))
            if detector == "audio_levels":
                # Bound result rows while retaining source-window time; each second is an independent cache suggestion.
                stride = max(1, round(1000 / int(settings.get("windowMs", 250))))
                while group := list(islice(windows, stride)):
                    a, _first_end, _rms, _peak = group[0]
                    b = group[-1][1]
                    db_rms = 10 * math.log10(max(1e-18, sum(10 ** (x[2] / 10) for x in group) / len(group))); db_peak = max(x[3] for x in group)
                    aggregation_ms = max(1, round((b - a) * 1000))
                    measurement_ms = int(settings.get("windowMs", 250))
                    add_source(start_origin + Fraction(str(a)), start_origin + Fraction(str(b)), f"Measured mono PCM in {measurement_ms} ms windows aggregated over approximately {aggregation_ms} ms: {db_rms:.1f} dBFS RMS and {db_peak:.1f} dBFS peak. This is a level measurement, not a speech or quality judgment.", signal=max(0, min(1, (db_peak + 80) / 80)), rmsDbfs=db_rms, peakDbfs=db_peak, measurement="mono PCM16", windowMs=measurement_ms, aggregationWindowMs=aggregation_ms)
            else:
                # Hold only the current candidate while bridging short loud
                # gaps. This keeps memory constant and retains the prior
                # merge-gap behavior.
                active_start = active_end = None
                quiet_power_seconds = quiet_duration = 0.0
                quietest_window_dbfs = loudest_window_dbfs = None

                def emit_silence() -> None:
                    nonlocal active_start, active_end, quiet_power_seconds, quiet_duration
                    nonlocal quietest_window_dbfs, loudest_window_dbfs
                    if active_start is not None and quiet_duration >= minimum and quiet_duration > 0:
                        # Avoid presenting the implementation's numerical floor
                        # for digital silence as a precise acoustic level.
                        mean_dbfs = max(-120.0, 10 * math.log10(max(1e-18, quiet_power_seconds / quiet_duration)))
                        margin_db = max(0.0, threshold - mean_dbfs)
                        signal = min(1.0, margin_db / 24.0)
                        add_source(
                            start_origin + Fraction(str(active_start)),
                            start_origin + Fraction(str(active_end)),
                            f"Quiet PCM windows at or below the {threshold:g} dBFS entry threshold averaged {mean_dbfs:.1f} dBFS RMS, {margin_db:.1f} dB below threshold, for at least {minimum:g} seconds. Once entered, the candidate stayed active through levels up to {exit_threshold:g} dBFS and configured short gaps to reduce threshold flicker. Measurements use 250 ms windows. Boundary timing is window-quantized. Quiet speech, breaths, and useful room tone may also appear quiet.",
                            signal=signal,
                            thresholdDbfs=threshold,
                            hysteresisDb=hysteresis,
                            exitThresholdDbfs=exit_threshold,
                            minimumDurationSeconds=minimum,
                            quietDurationSeconds=quiet_duration,
                            measurementWindowMs=250,
                            meanQuietWindowRmsDbfs=mean_dbfs,
                            quietestWindowRmsDbfs=quietest_window_dbfs,
                            loudestWindowRmsDbfs=loudest_window_dbfs,
                            thresholdMarginDb=margin_db,
                        )
                    active_start = active_end = None
                    quiet_power_seconds = quiet_duration = 0.0
                    quietest_window_dbfs = loudest_window_dbfs = None

                for a, b, rms, _peak in windows:
                    # Decide whether a new window lies beyond the configured
                    # bridge gap before extending the active quiet candidate.
                    if active_start is not None and a - active_end > merge:
                        emit_silence()
                    if active_start is None:
                        if rms > threshold:
                            continue
                        active_start = a
                        active_end = b
                    elif rms <= exit_threshold:
                        active_end = b
                    else:
                        continue
                    if rms <= threshold:
                        window_duration = b - a
                        quiet_power_seconds += 10 ** (rms / 10) * window_duration
                        quiet_duration += window_duration
                        quietest_window_dbfs = rms if quietest_window_dbfs is None else min(quietest_window_dbfs, rms)
                        loudest_window_dbfs = rms if loudest_window_dbfs is None else max(loudest_window_dbfs, rms)
                emit_silence()

        elif detector in {"speech_regions", "filler_words", "repeated_takes"}:
            context.report(0.2, "reading transcript", "Using local transcript word timestamps.")
            with _connect(store.database_path) as db:
                transcript = db.execute("SELECT id FROM transcripts WHERE id=? AND asset_id=?", (context.input.config["transcriptId"], payload["assetId"])).fetchone()
            if not transcript: raise ValueError("The referenced transcript is no longer available.")
            words = store.get_transcript(transcript["id"])["words"]
            if detector == "speech_regions":
                gap = int(settings["mergeGapMs"]) * 1000
                ranges=[]
                for word in words:
                    context.check_cancelled()
                    a,b=int(word["sourceInTicks"]),int(word["sourceOutTicks"])
                    if ranges and a-ranges[-1][1] <= gap: ranges[-1][1]=max(ranges[-1][1],b); ranges[-1][2].append(word)
                    else: ranges.append([a,b,[word]])
                for a,b,group in ranges:
                    context.check_cancelled()
                    cs=[w.get("confidence") for w in group if w.get("confidence") is not None]
                    confidence=sum(cs)/len(cs) if cs else None
                    word_rate=len(group)/max((b-a)/1_000_000,0.001)
                    results.append(_item(payload["assetId"], detector, a, b, "Possible speech region from local transcript word timestamps. Signal strength reflects transcript word density, not calibrated acoustic confidence. ASR can miss words or mis-time speech; review against playback.", confidence=confidence, signal=min(1.0,word_rate/4.0), transcriptId=transcript["id"], wordCount=len(group), wordRatePerSecond=word_rate))
            elif detector == "filler_words":
                gap = int(settings["mergeGapMs"]) * 1000
                groups=[]
                last_fill_index = -2
                for index, word in enumerate(words):
                    context.check_cancelled()
                    text = str(word.get("text", word.get("originalText", "")))
                    token = _filler_word(text)
                    if token is None:
                        continue
                    start, end = int(word["sourceInTicks"]), int(word["sourceOutTicks"])
                    if groups and index == last_fill_index + 1 and start - groups[-1]["end"] <= gap:
                        groups[-1]["end"] = max(groups[-1]["end"], end)
                        groups[-1]["matches"].append((word, token))
                    else:
                        groups.append({"start": start, "end": end, "matches": [(word, token)]})
                    last_fill_index = index
                for group in groups:
                    matches = group["matches"]
                    confidences = [float(word["confidence"]) for word, _ in matches if word.get("confidence") is not None]
                    confidence = sum(confidences) / len(confidences) if confidences else None
                    terms = [token for _, token in matches]
                    rendered = " ".join(str(word.get("text", word.get("originalText", ""))) for word, _ in matches)
                    results.append(_item(payload["assetId"], detector, group["start"], group["end"], "Matched a common filler token in local transcript word timestamps. ASR may misrecognize words, and hesitations can be intentional; listen before deciding whether to edit.", confidence=confidence, signal=1.0, transcriptId=transcript["id"], terms=terms, text=rendered, wordCount=len(matches)))
            else:
                groups=[]; current=[]
                for word in words:
                    context.check_cancelled()
                    if current and int(word["sourceInTicks"])-int(current[-1]["sourceOutTicks"])>900_000: groups.append(current); current=[]
                    current.append(word)
                if current: groups.append(current)
                # Keep matching bounded even for a single unusually long run-on
                # transcript segment. SequenceMatcher can otherwise allocate
                # large internal structures despite the pair-count ceiling.
                max_passage_words = 500
                candidates=[g for g in groups if 4 <= len(g) <= max_passage_words]
                skipped_long_passages = sum(1 for group in groups if len(group) > max_passage_words)
                threshold=float(settings["similarityThreshold"]); min_gap=int(float(settings["minimumSeparationSeconds"])*1_000_000); comparisons=0; search_limited=False
                for i,left in enumerate(candidates):
                    for right in candidates[i+1:]:
                        context.check_cancelled()
                        if comparisons>=2000:
                            search_limited=True
                            break
                        comparisons+=1
                        a,b=int(left[0]["sourceInTicks"]),int(left[-1]["sourceOutTicks"]); c,d=int(right[0]["sourceInTicks"]),int(right[-1]["sourceOutTicks"])
                        if c-b<min_gap: continue
                        lt=" ".join(w.get("text",w.get("originalText","")) for w in left).casefold(); rt=" ".join(w.get("text",w.get("originalText","")) for w in right).casefold()
                        left_tokens=lt.split(); right_tokens=rt.split()
                        if len(left_tokens)<4 or len(right_tokens)<4: continue
                        dur=1-min(1,abs((b-a)-(d-c))/max(b-a,d-c,1)); minimum_text=max(0,(threshold-.2*dur)/.8)
                        similarity=_bounded_text_similarity(left_tokens,right_tokens,minimum_text)
                        if similarity is None: continue
                        score=.8*similarity+.2*dur
                        if score>=threshold: results.append(_item(payload["assetId"],detector,a,b,"Reviewed transcript text and passage duration are similar to another separated region. Common short phrases are skipped. This may still be a repeated take, repeated phrasing, or coincidental similarity; review both passages.",confidence=None,signal=score,text=lt,matchedSourceInTicks=c,matchedSourceOutTicks=d,matchedText=rt,textSimilarity=similarity,durationSimilarity=dur))
                    if search_limited: break
                if search_limited:
                    warnings.append({"code":"repeated_take_comparison_limit","message":"Repeated-take comparison stopped at 2,000 passage pairs. This result set may be incomplete; shorten the transcript or analyze shorter ranges for a more complete search."})
                if skipped_long_passages:
                    warnings.append({"code":"repeated_take_passage_limit","message":f"Skipped {skipped_long_passages} transcript passage(s) longer than {max_passage_words} words to keep local matching responsive. Results may be incomplete; split long passages or analyze shorter ranges."})

        elif detector == "thumbnails":
            context.report(0.2, "selecting frames", "Selecting evenly spaced validated proxy thumbnails.")
            count=int(settings["count"]); selected=sorted({round(i*(len(thumbnails)-1)/max(1,count-1)) for i in range(min(count,len(thumbnails)))}) if thumbnails else []
            for n in selected:
                context.check_cancelled()
                thumb=thumbnails[n]; rel=str(thumb["path"]); path=(bundle/rel).resolve(); path.relative_to(bundle.resolve())
                pts=Fraction(thumb["sourcePts"])*Fraction(mapping["sourceTimebase"])-source_start
                ticks=min(max(0,duration-1),max(0,round(pts*1_000_000)))
                results.append(_item(payload["assetId"],detector,ticks,min(duration,ticks+1),"Exact frame sample at this source timestamp. The signal records successful deterministic sampling; it is not a semantic content score.",confidence=None,signal=1.0,sampleMethod="uniform_timestamp",sampleIsPoint=True,thumbnailPath=(bundle/rel).relative_to(store.root).as_posix(),thumbnailArtifactId=proxy_row["id"]))

        else:
            if not self.ffmpeg: raise ProcessFailure("FFmpeg is unavailable for video analysis.")
            estimated_bytes, free_bytes = _preflight_frame_staging(context, duration, float(settings.get("sampleFps",1)))
            context.report(0.08,"checking staging space",f"Estimated temporary frame data {estimated_bytes / (1024 ** 2):.1f} MiB · {free_bytes / (1024 ** 3):.1f} GiB free.")
            context.report(0.12, "sampling video", "Decoding small grayscale frames from the validated proxy.")
            rate=float(settings.get("sampleFps",1)); frames=iter(_frames(bundle/manifest["proxy"],self.ffmpeg,context,rate,duration/1_000_000,self.process))
            first = next(frames, None)
            if first is None: raise ValueError("No decodable proxy frames were available.")
            context.report(0.5, "analyzing frames", "Streaming timestamped proxy samples with bounded frame memory.")
            if detector == "shot_boundaries":
                threshold=float(settings["changeThreshold"]); separation=float(settings["minimumSeparationSeconds"]); last=-1e9; candidates=[]
                previous = first
                for current in frames:
                    ta,a=previous; tb,b=current
                    diff=sum(abs(x-y) for x,y in zip(a,b))/(255*FRAME_SIZE)
                    if diff>=threshold and float(tb-ta)>=0 and float(tb)>=last+separation:
                        candidates.append((ta,tb,diff)); last=float(tb)
                    previous=current
                for before,after,score in candidates:
                    # The cut can fall anywhere between adjacent samples.
                    relative_offset=offset-source_start
                    st=_source_ticks(before,relative_offset,duration); en=_source_ticks(after,relative_offset,duration)
                    results.append(_item(payload["assetId"],detector,st,en,"Large frame-luma change between adjacent sampled proxy frames. The exact cut may be anywhere in this interval. Motion, flashes, screen changes, or camera shake can look like a cut.",confidence=None,signal=min(1,score),changeFraction=score,samplingUncertaintySeconds=float(after-before)))
            elif detector == "near_black":
                min_d=float(settings["minimumDurationSeconds"]); threshold=int(settings["lumaThreshold"]); fraction=float(settings["darkFraction"]); active=[]
                def emit_dark(items):
                    if items and float(items[-1][0]-items[0][0])+1/rate>=min_d:
                        strength=sum(v for _,v in items)/len(items)
                        add(items[0][0],items[-1][0]+Fraction(1,1)/Fraction(str(rate)),"Most sampled pixels in the central image area were near black. Fades and intentionally dark scenes can trigger this suggestion.",signal=strength,darkPixelFraction=strength,sampleArea="central_80_percent")
                for t,frame in chain((first,), frames):
                    context.check_cancelled()
                    # Ignore persistent bars by measuring the middle 80% of the frame.
                    active_pixels=[frame[y*160+x] for y in range(9,81) for x in range(16,144)]
                    dark=sum(px<=threshold for px in active_pixels)/len(active_pixels)
                    if dark>=fraction: active.append((t,dark))
                    elif active:
                        emit_dark(active)
                        active=[]
                emit_dark(active)
            elif detector == "blur":
                threshold=float(settings["laplacianVarianceThreshold"]); min_d=float(settings["minimumDurationSeconds"]); group=[]
                sample_step=Fraction(1,1)/Fraction(str(rate))
                def emit_blur(items):
                    if len(items)>1 and float(items[-1][0]-items[0][0])+float(sample_step)>=min_d:
                        values=[value for _,value in items]
                        typical=median(values)
                        strength=1-min(1,typical/max(threshold,1))
                        add(items[0][0],items[-1][0]+sample_step,"Median sampled frame edge energy was low over this interval. This resolution- and image-content-dependent signal can reflect defocus, motion blur, low detail, or smooth backgrounds; it is not a probability of blur.",signal=strength,laplacianVariance=typical,sampleCount=len(values),minimumLaplacianVariance=min(values),maximumLaplacianVariance=max(values))
                for t,frame in chain((first,), frames):
                    context.check_cancelled()
                    pixels=list(frame); lap=[]
                    for y in range(1,89):
                        row=y*160
                        for x in range(1,159):
                            k=row+x; lap.append(4*pixels[k]-pixels[k-1]-pixels[k+1]-pixels[k-160]-pixels[k+160])
                    mean=sum(lap)/len(lap); var=sum((v-mean)**2 for v in lap)/len(lap)
                    if var<threshold: group.append((t,var))
                    else: emit_blur(group); group=[]
                emit_blur(group)
            elif detector == "motion":
                window=float(settings["windowSeconds"]); bucket=[]
                previous = first
                for current in frames:
                    ta,a=previous; tb,b=current
                    context.check_cancelled()
                    diff=sum(abs(x-y) for x,y in zip(a,b))/(255*FRAME_SIZE); bucket.append((ta,diff))
                    if float(tb-bucket[0][0])>=window:
                        level=sum(x[1] for x in bucket)/len(bucket); add(bucket[0][0],tb,"Average frame-to-frame pixel change in the sampled proxy; camera movement and cuts also increase activity.",signal=min(1,level*4),meanFrameChange=level); bucket=[]
                    previous=current
                if bucket:
                    level=sum(x[1] for x in bucket)/len(bucket); add(bucket[0][0],current[0],"Average frame-to-frame pixel change in the sampled proxy; camera movement and cuts also increase activity.",signal=min(1,level*4),meanFrameChange=level)

        if detector in {"shot_boundaries", "near_black", "blur", "motion"}:
            for result in results:
                context.check_cancelled()
                result["sampleIntervalSeconds"] = 1 / float(settings["sampleFps"])
                result["timingMethod"] = "proxy_sample_grid"
                result["explanation"] += " Timing follows the sampled proxy grid; ranges are cadence-limited estimates, including for variable-frame-rate footage. Image measurements use a normalized 160×90 grid; blur thresholds depend on resolution and aspect ratio."

        context.report(.9,"validating analysis results")
        if source_digest(source_path(store,asset),context)!=context.input.source_fingerprint: raise ValueError("Source changed during analysis; discarding results.")
        self.validate_job(context.input, context)
        output=context.staging_file(".json"); output.write_text(json.dumps({"schemaVersion":1,"assetId":payload["assetId"],"detectorId":detector,"detectorVersion":DETECTORS[detector]["version"],"sourceFingerprint":context.input.source_fingerprint,"sourceDurationTicks":duration,"config":settings,"warnings":warnings,"results":results},ensure_ascii=False),encoding="utf-8")
        return JobOutput(str(output),f"cache/analysis/{detector}/{context.job_id}.json",f"analysis:{detector}",{"assetId":payload["assetId"],"detectorId":detector,"detectorVersion":DETECTORS[detector]["version"],"config":settings,"proxyArtifactId":context.input.config["proxyArtifactId"],"transcriptArtifactId":context.input.config.get("transcriptArtifactId")})

    def validate_output(self, output: JobOutput) -> None:
        data=json.loads(Path(output.staging_path).read_text(encoding="utf-8"))
        if data.get("detectorId") not in DETECTORS or not isinstance(data.get("results"),list): raise ValueError("Detector output is invalid.")
        if not isinstance(data.get("assetId"),str) or not re.fullmatch(r"[0-9a-f]{64}",str(data.get("sourceFingerprint",""))) or type(data.get("sourceDurationTicks")) is not int or data["sourceDurationTicks"]<=0 or data.get("detectorVersion") != DETECTORS[data["detectorId"]]["version"]: raise ValueError("Detector output is missing source or version provenance.")
        for row in data["results"]:
            if row.get("assetId") != data["assetId"] or row.get("detectorId") != data["detectorId"] or row.get("detectorName") != DETECTORS[data["detectorId"]]["name"] or row.get("detectorVersion") != data["detectorVersion"] or not isinstance(row.get("explanation"),str) or not row["explanation"].strip(): raise ValueError("Detector result is missing explanation or provenance.")
            if row.get("suggestionOnly") is not True or type(row.get("sourceInTicks")) is not int or type(row.get("sourceOutTicks")) is not int or row["sourceInTicks"]<0 or row["sourceOutTicks"]<=row["sourceInTicks"] or row["sourceOutTicks"]>data["sourceDurationTicks"]: raise ValueError("Detector returned an invalid source range.")
            for field in ("confidence","signalStrength"):
                value=row.get(field)
                if value is not None and (not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<=value<=1): raise ValueError("Detector returned invalid signal strength.")
            if row.get("confidence") is None and row.get("signalStrength") is None: raise ValueError("Detector result requires confidence or signal strength.")
            if data["detectorId"] == "repeated_takes":
                start, end = row.get("matchedSourceInTicks"), row.get("matchedSourceOutTicks")
                if type(start) is not int or type(end) is not int or not 0 <= start < end <= data["sourceDurationTicks"]:
                    raise ValueError("Repeated-take result returned an invalid matched source range.")

    def commit_output(self, connection, store: ProjectStore, job_input: JobInput, artifact_id: str, target: Path, metadata: dict[str, Any] | None = None) -> None:
        data=json.loads(target.read_text(encoding="utf-8"))
        for row in data["results"]:
            store.add_analysis(asset_id=row["assetId"],kind=row["detectorId"],source_in_ticks=row["sourceInTicks"],source_out_ticks=row["sourceOutTicks"],result=row,generator_id=f"localcut-{row['detectorId']}",generator_version=row["detectorVersion"],source_fingerprint=job_input.source_fingerprint,confidence=row.get("confidence"),artifact_id=artifact_id,config=data["config"],connection=connection)

