"""Optional real-media tests: set LOCAL_CUT_TEST_FFMPEG/FFPROBE to enable."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from localcut_service.jobs import JobQueue
from localcut_service.persistence import ProjectStore, _sha256_file
from localcut_service.proxies import MediaProcess, ProcessFailure, ProxyHandler, proxy_job


@pytest.mark.parametrize("variant", ["audio", "vfr", "rotated", "delayed_audio", "anamorphic", "silent", "nonzero_start"])
def test_real_preview_bundle(tmp_path, variant):
    ffmpeg = os.environ.get("LOCAL_CUT_TEST_FFMPEG")
    ffprobe = os.environ.get("LOCAL_CUT_TEST_FFPROBE")
    if not ffmpeg or not ffprobe:
        pytest.skip("Set LOCAL_CUT_TEST_FFMPEG and LOCAL_CUT_TEST_FFPROBE for integration tests")
    source = tmp_path / "source space & [é] % ' tutorial.mp4"
    args = [ffmpeg, "-v", "error", "-nostdin", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=1.6"]
    if variant == "delayed_audio":
        args += ["-itsoffset", "0.2"]
    args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.4"]
    if variant == "vfr":
        args += ["-vf", "select=not(eq(mod(n\\,3)\\,1))", "-fps_mode", "vfr"]
    if variant == "anamorphic":
        args += ["-vf", "setsar=4/3"]
    if variant == "nonzero_start":
        args += ["-output_ts_offset", "3"]
    if variant == "silent":
        args += ["-an"]
    args += ["-c:v", "libx264", "-c:a", "aac", str(source)]
    subprocess.run(args, check=True, capture_output=True, timeout=30)
    if variant == "rotated":
        rotated = tmp_path / "rotated.mov"
        subprocess.run([ffmpeg, "-v", "error", "-i", str(source), "-c", "copy", "-metadata:s:v:0", "rotate=90", str(rotated)], check=True, capture_output=True, timeout=30)
        source = rotated
    digest = _sha256_file(source)
    store = ProjectStore.create(tmp_path / "project", "Test")
    asset_id = store.add_asset(source)
    profile = {"recommendations": {"proxyPreset": "low"}, "mediaTools": {"ffmpeg": {"version": "integration"}}}
    request = proxy_job(store, asset_id, profile)
    queue = JobQueue(tmp_path / "app", handlers={"proxy": ProxyHandler(ffmpeg, ffprobe)}, concurrency=1)
    try:
        result = queue.wait(queue.submit(request)["id"], timeout=60)
        assert result["state"] == "completed", result
        root = store.root / result["output"]["relativePath"]
        manifest = json.loads((root / "manifest.json").read_text())
        mapping = json.loads((root / "mapping.json").read_text())
        assert (manifest["analysis"] is None) == (variant == "silent")
        assert len(mapping["frames"]) > 5
        assert (root / "waveform.json").is_file() == (variant != "silent")
        assert queue.submit(request)["deduplicated"]
        assert _sha256_file(source) == digest
    finally:
        queue.close()


def test_real_ffmpeg_rejects_corrupt_input(tmp_path):
    ffmpeg = os.environ.get("LOCAL_CUT_TEST_FFMPEG")
    if not ffmpeg:
        pytest.skip("Set LOCAL_CUT_TEST_FFMPEG for integration tests")
    source = tmp_path / "broken & [take].mp4"
    source.write_bytes(b"not a movie")
    class Context:
        def check_cancelled(self): pass
        def report(self, *args): pass
    with pytest.raises(ProcessFailure, match="exit"):
        MediaProcess().run([ffmpeg, "-v", "error", "-nostdin", "-i", str(source), "-f", "null", "-"], Context())
    assert source.read_bytes() == b"not a movie"
