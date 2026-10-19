from __future__ import annotations

import json
import hashlib
from pathlib import Path
import sys
import time

import pytest

from localcut_service.jobs import JobCancelled, JobQueue
from localcut_service.proxies import MediaProcess, ProcessFailure, ProxyHandler, proxy_job, estimate_bytes, frame_mapping, rotation, settings_for
from localcut_service.persistence import ProjectStore


class Context:
    def __init__(self, cancel_marker: Path | None = None):
        self.cancel_marker = cancel_marker
        self.progress = []

    def check_cancelled(self):
        if self.cancel_marker and self.cancel_marker.exists():
            raise JobCancelled("fixture cancellation")

    def report(self, progress, stage, message=None):
        self.progress.append((progress, stage))


def timestamps(points, timebase="1/1000"):
    return {"streams": [{"codec_type": "video", "time_base": timebase}],
            "frames": [{"best_effort_timestamp": p, "duration": 40,
                        "key_frame": int(i == 0)} for i, p in enumerate(points)]}


def test_vfr_mapping_preserves_exact_rationals_and_nonzero_origin():
    result = frame_mapping(timestamps([9000, 9040, 9110, 9130]),
                           timestamps([0, 80, 220, 260], "1/2000"))
    assert result["sourceMinusProxySeconds"] == "9"
    assert result["sourceTimebase"] == "1/1000"
    assert result["proxyTimebase"] == "1/2000"
    assert [f["sourcePts"] for f in result["frames"]] == [9000, 9040, 9110, 9130]
    assert result["frames"][0]["sourceKeyframe"] is True


@pytest.mark.parametrize("source,proxy,reason", [
    ([0, 40, 110], [0, 40], "dropped or duplicated"),
    ([0, 40, 110], [0, 40, 40], "non-monotonic"),
    ([0, 40, 40], [0, 40, 110], "non-monotonic"),
    ([0, 40, 110], [0, 40, 120], "drift"),
    ([], [], "dropped or duplicated"),
])
def test_unsafe_timestamp_maps_fail(source, proxy, reason):
    with pytest.raises(ValueError, match=reason):
        frame_mapping(timestamps(source), timestamps(proxy))


def test_settings_are_conservative_and_bound_threads():
    assert settings_for({}) == {"preset": "low", "threads": 1}
    assert settings_for({"recommendations": {"proxyPreset": "high"}, "cpu": {"logicalCores": 64}}) == {"preset": "high", "threads": 8}
    assert settings_for({"recommendations": {"proxyPreset": "model supplied nonsense"}, "cpu": {"logicalCores": 1}})["preset"] == "low"


def test_disk_estimate_covers_fixed_overhead_audio_and_larger_presets():
    assert estimate_bytes(0, "low", False) == 64 * 1024**2
    assert estimate_bytes(60, "low", True) > estimate_bytes(60, "low", False)
    assert estimate_bytes(60, "high", False) > estimate_bytes(60, "balanced", False) > estimate_bytes(60, "low", False)
    assert estimate_bytes(120, "low", False) > estimate_bytes(60, "low", False)


def test_rotation_retains_quadrants_and_rejects_arbitrary_angles():
    assert rotation({"side_data_list": [{"rotation": -90}]}) == 270
    assert rotation({"tags": {"rotate": "180"}}) == 180
    with pytest.raises(ValueError, match="right-angle"):
        rotation({"tags": {"rotate": "45"}})


def test_real_process_preserves_unicode_spaces_and_shell_characters(tmp_path):
    path = tmp_path / "tutorial café 演示 & $; ' [draft].mov"
    path.write_bytes(b"original bytes")
    arguments = [str(path), "-literal", "two words", 'quote"inside', "$(echo unsafe)"]
    result = MediaProcess().run([getattr(sys, "_base_executable", sys.executable), "-c", "import json,sys; print(json.dumps(sys.argv[1:]))", *arguments], Context())
    assert json.loads(result) == arguments
    assert path.read_bytes() == b"original bytes"


def test_failed_process_is_not_success_and_reports_exit_status():
    with pytest.raises(ProcessFailure, match="exit 7.*broken codec"):
        MediaProcess().run([getattr(sys, "_base_executable", sys.executable), "-c", "import sys; sys.stderr.write('broken codec'); sys.exit(7)"], Context(), stage="encoding proxy")


@pytest.mark.parametrize("executable", [getattr(sys, "_base_executable", sys.executable), sys.executable])
def test_silent_process_is_cancelled_and_reaped(tmp_path, executable):
    started = tmp_path / "started"
    finished = tmp_path / "should-not-exist"
    script = "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(2); pathlib.Path(sys.argv[2]).write_text('late output')"
    before = time.monotonic()
    with pytest.raises(JobCancelled):
        MediaProcess().run([executable, "-c", script, str(started), str(finished)], Context(started))
    assert time.monotonic() - before < 2
    assert not finished.exists()


