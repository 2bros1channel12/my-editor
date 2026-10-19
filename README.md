# Local Cut desktop foundation

This is the Tauri/React/Python local-first video editor described in [`docs/VIDEO_EDITING_ASSISTANT_SPEC.md`](docs/VIDEO_EDITING_ASSISTANT_SPEC.md). It includes local project creation, nondestructive timeline primitives, transcript review, proxy assets, advisory footage analysis, reviewable transcript-led first-cut proposals with optional explicit passage ordering, neutral local reference-trait analysis, local H.264/AAC rendering with optional burned-in captions, and SRT sidecar export. The Windows service-sidecar build path is implemented; the full installer and release validation remain unverified. See [Windows sidecar packaging](docs/PACKAGING.md).

## Working from another PC

See [new PC setup](docs/NEW_PC_SETUP.md) for cloning, prerequisites, starting the desktop app, and moving editing projects safely.

## Prerequisites

- Windows 11 x64 for the first release target.
- Node.js 22 and npm 10.
- Python 3.11 or newer for development and packaging (installed desktop builds use the bundled service executable).
- Rust stable with the MSVC toolchain and Windows SDK for Tauri builds, plus Microsoft Edge WebView2 Runtime.
- FFmpeg is optional. The health screen reports its availability; no FFmpeg binary or codec is bundled by this foundation.

## Development commands

From `video-editor/` in PowerShell:

```powershell
npm install
python -m venv service/.venv
service/.venv/Scripts/python.exe -m pip install -e "./service[test]"
npm run dev
```

Run the desktop shell in another PowerShell window after installing Rust:

```powershell
$env:LOCAL_CUT_SERVICE_DIR = (Resolve-Path .\service).Path
$env:PYTHONPATH = (Resolve-Path .\service\src).Path
npm run tauri dev
```

For an installer, `npm run tauri build` first builds the frontend and then runs `scripts/build-sidecar.ps1`. The script uses an isolated build environment with pinned PyInstaller 6.22.3 and stages the `x86_64-pc-windows-msvc` service binary at the Tauri external-binary path. Tauri release builds start this bundled sidecar; development builds continue to start `python -m localcut_service`. Building the installer needs Python 3.11+, Rust/MSVC, and access to PyPI to acquire PyInstaller. The installed app does not need Python. FFmpeg and model weights remain separately acquired and are not bundled.

The Tauri supervisor starts `python -m localcut_service` as a child process in development and the packaged `localcut-service` sidecar in release builds. `LOCAL_CUT_PYTHON` can select a specific Python executable. `LOCAL_CUT_SERVICE_DIR` sets the development child working directory; `PYTHONPATH` points at the source package during development. The sidecar is compiled as a Windows x64 one-file executable and is launched with a process group so app shutdown also terminates service descendants.

Frontend checks:

```powershell
npm test
npm run build
```

Python service checks (from `video-editor/service/`):

```powershell
python -m pytest
```

The stdio protocol is newline-delimited JSON with version 1 request/response envelopes. `src/contracts.ts` maps every service method to typed request parameters and result data; compile-time bridge checks and `tests/contracts.test.ts` keep that map synchronized with both [`contracts/protocol.schema.json`](contracts/protocol.schema.json) and the Tauri method allowlist. The Rust boundary validates response version, request ID, envelope shape, and required top-level result fields before returning data to React. Methods include `ping`, `health`, `capabilities`, `project_create`, `project_open_last`, `project_save`, `media_import`, and `job_submit` / `job_list` / `job_cancel` / `job_retry`. Health reports the UI and shell separately from Python, FFmpeg, and writable local storage. When the service is unavailable, the UI remains open and reports a degraded state. See [`docs/PROJECT_STORAGE.md`](docs/PROJECT_STORAGE.md) for the schema, path, and recovery contract.

## Media import and device profile

