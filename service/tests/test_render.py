from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from localcut_service.jobs import JobQueue
from localcut_service.persistence import ProjectStore, _connect
from localcut_service.render import RenderHandler, _validate_av_end_alignment, _validate_av_start_alignment, list_exports, render_job


def _asset(store: ProjectStore, path: Path, *, with_audio: bool) -> str:
    path.write_bytes((path.name * 50).encode("utf-8"))
    metadata = {
        "durationTicks": 4_000_000,
        "video": {"width": 640, "height": 360, "avgFrameRate": {"value": 30}},
        "audio": [{"index": 1, "codec": "aac"}] if with_audio else [],
    }
    return store.import_asset_reference(path, metadata, probe_tool="fixture", probe_version="1", duration_ticks=4_000_000)["assetId"]


class RecordingProcess:
    def __init__(self):
        self.render_args: list[str] | None = None

    def run(self, args, context, **kwargs):
        if "-filter_complex" in args:
            self.render_args = list(args)
            Path(args[-1]).write_bytes(b"x" * 2048)
        return b""


class FixtureRenderHandler(RenderHandler):
    def __init__(self, *args, no_audio_path: str | None = None, audio_duration: str = "4", **kwargs):
        super().__init__(*args, **kwargs)
        self.no_audio_path = no_audio_path
        self.audio_duration = audio_duration

    def _probe(self, path: Path, context):
        audio = [] if self.no_audio_path and path.name == self.no_audio_path else [{"index": 1, "codec_type": "audio", "codec_name": "aac", "disposition": {"default": 1}}]
        return {
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "avg_frame_rate": "30/1", "sample_aspect_ratio": "1:1", "start_time": "0", "duration": "4", "side_data_list": []},
                *[{**stream, "start_time": "0", "duration": self.audio_duration} for stream in audio],
            ],
            "format": {"duration": "4"},
        }


def _project(tmp_path: Path, *, with_audio: bool = True):
    store = ProjectStore.create(tmp_path / "project with spaces", "Audio bed")
    video_path = tmp_path / "talking head [snow 雪].mp4"
    bed_path = tmp_path / "licensed music bed.mov"
    video_id = _asset(store, video_path, with_audio=True)
    bed_id = _asset(store, bed_path, with_audio=with_audio)
    clip = {"id": "clip-1", "assetId": video_id, "itemType": "clip", "trackId": "video", "timelineStartTicks": 0, "timelineDurationTicks": 4_000_000, "sourceInTicks": 0, "sourceOutTicks": 4_000_000, "properties": {"linkedAudio": True, "videoFadeInTicks": 500_000, "videoFadeOutTicks": 600_000}}
    bed = {"id": "bed-1", "assetId": bed_id, "itemType": "audio", "trackId": "music", "timelineStartTicks": 1_000_000, "timelineDurationTicks": 2_000_000, "sourceInTicks": 500_000, "sourceOutTicks": 2_500_000, "properties": {"role": "music-bed", "userSupplied": True, "audioGainDb": -18, "fadeInTicks": 500_000, "fadeOutTicks": 400_000}}
    store.autosave(store.project_id, {"timelineItems": [clip, bed]})
    fake_ffmpeg = tmp_path / "ffmpeg.exe"; fake_ffmpeg.write_bytes(b"ffmpeg tool fixture")
    fake_ffprobe = tmp_path / "ffprobe.exe"; fake_ffprobe.write_bytes(b"ffprobe tool fixture")
    return store, video_path, bed_path, fake_ffmpeg, fake_ffprobe


def test_audio_bed_is_in_render_snapshot_and_mixed_at_requested_time(tmp_path):
    store, video_path, bed_path, ffmpeg, ffprobe = _project(tmp_path)
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (video_path, bed_path)}
    process = RecordingProcess()
    handler = FixtureRenderHandler(str(ffmpeg), str(ffprobe), process=process)
    queue = JobQueue(tmp_path / "jobs", handlers={"rendering": handler}, concurrency=1)
    try:
        job = render_job(store, str(ffmpeg), str(ffprobe))
        assert len(job.config["audioItems"]) == 1
        result = queue.submit(job)
        completed = queue.wait(result["id"], 5)
        assert completed["state"] == "completed", completed.get("error")
        assert process.render_args is not None
        graph = process.render_args[process.render_args.index("-filter_complex") + 1]
        assert "[1:1]atrim=start=0.500000:end=2.500000" in graph
        assert "volume=-18.000dB" in graph
        assert "afade=t=in:d=0.500000" in graph
        assert "afade=t=out:st=1.600000:d=0.400000" in graph
        assert "adelay=delays=1000:all=1" in graph
        assert "amix=inputs=2" in graph and "alimiter=limit=0.95" in graph
        assert "fade=t=in:st=0:d=0.500000:color=black" in graph
        assert "fade=t=out:st=3.400000:d=0.600000:color=black" in graph
        assert process.render_args[process.render_args.index("-map", process.render_args.index("-map") + 1) + 1] == "[amixed]"
        assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (video_path, bed_path)} == before
        with _connect(store.database_path) as db:
            assert db.execute("SELECT COUNT(*) FROM exports WHERE project_id=? AND status='completed'", (store.project_id,)).fetchone()[0] == 1
            saved_settings = json.loads(db.execute("SELECT settings_json FROM exports WHERE project_id=? AND status='completed'", (store.project_id,)).fetchone()[0])
            assert saved_settings["audioVideoStartAlignmentValidated"] is True
            assert saved_settings["audioVideoStartOffsetSeconds"] == pytest.approx(0)
            assert saved_settings["audioVideoEndAlignmentValidated"] is True
            assert saved_settings["audioVideoEndOffsetSeconds"] == pytest.approx(0)
    finally:
        queue.close()


