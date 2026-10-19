# Local transcription and review

## Decisions from independent reviews

The inference reviewer recommended process-isolated ASR, full job-envelope provenance, conservative available-RAM checks, and explicit model acquisition. The transcript reviewer recommended source microseconds, analysis-audio offsets, immutable inference, separate review edits, and atomic registration with artifact publication. Their recommendations agreed. The remaining tradeoff was an embedded Python ASR package versus an external native CLI: Python 3.14 native dependency compatibility and reliable cancellation favored whisper.cpp. Both reviewers supplied findings before their implementation turns stopped at the account usage limit; the primary agent completed integration and tests.

The replaceable `SpeechBackend` protocol accepts local analysis audio/model paths, an output prefix, cancellation/progress context, and language mode. `WhisperCppBackend` uses a structured argument array with `shell=False` through the existing guarded process runner. It requests full JSON token timestamps, automatic language detection, bounded CPU threads and CPU inference. GPU inventory alone does not establish driver/backend compatibility. A different backend can be injected into `TranscriptionHandler` without changing persistence or review UI.

## Model setup and selection

Set `LOCAL_CUT_WHISPER_CLI` to an installed `whisper-cli.exe` and restart the app. Keep the release DLLs beside the executable. No executable or model is bundled. The adapter was exercised with the official Windows CPU v1.9.2 release. Its SHA-256 is recorded as the inference backend build identity; model name, repository revision, weight checksum, source hash and pipeline version identify cached results. A binary content hash is used because CLI version output is not a stable cross-build contract.