The Projects screen can import individual `.mp4` and `.mov` files or scan folders recursively into the currently reopened project. Choose **Link MP4 / MOV** to reference files in place or **Copy into project** for a verified, atomic byte-for-byte copy under the project’s `media/` folder; neither mode modifies the selected original. Use **Relink source…** to find a moved file. Matching content relinks directly; replacing changed content requires confirmation and marks its derived artifacts stale. FFprobe must be installed or configured with `ffprobePath` / `LOCAL_CUT_FFPROBE`. Duplicate paths and matching content hashes are reported as duplicates. Unsupported, inaccessible, and corrupt files produce per-file errors without aborting the rest of a batch. See [project storage and source lifecycle](docs/PROJECT_STORAGE.md).

Settings shows the detected operating system, CPU flags/core count, RAM, available NVIDIA GPU/VRAM when `nvidia-smi` can report it, FFmpeg/FFprobe, advertised codec and hardware acceleration lists, and free space in the app data volume. Hardware inventory is best-effort; advertised encoders do not guarantee device/driver operation. Recommendations are conservative hints for future proxy and model selection, not active processing settings.

## Configuration and data

Configuration is loaded from `%LOCALAPPDATA%\\LocalCut\\config.json` on Windows, with environment overrides. Supported settings include `dataDir`, `logLevel`, `ffmpegPath`, `ffprobePath`, and `jobConcurrency`; concurrency is bounded to 1–16 and can be overridden with `LOCAL_CUT_JOB_CONCURRENCY`. `LOCAL_CUT_DATA_DIR`, `LOCAL_CUT_LOG_LEVEL`, `LOCAL_CUT_FFMPEG`, `LOCAL_CUT_FFPROBE`, and `LOCAL_CUT_CONFIG` override those settings. Structured, rotating JSON logs are written under `<dataDir>/logs`. Request bodies, media contents, transcript text, and creative briefs are not logged. The service logs a method name and request ID for diagnostics; job lifecycle and progress events are stored locally with each project.

Cloud inference is not part of the product. Manual and proposal-assisted editing and export are local. The UI uses system fonts. The Python/media workflow performs no upload; explicit model downloads and local preview playback are described below. The packaged sidecar build path is documented in [PACKAGING.md](docs/PACKAGING.md); the complete installer build still needs Rust/MSVC validation.

## Proxy generation

Imported assets now offer **Generate preview assets**. Configure `ffmpegPath` and `ffprobePath` (or `LOCAL_CUT_FFMPEG` / `LOCAL_CUT_FFPROBE`) for your local FFmpeg binaries. The service method is `proxy_generate` with `path` and `assetId`; monitor/cancel/retry through Jobs. See [proxy pipeline documentation](docs/PROXY_PIPELINE.md) for bundle layout, timestamp guarantees, resource limits and real-media test commands.

## Local speech transcription

The separate local generation/embedding runtime and Search page are documented in [local model runtime and footage index](docs/MODEL_RUNTIME_AND_INDEX.md). Exact transcript indexing works without a model. Semantic search requires an explicitly configured installed embedding model; no model is downloaded automatically.

