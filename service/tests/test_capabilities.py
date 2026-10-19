from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from localcut_service.capabilities import _parse_ffmpeg_listing, _recommend, collect_capabilities


class MockPlatform:
    def system(self): return "MockOS"
    def release(self): return "1.0"
    def machine(self): return "arm64"
    def processor(self): return "Mock CPU"


def test_recommendations_are_conservative_and_thresholded() -> None:
    assert _recommend(None, None, [], None) == {"modelSizeClass": "small", "proxyPreset": "low", "confidence": "conservative"}
    balanced = _recommend(4, 16 * 1024**3, [], 25 * 1024**3)
    assert balanced["proxyPreset"] == "balanced"
    large = _recommend(8, 32 * 1024**3, [{"dedicatedVramBytes": 8 * 1024**3}], 100 * 1024**3)
    assert large["modelSizeClass"] == "large"


def test_ffmpeg_advertised_codecs_parse_without_claiming_device_support() -> None:
    listing = " V..... h264_nvenc NVIDIA NVENC H.264 encoder\n V....D h264 H.264 decoder\n"
    assert set(_parse_ffmpeg_listing(listing, "encoders")) == {"h264_nvenc", "h264"}
    assert _parse_ffmpeg_listing("Hardware acceleration methods:\ncuda\nd3d11va\n", "hwaccels") == ["cuda", "d3d11va"]


def test_profile_keeps_unknowns_and_isolates_tool_failures(tmp_path: Path) -> None:
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        executable = command[0]
        if executable in {"ffmpeg", "ffprobe"}:
            option = command[1]
            if option == "-version":
                return SimpleNamespace(returncode=0, stdout=f"{executable} version mock", stderr="")
            if option == "-encoders":
                return SimpleNamespace(returncode=0, stdout=" V..... libx264 encoder\n", stderr="")
            if option == "-decoders":
                return SimpleNamespace(returncode=1, stdout="", stderr="error")
            return SimpleNamespace(returncode=0, stdout="Hardware acceleration methods:\ncuda\n", stderr="")
        raise OSError("missing")

    profile = collect_capabilities(
        tmp_path,
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
        runner=runner,
        memory_provider=lambda: (None, None, "unknown"),
        gpu_provider=lambda _runner: [],
        platform_provider=MockPlatform,
    )
    assert profile["platform"]["system"] == "MockOS"
    assert profile["cpu"]["logicalCores"] is not None
    assert profile["memory"]["totalBytes"] is None
    assert profile["gpus"] == []
    assert profile["mediaTools"]["ffmpeg"]["encoders"] == ["libx264"]
    assert "-decoders listing failed" in profile["mediaTools"]["ffmpeg"]["errors"]
    assert "has not been verified" in profile["mediaTools"]["encoderListingSemantics"]
    assert profile["disk"]["freeBytes"] is not None


def test_profile_missing_tools_degrades_without_raising(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("localcut_service.capabilities.shutil.which", lambda _name: None)
    profile = collect_capabilities(tmp_path, memory_provider=lambda: (8, 4, "mock"), gpu_provider=lambda _runner: [], platform_provider=MockPlatform)
    assert profile["mediaTools"]["ffmpeg"]["available"] is False
    assert profile["mediaTools"]["ffprobe"]["available"] is False
    assert profile["recommendations"]["modelSizeClass"] == "small"