Models are multilingual GGML tiny/base/small from [ggerganov/whisper.cpp](https://huggingface.co/ggerganov/whisper.cpp/tree/5359861c739e955e79d9a303bcbc70fb988958b1), pinned to revision `5359861c739e955e79d9a303bcbc70fb988958b1`. Sizes and SHA-256 values come from that revision's LFS metadata, not branch names. Weights and whisper.cpp use MIT licensing; preserve their upstream license notices in release packaging. The application only downloads weights following the user's download action. Inference never downloads dependencies or uploads content.

| Model | Download bytes | Minimum available RAM guard | Suggested when available RAM is |
|---|---:|---:|---:|
| tiny | 77,691,713 | 2 GiB | unknown or below 5 GiB |
| base | 147,951,465 | 3 GiB | at least 5 GiB |
| small | 487,601,967 | 5 GiB | at least 8 GiB |

These are conservative resource guards, not benchmarks. CPU thread count is half the reported logical CPUs, capped at four. ASR jobs share one cancellable inference slot even when the general job pool has greater concurrency. Memory is checked again after waiting for that slot. If memory is unknown, tiny is recommended and inference failure still provides instructions to close other applications, choose tiny, and retry. There is no automatic model downgrade that would hide a changed inference configuration.

Downloads reserve model size plus 256 MiB free disk space, stream into job staging, and verify exact byte count and SHA-256 before publication. Corrupt existing model files are retained as `invalid-<uuid>.bin` beside the model for manual inspection/removal, permitting a fresh download without overwriting a valid model. Download cancellation is checked between 256 KiB reads; a blocked network operation can take up to the 15-second socket timeout to unwind. Interrupted downloads restart from zero under the queue's retry/recovery policy. Transient connection failures retry; integrity failures require an explicit retry. Completed downloads show verified status; jobs show progress, stage, failure and cancellation.

## Workflow and interfaces

1. Import an MP4/MOV into an open project and generate preview assets.
2. Models shows recommendation, bytes, RAM guard, backend availability and download status. Explicitly download and verify the selected weights.
3. Editor selects a source and starts local transcription. Missing/invalid preview audio and unavailable/changed sources produce actionable errors.
4. Review synchronized proxy video, click a segment or word to seek, and search the current corrected segment text.
5. Save segment corrections explicitly or restore the original inference. Corrections never rewrite original inferred text or word timings.
6. Select word checkboxes. Their minimum start and maximum end define one contiguous source range, including any gaps. Mark required, excluded or uncertain; seek to or remove existing markers. Overlapping markers are retained as review intent; conflict resolution belongs to later edit planning.

Methods are included in the TypeScript method union, both Rust allowlists and the JSON protocol schema: `models_status`, `model_download`, `transcription_generate`, `transcript_list`, `transcript_edit`, `transcript_marker`. Responses use typed transcript, segment, word, marker and model views. Queue progress is also shown in Editor/Models. Generic job retry remains available in Jobs.

Preview files are served only after proxy bundle validation through unguessable ticket URLs on a random loopback port. The server binds only `127.0.0.1`, implements byte ranges for seeking, rejects unknown tickets and exposes no directory browsing or user-supplied filesystem path. The desktop media CSP allows that loopback endpoint. URLs expire when the service stops. The browser's source time is `currentTime + sourceMinusProxySeconds` from the validated proxy mapping. Average frame rate is never used for seeking; VFR and rotated video use the existing proxy guarantees.

## Timestamp and storage contract

All review ranges use integer microseconds relative to the first source video presentation timestamp, with exclusive end boundaries. Whisper full-JSON offsets are milliseconds and are converted once. Analysis sample zero maps to `manifest.analysis.sourceStartSeconds - mapping.sourceStartSeconds`; that media-relative offset is added to every segment/word. Playback seeking uses the matching media-relative `sourceMinusProxySeconds - sourceStartSeconds` offset, and waveform source starts use the same origin. A source with a negative audio origin is rejected with a normalization explanation rather than receiving silently shifted timings. Unknown speakers remain null/unassigned. Word confidence is retained when provided; multiple subword-token confidence values are conservatively reduced to their minimum. Missing confidence stays null and is not presented as zero accuracy. Native zero-length word estimates receive an explicitly uncertain 10 ms review interval bounded by the parent segment; their original inferred offsets remain in the JSON artifact, text confidence remains available separately, and the UI warns about uncertain timing. Untimed punctuation or subword continuations attach to their parent word and mark it uncertain. These intervals are never presented as exact word boundaries. ASR word timing remains an estimate and is reviewable, not a claim of frame-perfect linguistic alignment.

```text
<project>/
  project.sqlite3                         # indexed schema v3 (existing filename/layout applies)
  models/<model>/<pinned-revision>/model.bin
  cache/transcripts/<job-id>.json          # immutable normalized inference
  cache/proxies/<proxy-job-id>/analysis.wav
  .localcut-staging/<job-id>/output.json   # discarded on failure/cancel/recovery
```

The actual database filename is `project.sqlite3` as defined by ProjectStore. Schema v3 adds `transcript_segments` (original_text, edited_text, source range, confidence, speaker), `transcript_range_markers` and transcript artifact/audio-origin references. Word records retain immutable inference text/timing and separate optional corrections. Existing v2 words are migrated into a review segment without changing their times. Segments and markers are indexed by transcript/source time. Review writes use SQLite transactions, WAL and full sync through the existing store; JSON artifacts are published using the job journal and atomic rename.

Transcription validates model integrity, source fingerprint, analysis media, timestamp bounds and finite confidence. It reserves 32 MiB plus an estimated 8 KiB per audio second for output. It checks the source again before publication. The job inserts the ready artifact and indexed transcript in one project transaction, then acknowledges completion to the queue. A registration error rolls back both indexed records and removes the uncommitted publication. Crash recovery reconciles the project's completed job and artifact before reporting success. Cache equivalence includes source, model/revision, backend build, pipeline and configuration; review corrections do not invalidate immutable inference. Status polling reuses a verified file/bundle only while its file-stat signature is unchanged; starting inference forces integrity verification again.

## Verification

Run from `video-editor/`:

```powershell
service/.venv/Scripts/python.exe -m pytest service/tests/test_transcription.py -q
npm test
npm run build
```

`test_transcription.py` creates a one-second PCM fixture and deterministic backend/full-JSON responses. It covers source audio offsets, missing confidence/speakers, atomic publication and rollback, deduplication, correction retention, markers, reopening, migration, cancellation, bad timestamps, insufficient RAM, checksum rejection, model download cleanup, structured unusual-path arguments and ticketed byte-range playback. React tests cover seek offsets, highlighting, corrected-text search, correction persistence, selected ranges and job submission.

For real inference, explicitly supply local fixture paths:

```powershell
$env:LOCAL_CUT_TEST_WHISPER = 'C:/tools/whisper/whisper-cli.exe'
$env:LOCAL_CUT_TEST_WHISPER_MODEL = 'C:/fixtures/ggml-tiny.bin'
$env:LOCAL_CUT_TEST_SPEECH = 'C:/fixtures/jfk.wav'
$env:LOCAL_CUT_TEST_FFMPEG = 'C:/tools/ffmpeg.exe'
$env:LOCAL_CUT_TEST_FFPROBE = 'C:/tools/ffprobe.exe'
service/.venv/Scripts/python.exe -m pytest service/tests/test_transcription_real.py -q -s
```

### Independent word-timing measurement

`service/tools/measure_word_timing.py` compares a service transcript JSON (`words` with `sourceInTicks`/`sourceOutTicks`) to a separately annotated JSON (`words` with `text`, `startSeconds`, and `endSeconds`). Supply the actual source media with `--source-media`; the tool hashes its bytes and requires both JSON files' lowercase SHA-256 `sourceFingerprint` values to match that digest. This prevents two annotations that merely repeat the same incorrect fingerprint from being compared. Both timelines must use the same source-relative origin: zero is the first source video presentation timestamp. For service transcript records, `originalText` takes precedence over corrected review `text`, preserving an evaluation of inference rather than human edits. The tool aligns normalized words with Levenshtein distance, reports WER, and measures boundary error only for exact text matches with `timingUncertain=false`. Per-word boundary error is the mean of absolute start and end errors; it reports the median and p90, start/end medians, uncertain matched count, and timing coverage (certain timing matches divided by reference word count). With no certain timing matches the target is unmet. The output includes the verified source path and digest. It exits with code 1 when median boundary error exceeds the default 250 ms target.

Example reference format:

```json
{
  "sampleId": "validation-001",
  "annotationMethod": "two-person-reviewed word boundaries",
  "sourceFingerprint": "<lowercase SHA-256 of the source media>",
  "words": [
    {"text": "A", "startSeconds": 0.42, "endSeconds": 0.58},
    {"text": "clear", "startSeconds": 0.61, "endSeconds": 0.93}
  ]
}
```

Run the tool with `service/.venv/Scripts/python.exe service/tools/measure_word_timing.py reference.json transcript.json --source-media C:/fixtures/lesson.mov`. Replace the fingerprint placeholder with the source media SHA-256 in both JSON files. The reference must be annotated independently of Whisper output; do not use the hypothesis timestamps to produce the reference. Unit tests validate normalization, standard WER accounting, immutable inference text, matching source identity and bytes, uncertain-timing coverage, service tick conversion, median boundary metrics, and rejection of empty/mismatched samples. No independent timing corpus is currently checked into the repository, so the ≤250 ms product target remains unverified until this tool is run on a reviewed representative corpus.

The real smoke test builds MP4 footage covering the full 11-second speech sample, generates actual preview/analysis assets, runs CPU inference, checks detected English, expected full sample text, model/backend provenance, bounded word/source timestamps, artifact registration, cache reuse, and unchanged source bytes. The source speech fixture is the upstream [JFK sample](https://github.com/ggml-org/whisper.cpp/blob/v1.9.2/samples/jfk.wav), SHA-256 `59dfb9a4acb36fe2a2affc14bacbee2920ff435cb13cc314a08c13f66ba7860e`. On the available Intel i9-12900K (16 cores/24 logical processors), whisper.cpp CPU release binary SHA-256 `95e3c0b0e778ad9499eb0125f97c1dcf437dd9eb4ea77050b043574f93c2631d` and tiny model revision `5359861c739e955e79d9a303bcbc70fb988958b1` completed the 11-second test transcription job in 1.5–1.8 seconds across two runs and returned the expected 22-word text. Test tooling and weights were acquired into the OS temporary folder, not bundled or installed into a user project. This smoke test validates the real inference path on a short fixture; it does not establish the 20-minute transcription target, timing accuracy against independently annotated word boundaries, or general transcription accuracy. Native Tauri/WebView playback and distributable sidecar packaging still require verification on a Rust-enabled desktop build.

Transcription progress reports preparation, waiting for the inference slot, inference, and validation stages; it does not invent a per-word completion percentage during the native CLI call. The general queue controls retries, cancellation, restart recovery and cleanup.

### 20-minute performance benchmark

The opt-in `test_real_20_minute_transcription_performance` generates 1,200 seconds of 1080p30 synthetic video with a locally looped copy of the short JFK speech sample, imports through the production FFprobe path, then runs proxy generation, tiny-model transcription, silence analysis, filler-token analysis, shot-boundary analysis, and first-cut proposal as persistent jobs. The tested host is an x64 Intel i9-12900K (16 cores/24 logical processors) with 31.8 GiB installed RAM, using CPU inference. The latest run began proxy generation 0.156 seconds after submission, transcribed 2,412 timestamped English words in 42.23 seconds, completed the three core analysis detectors in 7.32 seconds, and committed a pending first-cut plan with one reviewable operation in 0.58 seconds. Source generation took 37.72 seconds, and the imported source hash was unchanged. This verifies core analysis-to-proposal job flow and runtime on this host/fixture. Repetitive speech and a static test image do not establish accuracy, independent word-timing error, minimum-hardware performance, GPU behavior, or real-footage proxy throughput. Run the benchmark by setting `LOCAL_CUT_RUN_20MIN_ASR=1` alongside the five local fixture variables above.