See [transcription development and storage contract](docs/TRANSCRIPTION.md).
Install a compatible local [whisper.cpp CPU release](https://github.com/ggml-org/whisper.cpp/releases/tag/v1.9.2), keeping its DLLs beside `whisper-cli.exe`. Before starting the desktop app:

```powershell
$env:LOCAL_CUT_WHISPER_CLI = 'C:/tools/whisper/whisper-cli.exe'
$env:LOCAL_CUT_PYTHON = (Resolve-Path ./service/.venv/Scripts/python.exe).Path
npm run tauri dev
```

Generate preview assets in Projects, explicitly download a model in Models, then select the footage and **Transcribe locally** in Editor. Editor provides playback synchronization, source-time seeking, transcript search, saved text corrections, and required/excluded/uncertain range markers. The original inference remains available after corrections. Model downloads and transcription appear in the persistent job queue and support cancellation.

## Local footage analysis

Open **Analysis** to run independently configurable local detectors for silence and audio levels, transcript-timed speech regions and possible filler words, shot-boundary candidates, near-black and blur signals, representative proxy thumbnails, motion/activity, and possible repeated takes. Each detector is a cancellable persistent job with its own cache identity and source-time results. The interface presents results as review suggestions; it never deletes footage or changes the timeline automatically. See [analysis methods and caveats](docs/ANALYSIS.md).

## Project and manual editing

Use **Open project** to choose any existing native project folder, or **Reopen last** for the recent project. Editor now includes a guided brief and four editable generic style recipes; save requires an explicit review of neutral style traits. The optional creator-inspiration field stays ephemeral. See [guided briefs and editable project workflow](docs/GUIDED_BRIEFS.md) for the constraint schema, review rules, current planner behavior, and verification commands.

Use **New project** to choose a project name and local parent folder. In **Editor**, preview the selected source with its bounded local waveform summary, then add source ranges to the video track, split and trim clips, reorder or ripple-delete them, add/edit captions, set punch-in presets and linked-audio gain/fades, and undo/redo. Add a music/audio bed from an imported MP4 or MOV with an audio stream, choose source range and timeline placement, then set level and fades; the bed stays linked to its source. Each action saves the versioned project state. Clip source timestamps remain attached to original media; preview seeks account for the proxy offset. The Export panel renders a validated H.264/AAC MP4 from a captured timeline revision as a persistent background job, with an explicit option to burn accepted captions into the MP4 when the configured FFmpeg build supports libass. It also exports accepted captions as an SRT sidecar. Overlapping or speed-adjusted clips are not supported by the current render profile. See [project storage and recovery](docs/PROJECT_STORAGE.md) and [local export behavior](docs/EXPORT.md).

## Creative briefs and first-cut proposals

In **Editor**, enter a labeled or free-form brief, parse it locally into editable audience/objective/tone, target duration, aspect ratio, must-keep, must-remove, and optional preferred-passage-order constraints, then save a revision. Select a source and its reviewed transcript to queue a deterministic local proposal. The proposal separates transcript-timed passages into pending include operations, flags uncertain timing, honors required and excluded transcript markers and topic phrases, and warns when required topics are missing or the duration target is exceeded. A saved neutral pacing trait can adjust natural speech-gap grouping (fast 0.7 s, moderate 1.2 s, slow 1.8 s); it does not remove speech or authorize reordering, and its provenance is shown with the proposal. A saved punch-in frequency caps optional 110% suggestions to protected passages. Accept or reject each operation explicitly; acceptance applies the selected range to the editable timeline and is guarded against a stale project revision. When preferred-order phrases match kept passages uniquely, a separate reorder suggestion appears and can be accepted only after all those passages are accepted. Optionally create editable captions from reviewed transcript segments, and review separate subtle punch-in suggestions for brief-protected ranges. No LLM or network call is used; duration targets remain advisory.

The same Editor panel can optionally analyze a user-selected local reference clip into neutral pacing and cut-like change measurements. Remaining traits are left unknown for user editing. The reference file is not imported or retained, and the profile stays in memory until **Save profile to a new brief revision** is chosen. See [reference trait privacy and extraction limits](docs/CREATOR_REFERENCES.md).

See [brief and proposal behavior](docs/BRIEFS_AND_PROPOSALS.md) for parsing labels, cache provenance, review-state invalidation, priority rules, and undo limits.

Inference runs locally and makes no network request. Explicit model downloads contact Hugging Face and its download infrastructure for weights; media and transcript contents are never included in these requests. Preview playback uses a ticketed loopback server bound to `127.0.0.1` for the duration of the service process.

### Written edit plans

Use the **Plans** navigation page to generate and review source-bound rough-cut proposals. See [edit planning](docs/EDIT_PLANNING.md) for model setup, evidence validation, review guarantees, and current limits.

### Timeline engine

Editor uses the transactional native timeline engine with persistent undo/redo. See [timeline editing and storage](docs/TIMELINE_ENGINE.md) for commands, shortcuts, approval gates, recovery and preview limits.

### Reviewable revisions

Editor includes evidence-backed editing suggestions and a limited natural-language revision grammar. See [reviewable editing revisions](docs/EDITING_REVISIONS.md) for supported phrases, previews, protection rules, undo and verification.
