from __future__ import annotations

import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

import localcut_service.reference_traits as reference_traits


def _profile(narrative_structure="unknown"):
    return {
        "pacing": "unknown", "averageShotLengthSeconds": None, "jumpCutFrequencyPerMinute": None,
        "captionDensityPerMinute": None, "punchInFrequencyPerMinute": None, "musicEnergy": "unknown",
        "humorLevel": "unknown", "narrativeStructure": narrative_structure,
    }


def test_reference_profile_uses_only_neutral_traits_and_is_ephemeral(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "private creator reference.mp4"
    source.write_bytes(b"private reference fixture")
    scratch = tmp_path / "scratch"
    observed_args: list[str] = []

    class Probe:
        def __init__(self, executable):
            assert executable == "local-ffprobe"

        def probe(self, path):
            assert path == source.resolve()
            return {"durationSeconds": 120.0}

    class TemporaryDirectory:
        def __init__(self, *, prefix: str):
            assert prefix == "localcut-reference-"
            self.name = scratch

        def __enter__(self):
            scratch.mkdir()
            return str(scratch)

        def __exit__(self, *_args):
            shutil.rmtree(scratch)

    def run(args, **kwargs):
        observed_args.extend(args)
        assert kwargs["timeout"] == 120
        frame_bytes = reference_traits.FRAME_SIZE
        Path(args[-1]).write_bytes(bytes(frame_bytes) + bytes([255]) * frame_bytes)
        return SimpleNamespace(returncode=0, stderr=b"pts_time:0.0\npts_time:1.0\n")

    monkeypatch.setattr(reference_traits, "FFprobe", Probe)
    monkeypatch.setattr(reference_traits.subprocess, "run", run)
    monkeypatch.setattr(reference_traits.tempfile, "TemporaryDirectory", TemporaryDirectory)

    result = reference_traits.analyze_reference(str(source), ffmpeg="local-ffmpeg", ffprobe="local-ffprobe")

    expected_traits = {
        "pacing", "averageShotLengthSeconds", "jumpCutFrequencyPerMinute", "captionDensityPerMinute",
        "punchInFrequencyPerMinute", "musicEnergy", "humorLevel", "narrativeStructure",
    }
    assert set(result["traits"]) == expected_traits
    assert result["traits"]["narrativeStructure"] == "unknown"
    assert result["ephemeral"] is True
    assert str(source) not in json.dumps(result)
    assert not scratch.exists()
    assert "-protocol_whitelist" in observed_args and "file,pipe" in observed_args
    assert "-an" in observed_args


@pytest.mark.parametrize("value", [
    "chronological", "problem-solution", "question-answer", "step-by-step",
    "demonstration-first", "hook-explanation-summary", "story-led-lesson", "before-after", "unknown",
])
def test_neutral_narrative_structure_categories_are_accepted(value: str) -> None:
    result = reference_traits.validate_traits(_profile(value))
    assert result["narrativeStructure"] == value


@pytest.mark.parametrize("value", [
    "Creator X's signature intro", "brand identity", "signature catchphrase", ["problem-solution"], None,
])
def test_narrative_structure_rejects_freeform_or_non_neutral_values(value) -> None:
    with pytest.raises(ValueError, match="neutral structure category"):
        reference_traits.validate_traits(_profile(value))


def test_reference_profile_rejects_identity_or_asset_trait_fields() -> None:
    with pytest.raises(ValueError, match="unsupported trait"):
        reference_traits.validate_traits({"creatorIdentity": "person"})
