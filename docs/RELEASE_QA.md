# Release QA evidence

This is service/pipeline validation on the available Windows x64 development host, not approval of a signed installer or a formative user study. The host reports 24 logical CPU cores (Intel family 6 model 151), approximately 31.8 GiB physical RAM, and no reliably reported GPU. Existing project documentation identifies this host as an Intel i9-12900K. CPU inventory confidence is unknown, so GPU acceleration is not assumed.

## Integrated workflow

`service/tests/test_release_workflow.py` creates six seconds of synthetic 320×180/24 fps footage and exercises linked import, real FFmpeg proxies, a replaceable fixture speech adapter through the normal persistent transcription job, all ten independent detectors, exact transcript indexing/search, brief save, a structured fixture planner through the normal planning job, explicit written-plan approval, timeline compile, manual gain and conversational source-protection revisions, editable caption generation, reopening project storage and the persistent queue, a real 480p/24 fps burned-caption normalized render, and OTIO export.

The test verifies source SHA-256 remains unchanged, linked media is not copied, selections remain within source bounds, unapproved sections cannot compile, compile/manual/conversational operations can undo/redo, captions survive reopening, and adversarial transcript text remains search/planner evidence rather than an executable operation. Fixture inference is explicitly named and cannot establish real speech accuracy or model selection quality.

Measured wall times for the initial integrated run:

| Stage | Seconds |
| --- | ---: |
| Proxy generation | 3.340 |
| Fixture speech transcription | 1.601 |
| All ten detectors | 4.796 |
| Exact index | 0.441 |
| Fixture written planning | 0.335 |
| Real FFmpeg export | 1.069 |
| Complete test including source creation/review/reopen | 13.014 |

These are short synthetic pipeline measurements on this host. They are not representative-footage, minimum-hardware, or large-project performance guarantees.

## Regression and offline checks

Final settled-source regression rechecked on 2026-10-22: **520 passed, 14 skipped in 102.07 seconds** using `.venv/Scripts/python.exe -m pytest -q --junitxml=../release-service-results.xml` from `service/`. The optional gates are tested separately below. One pytest warning notes that `record_property` metadata is not part of the default xUnit2 schema; this does not affect the test assertions. The local XML file preserves the current aggregate test evidence. This supersedes the earlier 512-test run for current service regression counts.

The primary agent additionally ran the full UI suite (**93 passed**) and Rust service-boundary suite (**8 passed**) during this release pass. Native installer execution and visible desktop interaction are separate from those unit checks.

- Full service suite: **512 passed, 14 skipped in 109.15 seconds**, using `PYTHONPATH=<repository>/service` when invoking pytest from the repository root. The documented `cd service; python -m pytest` also makes the benchmark tooling importable. Skips reflect optional real-tool/model/long-benchmark gates.
- Explicit real-tool check: **5 passed, 2 skipped in 15.83 seconds**, including the real Whisper short sample, network-denied core/media service workflow, network-denied actual Whisper, and real FFmpeg caption/export test. At this stage the gated 20-minute ASR and 10-minute render tests were not enabled; later benchmark results should be recorded separately.
- The complete new 13-stage workflow was also rerun in a fresh Python process under the same external-DNS/socket-denying audit hook: **1 passed in 16.01 seconds** (15.763 seconds inside the workflow). Loopback remains allowed. This extends the process-level offline check through review, revisions, captions, restart/reopen and OTIO, with the same explicit fixture inference limitations.
- Real Whisper CPU smoke: the pinned tiny model returned English and the expected 22 words from the 11-second upstream JFK fixture in **1.974 seconds**. Backend binary SHA-256: `95e3c0b0e778ad9499eb0125f97c1dcf437dd9eb4ea77050b043574f93c2631d`; weights revision: `5359861c739e955e79d9a303bcbc70fb988958b1`. Fixtures already existed locally; no model download was needed for QA.

Existing focused suites cover cancellation cleanup, process restart recovery, transient retry, atomic publication, invalid output rejection, disk exhaustion, source-boundary validation, adversarial structured output, and preservation of prior completed renders. Passing these checks does not simulate power loss or storage hardware failure.

## Long synthetic benchmarks

The opt-in 20-minute 1080p30 looped-speech benchmark passed with the same local Whisper binary and pinned tiny model. Synthetic source generation took **64.48 seconds**; proxy generation started **0.069 seconds** after submission; actual CPU transcription returned **2,412 English words in 38.00 seconds**; silence/filler/shot-boundary analysis took **5.97 seconds**; and the deterministic first-cut proposal committed one pending reviewable operation in **0.49 seconds**. The source SHA-256 remained unchanged. This validates synthetic throughput against the five-minute transcription and eight-minute first-cut gates on this host, not independent timing accuracy or representative-footage selection quality. Other local build/QA work ran concurrently, so measurements are host observations rather than isolated benchmark comparisons.

Run with existing local fixtures and `LOCAL_CUT_RUN_20MIN_ASR=1`; no model acquisition is performed by the benchmark. The current combined opt-in invocation of the 20-minute ASR benchmark plus short and **10-minute 1080p30 clean/burned render tests passed all 3 tests in 414.87 seconds**. The long render verified H.264/AAC codecs, complete decode, accepted SRT text, visible burned captions, duration, audio/video start and end alignment within one output frame, and unchanged original media. This is endpoint alignment on a synthetic fixture, not independently reviewed content-level lip-sync.

The current real proxy integration suite also passed **8 tests in 14.58 seconds**, covering the existing ordinary/audio, VFR, rotation, delayed-audio, anamorphic, silent-video, nonzero-timestamp, unusual-path and corrupt-input fixture cases. These use real local FFmpeg/FFprobe.

## Release gates that require additional evidence

- A packaged desktop workflow under OS-level outbound network denial, including install/uninstall and native WebView playback.
- Independently annotated word boundaries and representative speech/footage for transcription, timestamp, selection, caption and lip-sync quality.
- Five-target-user formative acceptance and suggested-cut acceptance rates.
- Performance and reliability on the minimum supported hardware, and a representative long multiclip project.
- Distribution/signing and third-party license obligations review as described in `PACKAGING.md`.

The process-level offline tests restrict Python DNS/socket access; they do not establish OS-wide network containment for FFmpeg, native inference binaries, or the packaged GUI. No release claim should erase that distinction.