def test_timeout_terminates_silent_process():
    with pytest.raises(ProcessFailure, match="timed out"):
        MediaProcess().run([getattr(sys, "_base_executable", sys.executable), "-c", "import time; time.sleep(5)"], Context(), timeout=0.1)


def test_progress_protocol_reports_stage():
    context = Context()
    script = "import time; print('out_time_us=500000',flush=True); time.sleep(0.7)"
    MediaProcess().run([getattr(sys, "_base_executable", sys.executable), "-c", script], context, stage="encoding", duration=1, base=0.1, span=0.6)
    assert (0.4, "encoding") in context.progress


def test_mocked_rotated_vfr_bundle_validates_and_detects_tampering(tmp_path):
    source = tmp_path / "portrait café & [take 1].mov"
    original_bytes = b"source bytes never overwritten"
    source.write_bytes(original_bytes)
    store = ProjectStore.create(tmp_path / "project", "Fixture")
    asset = store.add_asset(source)
    job = proxy_job(store, asset, {})
    assert job.pipeline_version == "preview-2"
    context = Context()
    context.job_id = "test-job"
    context.input = job
    context.staging_file = lambda suffix: tmp_path / ("stage" + suffix)

    class FakeProcess:
        def __init__(self):
            self.commands = []

        def run(self, args, context, **kwargs):
            self.commands.append(args)
            if args[0] == "ffprobe-fixture":
                is_source = args[-1] == str(source)
                result = timestamps([9000, 9040, 9110] if is_source else [0, 40, 110])
                result["streams"][0].update(index=0, width=1920 if is_source else 406,
                    height=1080 if is_source else 720, sample_aspect_ratio="1:1" if is_source else "405:406",
                    start_time="9" if is_source else "0", side_data_list=[{"rotation": 90 if is_source else 0}])
                result["format"] = {"duration": "0.15"}
                return json.dumps(result).encode()
            output = args[-1]
            if output != "-":
                Path(output).write_bytes(b"\x89PNG\r\n\x1a\nfixture" if output.endswith(".png") else b"validated proxy fixture")
            return b""

    process = FakeProcess()
    handler = ProxyHandler("ffmpeg-fixture", "ffprobe-fixture", process)
    output = handler.run(context, handler.validate_input(job.payload))
    handler.validate_output(output)
    root = Path(output.staging_path)
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["sourceRotation"] == 90
    assert manifest["sourceFingerprint"] == hashlib.sha256(original_bytes).hexdigest()
    assert manifest["analysis"] is None
    assert len(manifest["thumbnails"]) == 3
    assert json.loads((root / "mapping.json").read_text())["sourceMinusProxySeconds"] == "9"
    assert json.loads((root / "mapping.json").read_text())["sourceStartSeconds"] == "9"
    assert source.read_bytes() == original_bytes
    encode = next(a for a in process.commands if a[-1].endswith("proxy.mp4") and a[0] == "ffmpeg-fixture")
    assert encode[encode.index("-i") + 1] == str(source)
    assert encode[encode.index("-fps_mode") + 1] == "passthrough"
    (root / "proxy.mp4").write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="checksum"):
        handler.validate_output(output)


@pytest.mark.parametrize("failure", ["encode", "cancel", "disk"])
def test_failed_generation_never_registers_partial_bundle(tmp_path, monkeypatch, failure):
    from collections import namedtuple
    from localcut_service.persistence import _connect
    source = tmp_path / "source.mov"
    source.write_bytes(b"original")
    store = ProjectStore.create(tmp_path / "project", "Test")
    asset = store.add_asset(source)
    request = proxy_job(store, asset, {})
    class FailingProcess:
        def run(self, args, context, **kwargs):
            if args[0] == "probe":
                return json.dumps({"streams": [{"codec_type": "video", "index": 0, "time_base": "1/1000", "width": 320, "height": 180}], "format": {"duration": "1"}}).encode()
            Path(args[-1]).write_bytes(b"partial")
            if failure == "cancel":
                context.queue.cancel(context.job_id)
                context.check_cancelled()
            raise ProcessFailure("encoding failed (exit 1): invalid codec")
    if failure == "disk":
        monkeypatch.setattr("localcut_service.proxies.shutil.disk_usage", lambda _: namedtuple("Disk", "total used free")(100, 99, 1))
    queue = JobQueue(tmp_path / "app", handlers={"proxy": ProxyHandler("ffmpeg", "probe", FailingProcess())})
    try:
        result = queue.wait(queue.submit(request)["id"])
        assert result["state"] == ("cancelled" if failure == "cancel" else "failed")
        assert not (store.root / ".localcut-staging" / result["id"]).exists()
        with _connect(store.database_path) as db:
            assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
        assert source.read_bytes() == b"original"
    finally:
        queue.close()


def test_changed_source_cannot_reuse_import_fingerprint(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"before")
    store = ProjectStore.create(tmp_path / "project", "Test")
    job = proxy_job(store, store.add_asset(source), {})
    source.write_bytes(b"after")
    with pytest.raises(ValueError, match="changed since import"):
        ProxyHandler().validate_job(job)

