import json
from pathlib import Path
import shutil
import struct
import wave

import pytest

from localcut_service import analysis
from localcut_service.analysis import AnalysisHandler, analysis_job
from localcut_service.config import AppConfig
from localcut_service.jobs import JobCancelled, JobOutput, JobQueue
from localcut_service.persistence import ProjectStore, _connect, _sha256_file
from localcut_service.server import dispatch


def test_repeated_take_upper_bound_skips_impossible_sequence_comparison(monkeypatch):
    class BoundedMatcher:
        def __init__(self, *_args, **_kwargs):
            pass

        def quick_ratio(self):
            return 0.25

        def ratio(self):
            raise AssertionError("full sequence comparison should be skipped")

    monkeypatch.setattr(analysis, "SequenceMatcher", BoundedMatcher)
    assert analysis._bounded_text_similarity(["one", "two"], ["three", "four"], 0.7) is None


@pytest.fixture
def prepared(tmp_path):
    store = ProjectStore.create(tmp_path / "project unusual [雪] !", "Analysis")
    source = tmp_path / "source footage [雪] !.mov"
    source.write_bytes(b"immutable source")
    imported = store.import_asset_reference(source, {"durationSeconds": 10}, probe_tool="fixture", probe_version="1", duration_ticks=10_000_000)
    asset_id = imported["assetId"]
    bundle = store.root / "cache" / "proxies" / "fixture-bundle"
    bundle.mkdir(parents=True)
    (bundle / "proxy.mp4").write_bytes(b"proxy")
    with wave.open(str(bundle / "analysis.wav"), "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(1000)
        # 0.5 seconds quiet and 1.5 seconds of a 0.5 amplitude tone.
        wav.writeframes(struct.pack("<h", 0) * 500 + struct.pack("<h", 16000) * 1500)
    mapping = {"sourceTimebase": "1/1000000", "proxyTimebase": "1/1000000", "sourceMinusProxySeconds": "1/4", "sourceStartSeconds": "0", "frames": []}
    (bundle / "mapping.json").write_text(json.dumps(mapping))
    (bundle / "thumbnail.png").write_bytes(b"fixture thumbnail")
    manifest = {"pipelineVersion": "preview-2", "analysis": {"path": "analysis.wav", "sampleRate": 1000, "samples": 2000, "sourceStartSeconds": "5/2"}, "proxy": "proxy.mp4", "thumbnails": [{"path": "thumbnail.png", "sourcePts": 5_000_000}]}
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    proxy_id = store.register_artifact(asset_id=asset_id, kind="proxy", relative_path=bundle.relative_to(store.root).as_posix(), source_fingerprint=_sha256_file(source), generator_id="fixture", generator_version="1", checksum_sha256=_sha256_file(bundle))
    return store, asset_id, source, bundle, proxy_id


def test_audio_detectors_use_original_audio_start_and_microsecond_ranges(prepared, tmp_path):
    store, asset_id, source, _, _ = prepared
    handler = AnalysisHandler(ffmpeg="ffmpeg")
    queue = JobQueue(tmp_path / "jobs", handlers={"analysis": handler}, concurrency=1)
    try:
        silence = queue.submit(analysis_job(store, asset_id, "silence", {"thresholdDbfs": -38, "minimumMs": 300, "mergeGapMs": 100}))
        completed = queue.wait(silence["id"], 3)
        assert completed["state"] == "completed", completed.get("error")
        saved = store.list_analyses(asset_id, "silence")
        assert saved
        row = saved[0]["result"]
        assert row["sourceInTicks"] == 2_500_000
        assert row["sourceOutTicks"] == 3_000_000
        assert row["suggestionOnly"] is True
        assert row["detectorVersion"] == "4"
        assert "Quiet speech" in row["explanation"]
        assert row["measurementWindowMs"] == 250
        assert "Boundary timing is window-quantized" in row["explanation"]
        levels = queue.submit(analysis_job(store, asset_id, "audio_levels", {"windowMs": 250}))
        assert queue.wait(levels["id"], 3)["state"] == "completed"
        measured = store.list_analyses(asset_id, "audio_levels")
        assert measured[0]["result"]["rmsDbfs"] < -8
        assert measured[-1]["result"]["peakDbfs"] == pytest.approx(-6.23, abs=.1)
        assert measured[0]["sourceInTicks"] == 2_500_000
        assert measured[0]["result"]["windowMs"] == 250
        assert measured[0]["result"]["aggregationWindowMs"] == 1000
        assert "250 ms windows aggregated over approximately 1000 ms" in measured[0]["result"]["explanation"]
        assert source.read_bytes() == b"immutable source"
    finally:
        queue.close()


def test_silence_signal_is_measured_per_span_not_derived_from_threshold(prepared, tmp_path):
    store, asset_id, _, bundle, proxy_id = prepared
    wav_path = bundle / "analysis.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(1000)
        wav.writeframes(
            struct.pack("<h", 1000) * 500
            + struct.pack("<h", 12000) * 500
            + struct.pack("<h", 3000) * 500
            + struct.pack("<h", 12000) * 500
        )
    with _connect(store.database_path) as db:
        db.execute("UPDATE artifacts SET checksum_sha256=? WHERE id=?", (_sha256_file(bundle), proxy_id))
        db.commit()

    queue = JobQueue(tmp_path / "jobs-silence-signal", handlers={"analysis": AnalysisHandler()}, concurrency=1)
    try:
        submitted = queue.submit(analysis_job(store, asset_id, "silence", {"thresholdDbfs": -18, "minimumMs": 400, "mergeGapMs": 0}))
        completed = queue.wait(submitted["id"], 3)
        assert completed["state"] == "completed", completed.get("error")
        rows = sorted(store.list_analyses(asset_id, "silence"), key=lambda row: row["sourceInTicks"])
        assert len(rows) == 2
        quieter, louder = [row["result"] for row in rows]
        assert quieter["meanQuietWindowRmsDbfs"] < louder["meanQuietWindowRmsDbfs"]
        assert quieter["thresholdMarginDb"] > louder["thresholdMarginDb"]
        assert quieter["signalStrength"] > louder["signalStrength"]
        assert quieter["signalStrength"] == pytest.approx(min(1, quieter["thresholdMarginDb"] / 24))
        assert quieter["meanQuietWindowRmsDbfs"] < -25
        assert louder["meanQuietWindowRmsDbfs"] < -15
        assert "dB below" in quieter["explanation"]
        assert quieter["thresholdDbfs"] == -18
    finally:
        queue.close()


def test_silence_hysteresis_keeps_threshold_flicker_in_one_candidate(prepared, tmp_path):
    store, asset_id, _, bundle, proxy_id = prepared
    wav_path = bundle / "analysis.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(1000)
        wav.writeframes(
            struct.pack("<h", 367) * 250
            + struct.pack("<h", 463) * 250
            + struct.pack("<h", 367) * 250
            + struct.pack("<h", 463) * 250
        )
    with _connect(store.database_path) as db:
        db.execute("UPDATE artifacts SET checksum_sha256=? WHERE id=?", (_sha256_file(bundle), proxy_id))
        db.commit()

    queue = JobQueue(tmp_path / "jobs-silence-hysteresis", handlers={"analysis": AnalysisHandler()}, concurrency=1)
    try:
        no_hysteresis = queue.submit(analysis_job(store, asset_id, "silence", {"thresholdDbfs": -38, "hysteresisDb": 0, "minimumMs": 500, "mergeGapMs": 0}))
        assert queue.wait(no_hysteresis["id"], 3)["state"] == "completed"
        assert store.list_analyses(asset_id, "silence") == []

        with_hysteresis = queue.submit(analysis_job(store, asset_id, "silence", {"thresholdDbfs": -38, "hysteresisDb": 4, "minimumMs": 500, "mergeGapMs": 0}))
        completed = queue.wait(with_hysteresis["id"], 3)
        assert completed["state"] == "completed", completed.get("error")
        rows = store.list_analyses(asset_id, "silence")
        assert len(rows) == 1
        result = rows[0]["result"]
        assert result["sourceInTicks"] == 2_500_000
        assert result["sourceOutTicks"] == 3_500_000
        assert result["hysteresisDb"] == 4
        assert result["exitThresholdDbfs"] == -34
        assert result["quietDurationSeconds"] == pytest.approx(.5)
        assert "reduce threshold flicker" in result["explanation"]
    finally:
        queue.close()


def test_nonzero_video_and_audio_pts_origins_are_stored_as_media_relative_time(prepared, tmp_path):
    store, asset_id, source, bundle, proxy_id = prepared
    mapping_path = bundle / "mapping.json"
    mapping = json.loads(mapping_path.read_text())
    mapping["sourceMinusProxySeconds"] = "37/4"
    mapping["sourceStartSeconds"] = "9"
    mapping_path.write_text(json.dumps(mapping))
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["analysis"]["sourceStartSeconds"] = "21/2"
    manifest["pipelineVersion"] = "preview-2"
    manifest["thumbnails"][0]["sourcePts"] = 9_000_000
    manifest_path.write_text(json.dumps(manifest))
    with _connect(store.database_path) as db:
        db.execute("UPDATE artifacts SET checksum_sha256=? WHERE id=?", (_sha256_file(bundle), proxy_id))
        db.commit()

    queue = JobQueue(tmp_path / "jobs-origins", handlers={"analysis": AnalysisHandler()}, concurrency=1)
    try:
        levels = queue.submit(analysis_job(store, asset_id, "audio_levels", {"windowMs": 250}))
        assert queue.wait(levels["id"], 3)["state"] == "completed"
        audio_rows = store.list_analyses(asset_id, "audio_levels")
        # Audio begins at absolute 10.5s; source video begins at 9s.
        assert audio_rows[0]["sourceInTicks"] == 1_500_000

        thumbs = queue.submit(analysis_job(store, asset_id, "thumbnails", {"count": 1}))
        assert queue.wait(thumbs["id"], 3)["state"] == "completed"
        thumb = store.list_analyses(asset_id, "thumbnails")[-1]
        assert thumb["sourceInTicks"] == 0
        assert thumb["sourceOutTicks"] == 1
        assert source.read_bytes() == b"immutable source"
    finally:
        queue.close()


def test_analysis_requires_proxy_with_current_timestamp_contract(prepared):
    store, asset_id, _, bundle, proxy_id = prepared
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["pipelineVersion"] = "preview-1"
    manifest_path.write_text(json.dumps(manifest))
    with _connect(store.database_path) as db:
        db.execute("UPDATE artifacts SET checksum_sha256=? WHERE id=?", (_sha256_file(bundle), proxy_id))
        db.commit()
    with pytest.raises(ValueError, match="older timestamp map"):
        analysis_job(store, asset_id, "audio_levels", {"windowMs": 250})


def test_video_timestamps_use_showinfo_pts_and_path_is_one_argument(prepared, tmp_path):
    store, asset_id, source, _, _ = prepared
    class FakeProcess:
        args = None
        options = None
        def run(self, args, context, **kwargs):
            self.args = args
            self.options = kwargs
            kwargs["stderr_capture"].write_text("[Parsed_showinfo] n: 0 pts: 0 pts_time:0\n[Parsed_showinfo] n: 1 pts: 1 pts_time:1\n[Parsed_showinfo] n: 2 pts: 2 pts_time:2\n")
            first = bytes([0]) * analysis.FRAME_SIZE
            changed = bytes([255]) * analysis.FRAME_SIZE
            kwargs["stdout_file"].write_bytes(first + changed + changed)
            return b""
    process = FakeProcess()
    handler = AnalysisHandler(ffmpeg="ffmpeg", process=process)
    queue = JobQueue(tmp_path / "jobs-video", handlers={"analysis": handler}, concurrency=1)
    try:
        job = analysis_job(store, asset_id, "shot_boundaries", {"sampleFps": 1, "changeThreshold": .2, "minimumSeparationSeconds": 1})
        result = queue.submit(job)
        assert queue.wait(result["id"], 3)["state"] == "completed"
        shot = store.list_analyses(asset_id, "shot_boundaries")[0]
        assert (shot["sourceInTicks"], shot["sourceOutTicks"]) == (250_000, 1_250_000)
        assert shot["result"]["suggestionOnly"]
        assert shot["result"]["detectorVersion"] == "4"
        assert str(source) not in process.args  # Source paths are never passed to FFmpeg.
        assert "-i" in process.args and process.args[process.args.index("-i") + 1].endswith("proxy.mp4")
        assert process.args[process.args.index("-progress") + 1] == "pipe:1"
        assert process.options["stdout_progress_only"] is True
        assert process.options["base"] == .12 and process.options["span"] == .38
    finally:
        queue.close()


def test_video_timestamp_mapping_uses_sample_pts_with_irregular_cadence(prepared, tmp_path):
    store, asset_id, _, _, _ = prepared
    class IrregularSamples:
        def run(self, args, context, **kwargs):
            kwargs["stderr_capture"].write_text("n: 0 pts_time:0\nn: 1 pts_time:0.5\nn: 2 pts_time:1.75\n")
            dark = bytes([0]) * analysis.FRAME_SIZE
            light = bytes([255]) * analysis.FRAME_SIZE
            kwargs["stdout_file"].write_bytes(dark + light + light)
            return b""
    queue = JobQueue(tmp_path / "jobs-irregular-pts", handlers={"analysis": AnalysisHandler(ffmpeg="ffmpeg", process=IrregularSamples())}, concurrency=1)
    try:
        submitted = queue.submit(analysis_job(store, asset_id, "shot_boundaries", {"sampleFps": 2, "changeThreshold": .2, "minimumSeparationSeconds": .25}))
        assert queue.wait(submitted["id"], 10)["state"] == "completed"
        result = store.list_analyses(asset_id, "shot_boundaries")[0]
        # The complete irregular .5s sample interval is reported as timing uncertainty.
        assert (result["sourceInTicks"], result["sourceOutTicks"]) == (250_000, 750_000)
        assert result["result"]["samplingUncertaintySeconds"] == pytest.approx(.5)
    finally:
        queue.close()


def test_video_timestamps_are_clamped_to_source_duration_at_boundaries(prepared, tmp_path):
    store, asset_id, _, _, _ = prepared
    class BoundarySamples:
        def run(self, args, context, **kwargs):
            # A negative initial PTS and a sample beyond source duration must
            # not produce negative or out-of-range persisted suggestions.
            kwargs["stderr_capture"].write_text("n: 0 pts_time:-0.5\nn: 1 pts_time:9.5\n")
            kwargs["stdout_file"].write_bytes(bytes([0]) * analysis.FRAME_SIZE + bytes([255]) * analysis.FRAME_SIZE)
            return b""
    queue = JobQueue(tmp_path / "jobs-boundaries", handlers={"analysis": AnalysisHandler(ffmpeg="ffmpeg", process=BoundarySamples())}, concurrency=1)
    try:
        submitted = queue.submit(analysis_job(store, asset_id, "shot_boundaries", {"sampleFps": 1, "changeThreshold": .2, "minimumSeparationSeconds": .25}))
        completed = queue.wait(submitted["id"], 3)
        assert completed["state"] == "completed", completed.get("error")
        rows = store.list_analyses(asset_id, "shot_boundaries")
        assert rows
        assert all(row["sourceInTicks"] >= 0 for row in rows)
        assert all(row["sourceOutTicks"] <= 10_000_000 for row in rows)
        assert all(row["sourceInTicks"] < row["sourceOutTicks"] for row in rows)
    finally:
        queue.close()


def test_shot_boundary_uses_media_relative_time_for_nonzero_vfr_pts_origin(prepared, tmp_path):
    store, asset_id, _, bundle, proxy_id = prepared
    mapping_path = bundle / "mapping.json"
    mapping = json.loads(mapping_path.read_text())
    mapping.update({"sourceMinusProxySeconds": "37/4", "sourceStartSeconds": "9"})
    mapping_path.write_text(json.dumps(mapping))
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["pipelineVersion"] = "preview-2"
    manifest_path.write_text(json.dumps(manifest))
    with _connect(store.database_path) as db:
        db.execute("UPDATE artifacts SET checksum_sha256=? WHERE id=?", (_sha256_file(bundle), proxy_id)); db.commit()
    class VariableSamples:
        def run(self, args, context, **kwargs):
            kwargs["stderr_capture"].write_text("n: 0 pts_time:0\nn: 1 pts_time:0.25\nn: 2 pts_time:1.5\n")
            kwargs["stdout_file"].write_bytes(bytes([0]) * analysis.FRAME_SIZE + bytes([255]) * analysis.FRAME_SIZE * 2)
            return b""
    queue = JobQueue(tmp_path / "jobs-nonzero-vfr", handlers={"analysis": AnalysisHandler(ffmpeg="ffmpeg", process=VariableSamples())}, concurrency=1)
    try:
        job = queue.submit(analysis_job(store, asset_id, "shot_boundaries", {"sampleFps": 1, "changeThreshold": .2, "minimumSeparationSeconds": .25}))
        done = queue.wait(job["id"], 3)
        assert done["state"] == "completed", done.get("error")
        row = store.list_analyses(asset_id, "shot_boundaries")[0]
        # Absolute source PTS begins at 9s while the edited media timeline begins at 0.
        assert (row["sourceInTicks"], row["sourceOutTicks"]) == (250_000, 500_000)
    finally:
        queue.close()


def test_blur_signal_uses_segment_median_not_only_trailing_sample(prepared, tmp_path):
    store, asset_id, _, _, _ = prepared
    flat = bytearray([80]) * analysis.FRAME_SIZE
    textured = flat.copy()
    for y in range(2, 88, 4):
        for x in range(2, 158, 4):
            textured[y * 160 + x] = 85
    flat, textured = bytes(flat), bytes(textured)
    class MixedSamples:
        def run(self, args, context, **kwargs):
            kwargs["stderr_capture"].write_text("n: 0 pts_time:0\nn: 1 pts_time:1\nn: 2 pts_time:2\nn: 3 pts_time:3\n")
            kwargs["stdout_file"].write_bytes(flat + textured + textured + flat)
            return b""
    queue = JobQueue(tmp_path / "jobs-blur-median", handlers={"analysis": AnalysisHandler(ffmpeg="ffmpeg", process=MixedSamples())}, concurrency=1)
    try:
        job = queue.submit(analysis_job(store, asset_id, "blur", {"sampleFps": 1, "minimumDurationSeconds": 2.5, "laplacianVarianceThreshold": 300}))
        done = queue.wait(job["id"], 3)
        assert done["state"] == "completed", done.get("error")
        result = store.list_analyses(asset_id, "blur")[0]["result"]
        assert result["sampleCount"] == 4
        assert result["minimumLaplacianVariance"] == 0
        assert result["maximumLaplacianVariance"] > result["laplacianVariance"] > 0
        assert "not a probability" in result["explanation"]
    finally:
        queue.close()


def test_repeated_take_comparison_limit_is_persisted_as_an_incompleteness_warning(prepared, tmp_path):
    store, asset_id, source, _, _ = prepared
    transcript_file = store.root / "cache" / "long-transcript.json"
    transcript_file.write_text("{}")
    artifact_id = store.register_artifact(asset_id=asset_id, kind="transcript", relative_path=transcript_file.relative_to(store.root).as_posix(), source_fingerprint=_sha256_file(source), generator_id="fixture-asr", generator_version="1", checksum_sha256=_sha256_file(transcript_file))
    words = []
    phrase = ["we", "show", "a", "tutorial"]
    for group in range(65):
        start = group * 1_300_000
        for index, text in enumerate(phrase):
            tick = start + index * 100_000
            words.append({"text": text, "sourceInTicks": tick, "sourceOutTicks": tick + 50_000})
    store.add_transcript(asset_id=asset_id, language="en", words=words, generator_id="fixture-asr", generator_version="1", source_fingerprint=_sha256_file(source), artifact_id=artifact_id)
    with _connect(store.database_path) as db:
        db.execute("UPDATE assets SET duration_ticks=? WHERE id=?", (100_000_000, asset_id)); db.commit()
    queue = JobQueue(tmp_path / "jobs-repeat-limit", handlers={"analysis": AnalysisHandler()}, concurrency=1)
    try:
        job = queue.submit(analysis_job(store, asset_id, "repeated_takes", {}))
        done = queue.wait(job["id"], 10)
        assert done["state"] == "completed", done.get("error")
        artifact = store.root / "cache" / "analysis" / "repeated_takes" / f"{job['id']}.json"
        data = json.loads(artifact.read_text())
        assert any(warning["code"] == "repeated_take_comparison_limit" for warning in data["warnings"])
    finally:
        queue.close()


def test_repeated_take_skips_oversized_passages_with_warning(prepared, tmp_path):
    store, asset_id, source, _, _ = prepared
    transcript_file = store.root / "cache" / "long-passage-transcript.json"
    transcript_file.write_text("{}")
    artifact_id = store.register_artifact(asset_id=asset_id, kind="transcript", relative_path=transcript_file.relative_to(store.root).as_posix(), source_fingerprint=_sha256_file(source), generator_id="fixture-asr", generator_version="1", checksum_sha256=_sha256_file(transcript_file))
    words = []
    for start in (1_000_000, 20_000_000):
        for index in range(501):
            tick = start + index * 20_000
            words.append({"text": f"word{index}", "sourceInTicks": tick, "sourceOutTicks": tick + 10_000})
    store.add_transcript(asset_id=asset_id, language="en", words=words, generator_id="fixture-asr", generator_version="1", source_fingerprint=_sha256_file(source), artifact_id=artifact_id)
    with _connect(store.database_path) as db:
        db.execute("UPDATE assets SET duration_ticks=? WHERE id=?", (40_000_000, asset_id)); db.commit()
    queue = JobQueue(tmp_path / "jobs-repeat-long-passage", handlers={"analysis": AnalysisHandler()}, concurrency=1)
    try:
        job = queue.submit(analysis_job(store, asset_id, "repeated_takes", {}))
        done = queue.wait(job["id"], 10)
        assert done["state"] == "completed", done.get("error")
        artifact = store.root / "cache" / "analysis" / "repeated_takes" / f"{job['id']}.json"
        data = json.loads(artifact.read_text())
        warning = next(item for item in data["warnings"] if item["code"] == "repeated_take_passage_limit")
        assert "Skipped 2 transcript passage(s)" in warning["message"]
        assert data["results"] == []
    finally:
        queue.close()


def test_detector_settings_and_proxy_changes_invalidate_only_that_analysis(prepared, tmp_path):
    store, asset_id, _, bundle, _ = prepared
    queue = JobQueue(tmp_path / "jobs-cache", handlers={"analysis": AnalysisHandler()}, concurrency=1)
    try:
        job = analysis_job(store, asset_id, "audio_levels", {"windowMs": 250})
        first = queue.submit(job); assert queue.wait(first["id"], 3)["state"] == "completed"
        duplicate = queue.submit(job)
        assert duplicate["deduplicated"] is True
        changed = queue.submit(analysis_job(store, asset_id, "audio_levels", {"windowMs": 500}))
        assert changed["id"] != first["id"]
        assert queue.wait(changed["id"], 3)["state"] == "completed"
        prior = analysis_job(store, asset_id, "audio_levels", {"windowMs": 250})
        alternate = store.root / "cache" / "proxies" / "updated-proxy"
        shutil.copytree(bundle, alternate)
        (alternate / "proxy.mp4").write_bytes(b"new validated proxy bytes")
        artifact_id = store.register_artifact(asset_id=asset_id, kind="proxy", relative_path=alternate.relative_to(store.root).as_posix(), source_fingerprint=prior.source_fingerprint, generator_id="fixture", generator_version="2", checksum_sha256=_sha256_file(alternate))
        with _connect(store.database_path) as db:
            db.execute("UPDATE artifacts SET created_at='9999-01-01T00:00:00Z' WHERE id=?", (artifact_id,)); db.commit()
        invalidated = analysis_job(store, asset_id, "audio_levels", {"windowMs": 250})
        assert invalidated.config["proxyArtifactId"] != prior.config["proxyArtifactId"]
        refreshed = queue.submit(invalidated)
        assert refreshed["id"] not in {first["id"], changed["id"]}
        assert queue.wait(refreshed["id"], 3)["state"] == "completed"
        assert len(store.list_analyses(asset_id, "audio_levels")) > 1
    finally:
        queue.close()


def test_transcript_edit_invalidates_cached_detector_input(prepared, tmp_path):
    store, asset_id, source, _, _ = prepared
    transcript_file = store.root / "cache" / "transcript-review.json"
    transcript_file.write_text("{}")
    artifact_id = store.register_artifact(asset_id=asset_id, kind="transcript", relative_path=transcript_file.relative_to(store.root).as_posix(), source_fingerprint=_sha256_file(source), generator_id="fixture-asr", generator_version="1", checksum_sha256=_sha256_file(transcript_file))
    transcript_id = store.add_transcript(asset_id=asset_id, language="en", words=[{"text": "hello", "sourceInTicks": 1_000_000, "sourceOutTicks": 1_200_000}], generator_id="fixture-asr", generator_version="1", source_fingerprint=_sha256_file(source), artifact_id=artifact_id)
    before = analysis_job(store, asset_id, "speech_regions", {})
    store.set_transcript_range_marker(transcript_id, 1_000_000, 1_500_000, "excluded")
    after = analysis_job(store, asset_id, "speech_regions", {})
    assert before.config["transcriptReviewFingerprint"] != after.config["transcriptReviewFingerprint"]
    assert before.config != after.config


def test_transcript_regions_and_repeated_takes_keep_source_timestamps(prepared, tmp_path):
    store, asset_id, source, _, _ = prepared
    transcript_file = store.root / "cache" / "fixture-transcript.json"
    transcript_file.write_text('{"fixture":true}')
    transcript_artifact = store.register_artifact(asset_id=asset_id, kind="transcript", relative_path=transcript_file.relative_to(store.root).as_posix(), source_fingerprint=_sha256_file(source), generator_id="fixture-asr", generator_version="1", checksum_sha256=_sha256_file(transcript_file))
    phrase = ["we", "show", "a", "video", "tutorial", "today"]
    words = []
    for start in (500_000, 5_000_000):
        for index, text in enumerate(phrase):
            a = start + index * 200_000
            words.append({"text": text, "sourceInTicks": a, "sourceOutTicks": a + 150_000, "confidence": .9})
    transcript_id = store.add_transcript(asset_id=asset_id, language="en", words=words, generator_id="fixture-asr", generator_version="1", source_fingerprint=_sha256_file(source), artifact_id=transcript_artifact)
    queue = JobQueue(tmp_path / "jobs-transcript", handlers={"analysis": AnalysisHandler()}, concurrency=1)
    try:
        speech = queue.submit(analysis_job(store, asset_id, "speech_regions", {}))
        assert queue.wait(speech["id"], 3)["state"] == "completed"
        spoken = store.list_analyses(asset_id, "speech_regions")
        assert spoken[0]["sourceInTicks"] == 500_000
        assert spoken[0]["result"]["transcriptId"] == transcript_id
        repeated = queue.submit(analysis_job(store, asset_id, "repeated_takes", {}))
        completed = queue.wait(repeated["id"], 3)
        assert completed["state"] == "completed", completed.get("error")
        pair = store.list_analyses(asset_id, "repeated_takes")[0]["result"]
        assert pair["sourceInTicks"] == 500_000
        assert pair["matchedSourceInTicks"] == 5_000_000
        assert pair["confidence"] is None
        assert pair["text"] == pair["matchedText"]
        assert pair["textSimilarity"] == pytest.approx(1)
        assert pair["durationSimilarity"] == pytest.approx(1)
        assert pair["detectorVersion"] == "5"
        before_edit=analysis_job(store,asset_id,"speech_regions",{})
        store.set_transcript_range_marker(transcript_id,500_000,1_000_000,"required")
        after_edit=analysis_job(store,asset_id,"speech_regions",{})
        assert before_edit.config["transcriptReviewFingerprint"] != after_edit.config["transcriptReviewFingerprint"]
    finally:
        queue.close()


def test_filler_word_suggestions_keep_word_times_and_settings_cache_independent(prepared, tmp_path):
    store, asset_id, source, _, _ = prepared
    transcript_file = store.root / "cache" / "fixture-filler-transcript.json"
    transcript_file.write_text('{"fixture":true}')
    artifact_id = store.register_artifact(asset_id=asset_id, kind="transcript", relative_path=transcript_file.relative_to(store.root).as_posix(), source_fingerprint=_sha256_file(source), generator_id="fixture-asr", generator_version="1", checksum_sha256=_sha256_file(transcript_file))
    words = [
        {"text": "Um,", "sourceInTicks": 1_000_000, "sourceOutTicks": 1_200_000, "confidence": .8},
        {"text": "uh.", "sourceInTicks": 1_300_000, "sourceOutTicks": 1_500_000, "confidence": .6},
        {"text": "like", "sourceInTicks": 1_550_000, "sourceOutTicks": 1_650_000, "confidence": .95},
        {"text": "er!", "sourceInTicks": 1_700_000, "sourceOutTicks": 1_950_000, "confidence": None},
    ]
    store.add_transcript(asset_id=asset_id, language="en", words=words, generator_id="fixture-asr", generator_version="1", source_fingerprint=_sha256_file(source), artifact_id=artifact_id)
    queue = JobQueue(tmp_path / "jobs-fillers", handlers={"analysis": AnalysisHandler()}, concurrency=1)
    try:
        first_input = analysis_job(store, asset_id, "filler_words", {"mergeGapMs": 250})
        first = queue.submit(first_input)
        done = queue.wait(first["id"], 3)
        assert done["state"] == "completed", done.get("error")
        rows = store.list_analyses(asset_id, "filler_words")
        assert [(row["sourceInTicks"], row["sourceOutTicks"]) for row in rows] == [(1_000_000, 1_500_000), (1_700_000, 1_950_000)]
        first_result = rows[0]["result"]
        assert first_result["assetId"] == asset_id
        assert first_result["detectorVersion"] == "1"
        assert first_result["terms"] == ["um", "uh"]
        assert first_result["confidence"] == pytest.approx(.7)
        assert "intentional" in first_result["explanation"]
        assert [row["result"]["terms"] for row in rows] == [["um", "uh"], ["er"]]

        changed_input = analysis_job(store, asset_id, "filler_words", {"mergeGapMs": 50})
        assert changed_input.config != first_input.config
        changed = queue.submit(changed_input)
        assert changed["id"] != first["id"]
        changed_done = queue.wait(changed["id"], 3)
        assert changed_done["state"] == "completed", changed_done.get("error")
        changed_rows = store.list_analyses(asset_id, "filler_words")
        assert len(changed_rows) == 5
        assert len({row["artifactId"] for row in changed_rows}) == 2
    finally:
        queue.close()


def test_cancellation_does_not_commit_artifact_or_rows(prepared, tmp_path):
    store, asset_id, _, _, _ = prepared
    class CancellingProcess:
        queue = None
        def run(self, args, context, **kwargs):
            self.queue.cancel(context.job_id)
            context.check_cancelled()
    process = CancellingProcess()
    handler = AnalysisHandler(ffmpeg="ffmpeg", process=process)
    queue = JobQueue(tmp_path / "jobs-cancel", handlers={"analysis": handler}, concurrency=1)
    process.queue = queue
    try:
        submitted = queue.submit(analysis_job(store, asset_id, "near_black", {}))
        done = queue.wait(submitted["id"], 3)
        assert done["state"] == "cancelled"
        assert store.list_analyses(asset_id, "near_black") == []
        with _connect(store.database_path) as db:
            assert db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='analysis:near_black'").fetchone()[0] == 0
    finally:
        queue.close()


def test_video_detector_fails_actionably_before_decode_when_staging_space_is_low(prepared, tmp_path, monkeypatch):
    from types import SimpleNamespace
    store, asset_id, _, _, _ = prepared
    calls=[]
    class MustNotDecode:
        def run(self, *_args, **_kwargs): calls.append(True); raise AssertionError("FFmpeg must not start after a failed space preflight")
    monkeypatch.setattr(analysis.shutil,"disk_usage",lambda _path:SimpleNamespace(total=1,used=0,free=1))
    queue=JobQueue(tmp_path/"jobs-low-disk",handlers={"analysis":AnalysisHandler(ffmpeg="ffmpeg",process=MustNotDecode())},concurrency=1)
    try:
        submitted=queue.submit(analysis_job(store,asset_id,"near_black",{}))
        result=queue.wait(submitted["id"],3)
        assert result["state"]=="failed"
        assert "free staging space" in result["error"]["message"]
        assert calls==[]
        assert store.list_analyses(asset_id,"near_black")==[]
    finally:
        queue.close()


def test_independent_detectors_and_bounded_settings():
    assert {row["id"] for row in analysis.catalog()} == set(analysis.DETECTORS)
    with pytest.raises(ValueError): analysis._settings("silence", {"thresholdDbfs": -100})
    with pytest.raises(ValueError, match="Unknown setting.*thresholdDbfss"):
        analysis._settings("silence", {"thresholdDbfss": -38})
    with pytest.raises(ValueError): analysis._settings("motion", {"sampleFps": float("nan")})
    assert analysis._settings("motion", {"sampleFps": 0.25})["sampleFps"] == 0.25
    assert analysis._settings("near_black", {"sampleFps": 0.5})["sampleFps"] == 0.5


def test_every_cached_result_has_source_time_and_detector_provenance(prepared, tmp_path):
    store, asset_id, _, _, _ = prepared
    queue = JobQueue(tmp_path / "jobs-provenance", handlers={"analysis": AnalysisHandler()}, concurrency=1)
    try:
        job = queue.submit(analysis_job(store, asset_id, "audio_levels", {"windowMs": 250}))
        assert queue.wait(job["id"], 3)["state"] == "completed"
        rows = store.list_analyses(asset_id, "audio_levels")
        assert rows
        for row in rows:
            result = row["result"]
            assert result["assetId"] == asset_id
            assert result["sourceInTicks"] < result["sourceOutTicks"] <= 10_000_000
            assert result["detectorId"] == "audio_levels"
            assert result["detectorName"] == analysis.DETECTORS["audio_levels"]["name"]
            assert result["detectorVersion"] == analysis.DETECTORS["audio_levels"]["version"]
            assert result["explanation"]
            assert result["suggestionOnly"] is True
            assert result["confidence"] is not None or result["signalStrength"] is not None
    finally:
        queue.close()


def test_near_black_sample_duration_covers_sample_interval(prepared, tmp_path):
    store, asset_id, _, _, _ = prepared
    class DarkSamples:
        def run(self, args, context, **kwargs):
            kwargs["stderr_capture"].write_text("n: 0 pts: 0 pts_time:0\nn: 1 pts: 1 pts_time:1\nn: 2 pts: 2 pts_time:2\n")
            kwargs["stdout_file"].write_bytes(bytes([0]) * (analysis.FRAME_SIZE * 3))
            return b""
    queue = JobQueue(tmp_path / "jobs-dark", handlers={"analysis": AnalysisHandler(ffmpeg="ffmpeg", process=DarkSamples())}, concurrency=1)
    try:
        job=queue.submit(analysis_job(store, asset_id, "near_black", {"sampleFps": 1, "minimumDurationSeconds": 1.5}))
        assert queue.wait(job["id"], 3)["state"] == "completed"
        row=store.list_analyses(asset_id,"near_black")[0]
        assert row["sourceOutTicks"] - row["sourceInTicks"] >= 2_000_000
    finally:
        queue.close()


def test_near_black_ignores_persistent_letterbox_bars(prepared, tmp_path):
    store, asset_id, _, _, _ = prepared
    class LetterboxedSamples:
        def run(self, args, context, **kwargs):
            kwargs["stderr_capture"].write_text("n: 0 pts: 0 pts_time:0\nn: 1 pts: 1 pts_time:1\nn: 2 pts: 2 pts_time:2\n")
            frame = bytearray([0] * analysis.FRAME_SIZE)
            for y in range(9, 81):
                for x in range(16, 144):
                    frame[y * 160 + x] = 90
            kwargs["stdout_file"].write_bytes(bytes(frame) * 3)
            return b""
    queue = JobQueue(tmp_path / "jobs-bars", handlers={"analysis": AnalysisHandler(ffmpeg="ffmpeg", process=LetterboxedSamples())}, concurrency=1)
    try:
        job = queue.submit(analysis_job(store, asset_id, "near_black", {"sampleFps": 1, "minimumDurationSeconds": 1.5}))
        assert queue.wait(job["id"], 3)["state"] == "completed"
        assert store.list_analyses(asset_id, "near_black") == []
    finally:
        queue.close()


def test_near_black_ignores_4_3_content_letterboxed_inside_16_9_proxy(prepared, tmp_path):
    store, asset_id, _, _, _ = prepared
    class FourThreeSamples:
        def run(self, args, context, **kwargs):
            kwargs["stderr_capture"].write_text("n: 0 pts_time:0\nn: 1 pts_time:1\nn: 2 pts_time:2\n")
            # A 4:3 image fitted into 16:9 has horizontal letterbox bars.
            frame = bytearray([0] * analysis.FRAME_SIZE)
            for y in range(15, 75):
                for x in range(160):
                    frame[y * 160 + x] = 96
            kwargs["stdout_file"].write_bytes(bytes(frame) * 3)
            return b""
    queue = JobQueue(tmp_path / "jobs-bars-4-3", handlers={"analysis": AnalysisHandler(ffmpeg="ffmpeg", process=FourThreeSamples())}, concurrency=1)
    try:
        submitted = queue.submit(analysis_job(store, asset_id, "near_black", {"sampleFps": 1, "minimumDurationSeconds": 1.5}))
        completed = queue.wait(submitted["id"], 3)
        assert completed["state"] == "completed", completed.get("error")
        assert store.list_analyses(asset_id, "near_black") == []
    finally:
        queue.close()


def test_near_black_flags_dark_image_content_with_review_caveat(prepared, tmp_path):
    store, asset_id, _, _, _ = prepared
    class DarkSceneSamples:
        def run(self, args, context, **kwargs):
            kwargs["stderr_capture"].write_text("n: 0 pts_time:0\nn: 1 pts_time:1\nn: 2 pts_time:2\n")
            frame = bytearray([0] * analysis.FRAME_SIZE)
            # The content area is dim and the surrounding bars are also black.
            for y in range(15, 75):
                for x in range(160):
                    frame[y * 160 + x] = 8
            kwargs["stdout_file"].write_bytes(bytes(frame) * 3)
            return b""
    queue = JobQueue(tmp_path / "jobs-dark-scene-bars", handlers={"analysis": AnalysisHandler(ffmpeg="ffmpeg", process=DarkSceneSamples())}, concurrency=1)
    try:
        submitted = queue.submit(analysis_job(store, asset_id, "near_black", {"sampleFps": 1, "minimumDurationSeconds": 1.5}))
        completed = queue.wait(submitted["id"], 3)
        assert completed["state"] == "completed", completed.get("error")
        rows = store.list_analyses(asset_id, "near_black")
        assert rows
        assert rows[0]["result"]["suggestionOnly"] is True
        assert "intentionally dark scenes can trigger" in rows[0]["result"]["explanation"]
    finally:
        queue.close()


def test_detector_cache_identity_isolated_by_detector_and_transcript(prepared):
    store, asset_id, source, _, _ = prepared
    transcript_file=store.root/"cache"/"transcript-for-cache.json"; transcript_file.write_text("{}")
    aid=store.register_artifact(asset_id=asset_id,kind="transcript",relative_path=transcript_file.relative_to(store.root).as_posix(),source_fingerprint=_sha256_file(source),generator_id="fixture",generator_version="1",checksum_sha256=_sha256_file(transcript_file))
    store.add_transcript(asset_id=asset_id,language="en",words=[{"text":"hello","sourceInTicks":1_000_000,"sourceOutTicks":1_100_000}],generator_id="fixture",generator_version="1",source_fingerprint=_sha256_file(source),artifact_id=aid)
    audio=analysis_job(store,asset_id,"audio_levels",{"windowMs":250})
    speech=analysis_job(store,asset_id,"speech_regions",{})
    changed_transcript=store.root/"cache"/"transcript-for-cache-v2.json"; changed_transcript.write_text("{\"v\":2}")
    aid2=store.register_artifact(asset_id=asset_id,kind="transcript",relative_path=changed_transcript.relative_to(store.root).as_posix(),source_fingerprint=_sha256_file(source),generator_id="fixture",generator_version="2",checksum_sha256=_sha256_file(changed_transcript))
    store.add_transcript(asset_id=asset_id,language="en",words=[{"text":"hello","sourceInTicks":1_000_000,"sourceOutTicks":1_100_000}],generator_id="fixture",generator_version="2",source_fingerprint=_sha256_file(source),artifact_id=aid2)
    speech2=analysis_job(store,asset_id,"speech_regions",{})
    assert audio.config["detector"] != speech.config["detector"]
    assert speech.config["transcriptArtifactId"] != speech2.config["transcriptArtifactId"]


def test_analysis_list_marks_corrupt_artifacts_and_replaced_proxy_stale(prepared, tmp_path):
    store, asset_id, source, bundle, proxy_id = prepared
    source_fingerprint = _sha256_file(source)
    output = store.root / "cache" / "analysis" / "audio_levels" / "fixture.json"
    output.parent.mkdir(parents=True)
    result = {"assetId": asset_id, "sourceInTicks": 1_000_000, "sourceOutTicks": 2_000_000, "detectorId": "audio_levels", "detectorName": analysis.DETECTORS["audio_levels"]["name"], "detectorVersion": analysis.DETECTORS["audio_levels"]["version"], "confidence": None, "signalStrength": .5, "explanation": "Fixture measurement.", "suggestionOnly": True}
    output.write_text(json.dumps({"schemaVersion": 1, "assetId": asset_id, "detectorId": "audio_levels", "detectorVersion": analysis.DETECTORS["audio_levels"]["version"], "sourceFingerprint": source_fingerprint, "sourceDurationTicks": 10_000_000, "config": {"windowMs": 250}, "warnings": [], "results": [result]}), encoding="utf-8")
    output_checksum = _sha256_file(output)
    artifact_id = store.register_artifact(asset_id=asset_id, kind="analysis:audio_levels", relative_path=output.relative_to(store.root).as_posix(), source_fingerprint=source_fingerprint, generator_id="localcut-audio_levels", generator_version=analysis.DETECTORS["audio_levels"]["version"], checksum_sha256=output_checksum, config={"proxyArtifactId": proxy_id, "proxyChecksum": _sha256_file(bundle), "settings": {"windowMs": 250}})
    store.add_analysis(asset_id=asset_id, kind="audio_levels", source_in_ticks=1_000_000, source_out_ticks=2_000_000, result=result, generator_id="localcut-audio_levels", generator_version=analysis.DETECTORS["audio_levels"]["version"], source_fingerprint=source_fingerprint, artifact_id=artifact_id)
    config = AppConfig(data_dir=tmp_path / "app-data")
    listed = dispatch("analysis_list", {"path": str(store.root), "assetId": asset_id}, config)
    assert listed["results"][0]["validity"] == "integrity_ok"

    tampered_row = {**result, "explanation": "Modified database copy."}
    with _connect(store.database_path) as db:
        db.execute("UPDATE analyses SET result_json=? WHERE asset_id=? AND kind='audio_levels'", (json.dumps(tampered_row), asset_id))
        db.commit()
    tampered = dispatch("analysis_list", {"path": str(store.root), "assetId": asset_id}, config)
    assert tampered["results"][0]["validity"] == "invalid"
    assert "does not match its validated artifact" in tampered["results"][0]["validityReason"]
    with _connect(store.database_path) as db:
        db.execute("UPDATE analyses SET result_json=? WHERE asset_id=? AND kind='audio_levels'", (json.dumps(result), asset_id))
        db.commit()

    transcript_path = store.root / "cache" / "transcript" / "fixture.json"
    transcript_path.parent.mkdir(parents=True)
    transcript_path.write_text("{}", encoding="utf-8")
    transcript_artifact = store.register_artifact(asset_id=asset_id, kind="transcript", relative_path=transcript_path.relative_to(store.root).as_posix(), source_fingerprint=source_fingerprint, generator_id="fixture-asr", generator_version="1", checksum_sha256=_sha256_file(transcript_path))
    transcript_id = store.add_transcript(asset_id=asset_id, language="en", words=[{"text": "hello", "sourceInTicks": 1_000_000, "sourceOutTicks": 1_200_000}], generator_id="fixture-asr", generator_version="1", source_fingerprint=source_fingerprint, artifact_id=transcript_artifact)
    from localcut_service.analysis import _transcript_review_fingerprint
    review_fingerprint = _transcript_review_fingerprint(store.get_transcript(transcript_id))
    speech_result = {"assetId": asset_id, "sourceInTicks": 1_000_000, "sourceOutTicks": 1_200_000, "detectorId": "speech_regions", "detectorName": analysis.DETECTORS["speech_regions"]["name"], "detectorVersion": analysis.DETECTORS["speech_regions"]["version"], "confidence": .8, "signalStrength": .5, "explanation": "Transcript-derived speech cue.", "suggestionOnly": True}
    speech_path = store.root / "cache" / "analysis" / "speech_regions" / "fixture.json"
    speech_path.parent.mkdir(parents=True)
    speech_path.write_text(json.dumps({"schemaVersion": 1, "assetId": asset_id, "detectorId": "speech_regions", "detectorVersion": analysis.DETECTORS["speech_regions"]["version"], "sourceFingerprint": source_fingerprint, "sourceDurationTicks": 10_000_000, "config": {}, "warnings": [], "results": [speech_result]}), encoding="utf-8")
    speech_config = {"proxyArtifactId": proxy_id, "proxyChecksum": _sha256_file(bundle), "transcriptId": transcript_id, "transcriptArtifactId": transcript_artifact, "transcriptChecksum": _sha256_file(transcript_path), "transcriptReviewFingerprint": review_fingerprint}
    speech_artifact = store.register_artifact(asset_id=asset_id, kind="analysis:speech_regions", relative_path=speech_path.relative_to(store.root).as_posix(), source_fingerprint=source_fingerprint, generator_id="fixture", generator_version="1", checksum_sha256=_sha256_file(speech_path), config=speech_config)
    store.add_analysis(asset_id=asset_id, kind="speech_regions", source_in_ticks=1_000_000, source_out_ticks=1_200_000, result=speech_result, generator_id="fixture", generator_version="1", source_fingerprint=source_fingerprint, artifact_id=speech_artifact)
    before_review = dispatch("analysis_list", {"path": str(store.root), "assetId": asset_id}, config)
    speech_row = next(row for row in before_review["results"] if row["detectorId"] == "speech_regions")
    assert speech_row["validity"] == "integrity_ok"
    store.set_transcript_range_marker(transcript_id, 1_000_000, 1_200_000, "excluded")
    after_review = dispatch("analysis_list", {"path": str(store.root), "assetId": asset_id}, config)
    speech_row = next(row for row in after_review["results"] if row["detectorId"] == "speech_regions")
    assert speech_row["validity"] == "stale"
    assert "reviewed transcript" in speech_row["validityReason"]

    replacement = store.root / "cache" / "proxies" / "replacement-proxy"
    shutil.copytree(bundle, replacement)
    replacement_id = store.register_artifact(asset_id=asset_id, kind="proxy", relative_path=replacement.relative_to(store.root).as_posix(), source_fingerprint=source_fingerprint, generator_id="fixture", generator_version="2", checksum_sha256=_sha256_file(replacement))
    with _connect(store.database_path) as db:
        db.execute("UPDATE artifacts SET created_at='9999-01-01T00:00:00Z' WHERE id=?", (replacement_id,)); db.commit()
    stale = dispatch("analysis_list", {"path": str(store.root), "assetId": asset_id}, config)
    assert stale["results"][0]["validity"] == "stale"
    assert "proxy" in stale["results"][0]["validityReason"]

    (replacement / "proxy.mp4").write_bytes(b"tampered proxy")
    corrupt_proxy = dispatch("analysis_list", {"path": str(store.root), "assetId": asset_id}, config)
    assert corrupt_proxy["results"][0]["validity"] == "stale"
    assert "integrity check" in corrupt_proxy["results"][0]["validityReason"]

    output.write_text("corrupt", encoding="utf-8")
    invalid = dispatch("analysis_list", {"path": str(store.root), "assetId": asset_id}, config)
    assert invalid["results"][0]["validity"] == "invalid"
    assert "integrity check" in invalid["results"][0]["validityReason"]


def test_analysis_list_marks_old_detector_algorithm_stale(prepared, tmp_path):
    store, asset_id, source, bundle, proxy_id = prepared
    detector_id = "silence"
    current_version = int(analysis.DETECTORS[detector_id]["version"])
    previous_version = str(current_version - 1)
    fingerprint = _sha256_file(source)
    result = {
        "assetId": asset_id,
        "sourceInTicks": 1_000_000,
        "sourceOutTicks": 2_000_000,
        "detectorId": detector_id,
        "detectorName": analysis.DETECTORS[detector_id]["name"],
        "detectorVersion": previous_version,
        "confidence": None,
        "signalStrength": 0.5,
        "explanation": "Historical detector suggestion.",
        "suggestionOnly": True,
    }
    output = store.root / "cache" / "analysis" / detector_id / "old-version.json"
    output.parent.mkdir(parents=True)
    output.write_text(json.dumps({"schemaVersion": 1, "assetId": asset_id, "detectorId": detector_id, "detectorVersion": previous_version, "sourceFingerprint": fingerprint, "sourceDurationTicks": 10_000_000, "config": {}, "warnings": [], "results": [result]}), encoding="utf-8")
    artifact = store.register_artifact(
        asset_id=asset_id,
        kind=f"analysis:{detector_id}",
        relative_path=output.relative_to(store.root).as_posix(),
        source_fingerprint=fingerprint,
        generator_id=f"localcut-{detector_id}",
        generator_version=previous_version,
        checksum_sha256=_sha256_file(output),
        config={"proxyArtifactId": proxy_id, "proxyChecksum": _sha256_file(bundle), "settings": {}},
    )
    store.add_analysis(
        asset_id=asset_id,
        kind=detector_id,
        source_in_ticks=1_000_000,
        source_out_ticks=2_000_000,
        result=result,
        generator_id=f"localcut-{detector_id}",
        generator_version=previous_version,
        source_fingerprint=fingerprint,
        artifact_id=artifact,
        config={"proxyArtifactId": proxy_id, "proxyChecksum": _sha256_file(bundle), "settings": {}},
    )

    response = dispatch("analysis_list", {"path": str(store.root), "assetId": asset_id}, AppConfig(data_dir=tmp_path / "app-data"))
    saved = next(item for item in response["results"] if item["detectorId"] == detector_id)
    assert saved["validity"] == "stale"
    assert f"detector version {previous_version}" in saved["validityReason"]
    assert f"current version is {current_version}" in saved["validityReason"]


def test_analysis_list_memoizes_proxy_hash_until_forced_refresh(prepared, tmp_path):
    from localcut_service import server as service_server
    from localcut_service.integrity import IntegrityDigestCache

    store, asset_id, _, _, _ = prepared
    calls = []
    previous_cache = service_server._INTEGRITY_DIGEST_CACHE

    def counting_hasher(path):
        calls.append(path)
        return _sha256_file(path)

    service_server._INTEGRITY_DIGEST_CACHE = IntegrityDigestCache(counting_hasher)
    config = AppConfig(data_dir=tmp_path / "app-data")
    try:
        first = dispatch("analysis_list", {"path": str(store.root), "assetId": asset_id}, config)
        assert first["prerequisites"]["proxyReady"] is True
        first_count = len(calls)
        assert first_count > 0

        second = dispatch("analysis_list", {"path": str(store.root), "assetId": asset_id}, config)
        assert second["prerequisites"]["proxyReady"] is True
        assert len(calls) == first_count

        forced = dispatch("analysis_list", {"path": str(store.root), "assetId": asset_id, "verifyIntegrity": True}, config)
        assert forced["prerequisites"]["proxyReady"] is True
        assert len(calls) > first_count
    finally:
        service_server._INTEGRITY_DIGEST_CACHE = previous_cache


@pytest.mark.parametrize("detector", ["blur", "motion", "thumbnails"])
def test_video_detectors_commit_independent_artifacts_through_job_queue(prepared, tmp_path, detector):
    store, asset_id, _, _, _ = prepared
    class Samples:
        def run(self, args, context, **kwargs):
            kwargs["stderr_capture"].write_text("n: 0 pts_time:0\nn: 1 pts_time:1\nn: 2 pts_time:2\n")
            dark = bytes([0]) * analysis.FRAME_SIZE
            light = bytes([255]) * analysis.FRAME_SIZE
            kwargs["stdout_file"].write_bytes(dark + light + light)
            return b""
    queue = JobQueue(tmp_path / f"jobs-{detector}", handlers={"analysis": AnalysisHandler(ffmpeg="ffmpeg", process=Samples())}, concurrency=1)
    try:
        settings = {"minimumDurationSeconds": 1.5} if detector == "blur" else {"windowSeconds": 1} if detector == "motion" else {"count": 1}
        submitted = queue.submit(analysis_job(store, asset_id, detector, settings))
        completed = queue.wait(submitted["id"], 3)
        assert completed["state"] == "completed", completed.get("error")
        rows = store.list_analyses(asset_id, detector)
        assert rows
        assert all(row["result"]["detectorId"] == detector for row in rows)
        if detector != "thumbnails":
            assert all(row["result"]["timingMethod"] == "proxy_sample_grid" for row in rows)
            assert all(row["result"]["sampleIntervalSeconds"] == 1 for row in rows)
            assert all("cadence-limited estimates" in row["result"]["explanation"] for row in rows)
        with _connect(store.database_path) as db:
            assert db.execute("SELECT COUNT(*) FROM artifacts WHERE kind=? AND status='ready'", (f"analysis:{detector}",)).fetchone()[0] == 1
    finally:
        queue.close()


@pytest.mark.parametrize("input_kind", ["source", "proxy"])
def test_cancel_during_input_integrity_hash_does_not_commit(prepared, tmp_path, monkeypatch, input_kind):
    store, asset_id, source, _, _ = prepared
    active = {}
    real_source_digest = analysis.source_digest
    real_artifact_digest = analysis._sha256_file

    class RememberContext(AnalysisHandler):
        def run(self, context, payload):
            active["context"] = context
            return super().run(context, payload)

    def source_digest(path, context=None):
        if input_kind == "source" and context is not None:
            queue.cancel(context.job_id)
        return real_source_digest(path, context)

    def artifact_digest(path, check_cancelled=None):
        if input_kind == "proxy" and check_cancelled is not None:
            queue.cancel(active["context"].job_id)
        return real_artifact_digest(path, check_cancelled)

    monkeypatch.setattr(analysis, "source_digest", source_digest)
    monkeypatch.setattr(analysis, "_sha256_file", artifact_digest)
    queue = JobQueue(tmp_path / f"cancel-hash-{input_kind}", handlers={"analysis": RememberContext()}, concurrency=1)
    try:
        submitted = queue.submit(analysis_job(store, asset_id, "audio_levels", {}))
        done = queue.wait(submitted["id"], 3)
        assert done["state"] == "cancelled", done.get("error")
        assert store.list_analyses(asset_id) == []
        with _connect(store.database_path) as db:
            assert db.execute("SELECT COUNT(*) FROM artifacts WHERE kind LIKE 'analysis:%'").fetchone()[0] == 0
        assert not active["context"].stage_root.exists()
        assert source.read_bytes() == b"immutable source"
    finally:
        queue.close()


@pytest.mark.parametrize("detector", ["speech_regions", "repeated_takes"])
def test_cancel_during_transcript_grouping_stops_before_next_word(prepared, tmp_path, monkeypatch, detector):
    store, asset_id, source, _, _ = prepared
    transcript_file = store.root / "cache" / "cancellable-transcript.json"
    transcript_file.write_text("{}")
    artifact_id = store.register_artifact(asset_id=asset_id, kind="transcript", relative_path=transcript_file.relative_to(store.root).as_posix(), source_fingerprint=_sha256_file(source), generator_id="fixture", generator_version="1", checksum_sha256=_sha256_file(transcript_file))
    store.add_transcript(asset_id=asset_id, language="en", words=[{"text": "hello", "sourceInTicks": 0, "sourceOutTicks": 100_000}], generator_id="fixture", generator_version="1", source_fingerprint=_sha256_file(source), artifact_id=artifact_id)
    active = {}
    original_get = ProjectStore.get_transcript

    class CancelOnIteration(list):
        def __iter__(self):
            queue.cancel(active["context"].job_id)
            yield self[0]
            raise AssertionError("Transcript grouping continued after cancellation")

    def get_transcript(self, transcript_id):
        value = original_get(self, transcript_id)
        if active.get("reading"):
            value["words"] = CancelOnIteration(value["words"])
        return value

    class RememberStage(AnalysisHandler):
        def run(self, context, payload):
            active["context"] = context
            original_report = context.report

            def report(progress, stage, message=None):
                original_report(progress, stage, message)
                if stage == "reading transcript":
                    active["reading"] = True

            context.report = report
            return super().run(context, payload)

    monkeypatch.setattr(ProjectStore, "get_transcript", get_transcript)
    queue = JobQueue(tmp_path / f"cancel-words-{detector}", handlers={"analysis": RememberStage()}, concurrency=1)
    try:
        submitted = queue.submit(analysis_job(store, asset_id, detector, {}))
        done = queue.wait(submitted["id"], 3)
        assert done["state"] == "cancelled", done.get("error")
        assert store.list_analyses(asset_id) == []
        assert not active["context"].stage_root.exists()
    finally:
        queue.close()


@pytest.mark.parametrize("start,end", [(-1, 100), (100, 100), (200, 100), (0, 10_000_001), (True, 100), (None, 100)])
def test_repeated_take_output_rejects_invalid_comparison_range(tmp_path, start, end):
    detector = "repeated_takes"
    path = tmp_path / "result.json"
    row = analysis._item("asset", detector, 0, 100, "Possible repeated passage.", signal=.9, matchedSourceInTicks=start, matchedSourceOutTicks=end)
    path.write_text(json.dumps({"assetId": "asset", "detectorId": detector, "detectorVersion": analysis.DETECTORS[detector]["version"], "sourceFingerprint": "f" * 64, "sourceDurationTicks": 10_000_000, "results": [row]}))
    with pytest.raises(ValueError, match="matched source range"):
        AnalysisHandler().validate_output(JobOutput(str(path), "cache/result.json", "analysis:repeated_takes"))


def test_changed_video_algorithm_invalidates_only_its_own_cache(prepared, tmp_path, monkeypatch):
    store, asset_id, _, _, _ = prepared
    audio_before = analysis_job(store, asset_id, "audio_levels", {})
    video_before = analysis_job(store, asset_id, "near_black", {})
    queue = JobQueue(tmp_path / "version-cache", concurrency=1, autostart=False)
    try:
        video_key = queue._hash_input(video_before, store.project_id)
        audio_key = queue._hash_input(audio_before, store.project_id)
        monkeypatch.setitem(analysis.DETECTORS["near_black"], "version", "next-algorithm")
        video_after = analysis_job(store, asset_id, "near_black", {})
        audio_after = analysis_job(store, asset_id, "audio_levels", {})
        assert queue._hash_input(video_after, store.project_id) != video_key
        assert queue._hash_input(audio_after, store.project_id) == audio_key
    finally:
        queue.close()


def test_artifact_bundle_hash_can_cancel_between_file_chunks(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "large.bin").write_bytes(b"x" * (3 * 1024 * 1024))
    calls = []

    def check():
        calls.append(True)
        if len(calls) == 5:
            raise JobCancelled("cancel during bundle read")

    expected = _sha256_file(bundle)
    assert _sha256_file(bundle, lambda: None) == expected
    with pytest.raises(JobCancelled):
        _sha256_file(bundle, check)
    assert len(calls) == 5

