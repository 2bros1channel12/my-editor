from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from tools.measure_word_timing import measure


_SOURCE_BYTES = b"annotated source fixture"
_FINGERPRINT = hashlib.sha256(_SOURCE_BYTES).hexdigest()


def _write(path: Path, words: list[dict], fingerprint: str = _FINGERPRINT) -> Path:
    path.write_text(json.dumps({"sourceFingerprint": fingerprint, "words": words}), encoding="utf-8")
    return path


def _measure(tmp_path: Path, reference: Path, hypothesis: Path) -> dict:
    source = tmp_path / "source.mov"
    source.write_bytes(_SOURCE_BYTES)
    return measure(reference, hypothesis, source)


def test_measure_uses_independent_reference_and_service_source_ticks(tmp_path: Path) -> None:
    reference = _write(tmp_path / "reference.json", [
        {"text": "We", "startSeconds": 0.0, "endSeconds": 0.2},
        {"text": "test", "startSeconds": 0.3, "endSeconds": 0.6},
        {"text": "timing.", "startSeconds": 0.7, "endSeconds": 1.0},
    ])
    hypothesis = _write(tmp_path / "transcript.json", [
        {"originalText": "We", "sourceInTicks": 20_000, "sourceOutTicks": 240_000},
        {"originalText": "test", "sourceInTicks": 500_000, "sourceOutTicks": 900_000},
        {"originalText": "timing", "sourceInTicks": 710_000, "sourceOutTicks": 1_030_000},
    ])

    result = _measure(tmp_path, reference, hypothesis)

    assert result["matchedWords"] == 3
    assert result["wordErrorRate"] == 0
    assert result["medianBoundaryErrorMs"] == 30
    assert result["medianStartErrorMs"] == 20
    assert result["medianEndErrorMs"] == 40
    assert "independently supplied" in result["referenceType"]


def test_word_alignment_reports_insertions_and_substitutions_as_standard_wer(tmp_path: Path) -> None:
    reference = _write(tmp_path / "reference.json", [
        {"text": word, "startSeconds": index, "endSeconds": index + 0.5}
        for index, word in enumerate(("a", "bravo", "charlie", "delta"))
    ])
    hypothesis = _write(tmp_path / "transcript.json", [
        {"text": word, "startSeconds": index, "endSeconds": index + 0.5}
        for index, word in enumerate(("a", "wrong", "charlie", "extra", "delta"))
    ])

    result = _measure(tmp_path, reference, hypothesis)

    assert result["matchedWords"] == 3
    assert result["wordErrorRate"] == 0.5  # One substitution and one insertion.
    assert result["timingMatchedWordCount"] == 3


@pytest.mark.parametrize("words", [[], [{"text": "two words", "startSeconds": 0, "endSeconds": 1}]])
def test_word_timing_benchmark_rejects_empty_or_ambiguous_word_records(tmp_path: Path, words) -> None:
    reference = _write(tmp_path / "reference.json", words or [{"text": "known", "startSeconds": 0, "endSeconds": 1}])
    hypothesis = _write(tmp_path / "transcript.json", words)
    with pytest.raises(ValueError):
        _measure(tmp_path, reference, hypothesis)


def test_word_timing_benchmark_rejects_unmatched_text(tmp_path: Path) -> None:
    reference = _write(tmp_path / "reference.json", [{"text": "reference", "startSeconds": 0, "endSeconds": 1}])
    hypothesis = _write(tmp_path / "transcript.json", [{"text": "other", "startSeconds": 0, "endSeconds": 1}])
    with pytest.raises(ValueError, match="No transcript words matched"):
        _measure(tmp_path, reference, hypothesis)


def test_measure_uses_immutable_inference_text_over_edited_review_text(tmp_path: Path) -> None:
    reference = _write(tmp_path / "reference.json", [{"text": "inference", "startSeconds": 0, "endSeconds": 1}])
    hypothesis = _write(tmp_path / "transcript.json", [{
        "originalText": "inference", "text": "corrected", "startSeconds": 0, "endSeconds": 1,
    }])

    result = _measure(tmp_path, reference, hypothesis)

    assert result["wordErrorRate"] == 0


def test_measure_rejects_reference_from_different_source_asset(tmp_path: Path) -> None:
    reference = _write(tmp_path / "reference.json", [{"text": "same", "startSeconds": 0, "endSeconds": 1}], "a" * 64)
    hypothesis = _write(tmp_path / "transcript.json", [{"text": "same", "startSeconds": 0, "endSeconds": 1}], "b" * 64)

    with pytest.raises(ValueError, match="do not match"):
        _measure(tmp_path, reference, hypothesis)


def test_uncertain_timing_remains_visible_but_does_not_count_toward_accuracy(tmp_path: Path) -> None:
    reference = _write(tmp_path / "reference.json", [{"text": "word", "startSeconds": 0, "endSeconds": 1}])
    hypothesis = _write(tmp_path / "transcript.json", [{
        "text": "word", "startSeconds": 0.5, "endSeconds": 1.5, "timingUncertain": True,
    }])

    result = _measure(tmp_path, reference, hypothesis)

    assert result["matchedWords"] == 1
    assert result["timingUncertainMatchedWords"] == 1
    assert result["timingMatchedWordCount"] == 0
    assert result["timingCoverage"] == 0
    assert result["medianBoundaryErrorMs"] is None


def test_measure_requires_valid_source_fingerprints(tmp_path: Path) -> None:
    reference = tmp_path / "reference.json"
    reference.write_text(json.dumps({"sourceFingerprint": "not-a-hash", "words": [{"text": "word", "startSeconds": 0, "endSeconds": 1}]}), encoding="utf-8")
    hypothesis = _write(tmp_path / "transcript.json", [{"text": "word", "startSeconds": 0, "endSeconds": 1}])

    with pytest.raises(ValueError, match="sourceFingerprint"):
        _measure(tmp_path, reference, hypothesis)


def test_measure_verifies_shared_fingerprint_against_source_media_bytes(tmp_path: Path) -> None:
    forged = "b" * 64
    reference = _write(tmp_path / "reference.json", [{"text": "same", "startSeconds": 0, "endSeconds": 1}], forged)
    hypothesis = _write(tmp_path / "transcript.json", [{"text": "same", "startSeconds": 0, "endSeconds": 1}], forged)

    with pytest.raises(ValueError, match="does not match the SHA-256 of --source-media"):
        _measure(tmp_path, reference, hypothesis)