def test_audio_bed_without_audio_stream_fails_without_publishing_export(tmp_path):
    store, _, bed_path, ffmpeg, ffprobe = _project(tmp_path, with_audio=False)
    process = RecordingProcess()
    handler = FixtureRenderHandler(str(ffmpeg), str(ffprobe), process=process, no_audio_path=bed_path.name)
    queue = JobQueue(tmp_path / "jobs-no-audio", handlers={"rendering": handler}, concurrency=1)
    try:
        job = render_job(store, str(ffmpeg), str(ffprobe))
        result = queue.submit(job)
        failed = queue.wait(result["id"], 5)
        assert failed["state"] == "failed"
        assert "no audio track" in failed["error"]["message"]
        assert process.render_args is None
        with _connect(store.database_path) as db:
            assert db.execute("SELECT COUNT(*) FROM exports WHERE project_id=?", (store.project_id,)).fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='export:mp4' AND status='ready'").fetchone()[0] == 0
    finally:
        queue.close()


def test_end_alignment_failure_preserves_previous_successful_export(tmp_path):
    store, _, _, ffmpeg, ffprobe = _project(tmp_path)
    handler = FixtureRenderHandler(str(ffmpeg), str(ffprobe), process=RecordingProcess())
    queue = JobQueue(tmp_path / "jobs-preserve-export", handlers={"rendering": handler}, concurrency=1)
    try:
        first_job = queue.submit(render_job(store, str(ffmpeg), str(ffprobe)))
        first_done = queue.wait(first_job["id"], 5)
        assert first_done["state"] == "completed", first_done.get("error")
        previous = list_exports(store)[0]
        previous_path = store.root / previous["relativePath"]
        previous_bytes = previous_path.read_bytes()
        previous_checksum = previous["checksumSha256"]

        edited_state = store.load_state()
        edited_state["timelineItems"][0]["properties"]["audioGainDb"] = -1
        store.autosave(store.project_id, edited_state)
        handler.audio_duration = "4.034"
        second_job = queue.submit(render_job(store, str(ffmpeg), str(ffprobe)))
        second_done = queue.wait(second_job["id"], 5)
        assert second_done["state"] == "failed"
        assert "end times differ" in second_done["error"]["message"]
        assert previous_path.read_bytes() == previous_bytes
        exports = list_exports(store)
        assert len(exports) == 1
        assert exports[0]["checksumSha256"] == previous_checksum
        assert exports[0]["relativePath"] == previous["relativePath"]
    finally:
        queue.close()


@pytest.mark.parametrize("value", [True, -1, 4_000_001, "100000", None])
def test_bounded_timeline_settings_reject_malformed_values(value):
    from localcut_service.render import _bounded_ticks

    with pytest.raises(ValueError, match="nonnegative integer"):
        _bounded_ticks(value, "Clip fade", 4_000_000)


@pytest.mark.parametrize("value", ["loud", None, True, float("nan"), 25])
def test_audio_gain_validation_rejects_malformed_values(value):
    from localcut_service.render import _validated_gain

    with pytest.raises(ValueError, match="gain"):
        _validated_gain(value, "Clip audio gain")


def test_render_av_start_alignment_accepts_offsets_within_one_frame():
    offset = _validate_av_start_alignment({"streams": [
        {"codec_type": "video", "start_time": "0.000000"},
        {"codec_type": "audio", "start_time": "0.033333"},
    ]}, 30)
    assert offset == pytest.approx(0.033333)


def test_render_av_start_alignment_rejects_offsets_over_one_frame():
    with pytest.raises(ValueError, match="more than one 30 fps frame"):
        _validate_av_start_alignment({"streams": [
            {"codec_type": "video", "start_time": "0.000000"},
            {"codec_type": "audio", "start_time": "0.034"},
        ]}, 30)


@pytest.mark.parametrize("probe", [
    {"streams": [{"codec_type": "video", "start_time": "0"}]},
    {"streams": [{"codec_type": "video", "start_time": "0"}, {"codec_type": "audio"}]},
    {"streams": [{"codec_type": "video", "start_time": "NaN"}, {"codec_type": "audio", "start_time": "0"}]},
])
def test_render_av_start_alignment_fails_closed_without_valid_stream_times(probe):
    with pytest.raises(ValueError, match="synchronization|streams"):
        _validate_av_start_alignment(probe, 30)


def test_render_av_end_alignment_accepts_stream_end_offsets_within_one_frame():
    offset = _validate_av_end_alignment({"streams": [
        {"codec_type": "video", "start_time": "0", "duration": "4"},
        {"codec_type": "audio", "start_time": "0", "duration": "4.033"},
    ]}, 30)
    assert offset == pytest.approx(0.033)


def test_render_av_end_alignment_rejects_cumulative_drift_over_one_frame():
    with pytest.raises(ValueError, match="end times differ.*more than one 30 fps frame"):
        _validate_av_end_alignment({"streams": [
            {"codec_type": "video", "start_time": "0", "duration": "600"},
            {"codec_type": "audio", "start_time": "0", "duration": "600.034"},
        ]}, 30)


@pytest.mark.parametrize("probe", [
    {"streams": [{"codec_type": "video", "start_time": "0", "duration": "1"}]},
    {"streams": [{"codec_type": "video", "start_time": "0"}, {"codec_type": "audio", "start_time": "0", "duration": "1"}]},
    {"streams": [{"codec_type": "video", "start_time": "0", "duration": "NaN"}, {"codec_type": "audio", "start_time": "0", "duration": "1"}]},
])
def test_render_av_end_alignment_fails_closed_without_valid_stream_durations(probe):
    with pytest.raises(ValueError, match="synchronization|streams|durations"):
        _validate_av_end_alignment(probe, 30)
