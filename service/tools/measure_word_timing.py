"""Compare local word timestamps with an independently annotated reference."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
from typing import Any

_WORD = re.compile(r"[a-z0-9]+(?:['’][a-z0-9]+)*", re.IGNORECASE)


def _tokens(text: Any) -> list[str]:
    if not isinstance(text, str):
        return []
    return [token.casefold().replace("’", "'") for token in _WORD.findall(str(text))]


def _time_range(word: dict[str, Any], *, reference: bool) -> tuple[float, float]:
    if "sourceInTicks" in word and "sourceOutTicks" in word:
        raw_start, raw_end = word["sourceInTicks"], word["sourceOutTicks"]
        if any(isinstance(value, bool) or type(value) is not int for value in (raw_start, raw_end)):
            raise ValueError("Service source ticks must be integer values.")
        start, end = raw_start / 1_000_000, raw_end / 1_000_000
    elif "startSeconds" in word and "endSeconds" in word:
        start, end = word["startSeconds"], word["endSeconds"]
    else:
        label = "Reference" if reference else "Hypothesis"
        raise ValueError(f"{label} word needs sourceInTicks/sourceOutTicks or startSeconds/endSeconds.")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in (start, end)) or start < 0 or end <= start:
        label = "Reference" if reference else "Hypothesis"
        raise ValueError(f"{label} word has an invalid time range.")
    return float(start), float(end)


def _read_words(path: Path, *, reference: bool) -> tuple[list[dict[str, Any]], str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("words") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError(f"{path}: expected a JSON object with a words array.")
    fingerprint = data.get("sourceFingerprint")
    if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise ValueError(f"{path}: sourceFingerprint must be the lowercase SHA-256 of the annotated source asset.")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"{path}: each word must be an object.")
        # Database transcript views expose corrected `text` alongside immutable
        # `originalText`; timing accuracy measures the original ASR hypothesis.
        text = row.get("originalText", row.get("text"))
        tokens = _tokens(text)
        if len(tokens) != 1:
            raise ValueError(f"{path}: each word record must normalize to exactly one token: {text!r}.")
        start, end = _time_range(row, reference=reference)
        result.append({"token": tokens[0], "start": start, "end": end, "timingUncertain": bool(row.get("timingUncertain", False))})
    if not result:
        raise ValueError(f"{path}: words array is empty.")
    return result, fingerprint


def _align(reference: list[dict[str, Any]], hypothesis: list[dict[str, Any]]) -> tuple[list[tuple[int, int]], int]:
    """Levenshtein alignment; return only exact normalized token matches."""
    n, m = len(reference), len(hypothesis)
    costs = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1): costs[i][0] = i
    for j in range(1, m + 1): costs[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            substitution = costs[i - 1][j - 1] + (reference[i - 1]["token"] != hypothesis[j - 1]["token"])
            costs[i][j] = min(substitution, costs[i - 1][j] + 1, costs[i][j - 1] + 1)
    matches = []
    i, j = n, m
    while i or j:
        if i and j and reference[i - 1]["token"] == hypothesis[j - 1]["token"] and costs[i][j] == costs[i - 1][j - 1]:
            matches.append((i - 1, j - 1)); i -= 1; j -= 1
        elif i and j and costs[i][j] == costs[i - 1][j - 1] + 1:
            i -= 1; j -= 1
        elif i and costs[i][j] == costs[i - 1][j] + 1:
            i -= 1
        else:
            j -= 1
    return list(reversed(matches)), costs[n][m]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def measure(reference_path: Path, hypothesis_path: Path, source_media_path: Path) -> dict[str, Any]:
    reference, reference_fingerprint = _read_words(reference_path, reference=True)
    hypothesis, hypothesis_fingerprint = _read_words(hypothesis_path, reference=False)
    if reference_fingerprint != hypothesis_fingerprint:
        raise ValueError("Reference and hypothesis sourceFingerprint values do not match; compare timestamps from the same source asset.")
    actual_fingerprint = _sha256_file(source_media_path)
    if reference_fingerprint != actual_fingerprint:
        raise ValueError("sourceFingerprint does not match the SHA-256 of --source-media; verify both annotations use this exact source file.")
    matches, errors = _align(reference, hypothesis)
    reference_count = len(reference)
    matched_errors = []
    start_errors = []
    end_errors = []
    uncertain_matches = 0
    for ref_index, hyp_index in matches:
        ref, hyp = reference[ref_index], hypothesis[hyp_index]
        start_error = abs(ref["start"] - hyp["start"])
        end_error = abs(ref["end"] - hyp["end"])
        if hyp["timingUncertain"]:
            uncertain_matches += 1
            continue
        start_errors.append(start_error)
        end_errors.append(end_error)
        matched_errors.append((start_error + end_error) / 2)
    if not matches:
        raise ValueError("No transcript words matched the reference; timing error cannot be measured.")

    def percentile_ms(values: list[float], percentile: float) -> float:
        ordered = sorted(values)
        index = min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1)
        return round(ordered[index] * 1000, 2)

    return {
        "reference": str(reference_path),
        "hypothesis": str(hypothesis_path),
        "sourceMedia": str(source_media_path),
        "sourceFingerprint": actual_fingerprint,
        "referenceWordCount": reference_count,
        "hypothesisWordCount": len(hypothesis),
        "matchedWords": len(matches),
        "wordErrorRate": round(errors / reference_count, 4),
        "timingMatchedWordCount": len(matched_errors),
        "timingUncertainMatchedWords": uncertain_matches,
        "timingCoverage": round(len(matched_errors) / reference_count, 4),
        "timingCoverageDefinition": "timingMatchedWordCount divided by referenceWordCount",
        "medianBoundaryErrorMs": round(statistics.median(matched_errors) * 1000, 2) if matched_errors else None,
        "p90BoundaryErrorMs": percentile_ms(matched_errors, 0.9) if matched_errors else None,
        "medianStartErrorMs": round(statistics.median(start_errors) * 1000, 2) if start_errors else None,
        "medianEndErrorMs": round(statistics.median(end_errors) * 1000, 2) if end_errors else None,
        "boundaryErrorDefinition": "per exact text match with timingUncertain=false, mean of absolute start and end boundary error; median across those words",
        "referenceType": "independently supplied word-level annotation; not inferred from the hypothesis",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path, help="JSON with words [{text,startSeconds,endSeconds}, ...]")
    parser.add_argument("hypothesis", type=Path, help="Service transcript JSON with source ticks, or normalized words with second offsets")
    parser.add_argument("--source-media", type=Path, required=True, help="The exact source media file; its SHA-256 is checked against both JSON fingerprints")
    parser.add_argument("--max-median-error-ms", type=float, default=250.0, help="Optional acceptance threshold; default 250 ms")
    args = parser.parse_args()
    if not math.isfinite(args.max_median_error_ms) or args.max_median_error_ms < 0:
        parser.error("--max-median-error-ms must be finite and nonnegative")
    try:
        result = measure(args.reference, args.hypothesis, args.source_media)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.error(str(exc))
    result["targetMedianBoundaryErrorMs"] = args.max_median_error_ms
    result["targetMet"] = result["medianBoundaryErrorMs"] is not None and result["medianBoundaryErrorMs"] <= args.max_median_error_ms
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["targetMet"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
