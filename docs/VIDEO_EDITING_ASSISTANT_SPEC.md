# Local-first video editing assistant: MVP specification

**Status:** Product specification; desktop/service foundation, persistence, media import, proxy generation, local transcription, detector suggestions and review UI, project creation, transcript review, basic nondestructive timeline editing, deterministic brief parsing, transcript-led first-cut proposals with optional editable captions, separate punch-in suggestions, explicit preferred-passage reordering, neutral local reference-trait analysis with explicit-save persistence, a user-supplied audio-bed track with local mix, per-clip fade-through-black transitions, a Windows service-sidecar build path, and a constrained local H.264/AAC render with optional burned-in captions and SRT export are implemented. The standalone service sidecar has a protocol smoke check; the complete Tauri installer and release validation remain unverified. The current renderer rejects overlapping or speed-adjusted clips and does not render overlays or cross-dissolves.  
**Repository context:** The current root project is TalkATC, an offline Windows desktop companion targeting .NET 8. It has no video editing pipeline, timeline model, media dependencies, or video-specific acceptance suite. This document defines a separate product direction; the existing Windows desktop app is evidence of repository familiarity, not a reusable editor architecture.

## Product summary

Build a Windows desktop assistant that turns user-owned talking-head and tutorial recordings into a private, reviewable, editable first cut from a natural-language creative brief. It automates transcription and proposes trims, emphasis, and captions. The creator remains in control of every edit; source files are never rewritten.

### Primary user and problem

The primary user is a solo educator, software instructor, or subject-matter expert recording 10–60 minute, single-camera lessons. They understand the material but lose time scrubbing for pauses, correcting transcripts, making basic cuts, and captioning. Unreleased or private recordings should not need to be uploaded to a service to get a useful first cut.

The MVP is an editing assistant, not a professional nonlinear editor replacement. It targets clear speech, one speaker, one main camera angle, and mostly continuous instruction. English is the initial supported transcription language.

## Supported platform and hardware

- **First release:** Windows 11 x64. Windows 10 may be evaluated during validation, but is not a release support commitment. This narrows packaging and media-device validation for the first release; it follows the repository's Windows desktop precedent without assuming its code is reusable.
- **Minimum supported:** 4-core x64 CPU with AVX2, 16 GB RAM, SSD, integrated graphics, and free space for the source, proxy/cache, and output (plan for at least 2–3× source size). CPU-only analysis is supported with reduced speed.
- **Recommended:** 8-core CPU, 32 GB RAM, NVMe storage, a GPU with 8 GB VRAM where supported, and at least 100 GB free working space. GPU acceleration is optional, not a requirement.
- **Initial media validation target:** 1080p, 30 fps talking-head/tutorial footage. Other resolutions/frame rates may import, but are outside the first performance guarantee until measured. Handle variable-frame-rate input through normalized proxy timing and verify source-time mapping before export.

The risk review proposed an 8 GB RAM floor; the product and architecture reviews favored 16 GB. The release floor is set to 16 GB because transcription, proxying, editing, and a local language model compete for memory. A later benchmark may justify a lower tested configuration.

## Complete MVP workflow

1. **Create a project and import footage.** Create a named project, select one or more local recordings, and choose copy-into-project or link-in-place. Probe the media, show duration/format/storage requirements, and preserve originals unchanged. Generate a low-resolution proxy, waveform, and thumbnail strip in the project cache.
2. **Set the brief.** The user enters audience, lesson objective, desired duration, tone, must-keep topics, must-remove topics, and optional aspect ratio. The interface turns the brief into an editable, visible set of constraints before generation.
3. **Optionally describe a reference.** The user may provide a lawful reference clip/file for on-device analysis. The app extracts only a neutral, editable trait profile: pacing, average shot length, jump-cut frequency, caption density, punch-in frequency, music energy, humor level, and narrative structure. It displays and allows editing of that profile. Reference media is not retained by default; profile persistence requires an explicit save action. No named-creator “make it like” mode is offered.
4. **Analyze locally.** Create or reuse a proxy, transcribe speech with word timestamps and confidence, detect silence/pause and possible filler spans, and identify coarse scene boundaries. Show progress, estimates, and cancellation. The user corrects uncertain words and can protect transcript spans that must remain.
5. **Generate a first-cut proposal.** Use the brief, transcript, protected spans, and approved trait profile to suggest trims and reorder only where the brief calls for it. Add optional punch-in emphasis and editable captions. Every operation is labeled as a proposal with a brief rationale; uncertain transcript spans and low-confidence edits are flagged. Nothing is applied without review.
6. **Review and edit.** Preview the proposed cut against the transcript and a native multitrack timeline. Accept, reject, or adjust individual edits; restore any source span; perform basic trim, split, ripple delete, reorder, caption correction, punch-in crop preset, and audio level/fade edits. Provide undo/redo and autosave.
7. **Export and reopen.** Render locally to H.264 MP4 with accepted captions burned in or exported as an SRT sidecar. Save the editable project and timeline as the canonical edit. Report export progress and verify the completed file can be reopened/played. Preserve the prior successful export if a new render fails.

## Included features

- Project creation, local media link/copy, media probing, proxy generation, thumbnail/waveform cache, and missing/changed-media relink.
- Single-camera, single-speaker talking-head/tutorial first-cut assistance.
- Local speech-to-text with word-level timing, confidence indicators, transcript correction, and transcript-to-timeline navigation.
- Reviewable pause/silence/filler candidates and optional coarse scene boundaries; no automatic factual rewrite.
- Natural-language brief parsed into user-visible, editable constraints.
- Optional user-provided reference analysis producing only the neutral trait profile listed above.
- Editable single video track with linked source audio, caption track, and simple music/audio bed track for user-supplied or properly licensed tracks; no included music library.
- Basic cuts, trims, ripple deletes, reorder, restore-source-span, caption editing, simple punch-in crop preset, audio gain and fades.
- Per-edit rationale/confidence, accept/reject, undo/redo, autosave and crash recovery.
- Local H.264 MP4 export and SRT sidecar, with burn-in as a user choice.
- CPU-only operation, background jobs, pause/cancel where safe, progress, storage warnings, and actionable errors.

## Explicit non-goals

- Reproducing or identifying a creator's identity, voice, likeness, branding, logos, signature intro/outro, signature assets, or other distinctive identity markers.
- Voice cloning, face/identity matching, branded-template extraction, copyrighted music cloning, scraping references, or copying music from a reference.
- Generative video, voiceover, or music; automatic factual rewriting; autonomous publication or social account integration.
- Cloud inference, required accounts, collaboration, mobile editing, or multi-user review.
- Multi-camera synchronization, multicamera switching, complex compositing, keyframe-heavy motion graphics, advanced color grading, advanced audio mixing, broad cinematic/fiction workflows, or replacing a full professional NLE.
- Guaranteed round-trip interchange with Premiere, Resolve, or other NLEs. Interchange may be added only after validating that the supported edit features survive round-trip.
- Bundling model weights, music, codecs, or FFmpeg builds until each component's distribution license and notices are reviewed.

## Creator-reference boundary

References may inform only visible, user-adjustable editing traits: pacing, average shot length, jump-cut frequency, caption density, punch-ins, music energy, humor level, and narrative structure. The app must not extract or generate identity, voice, likeness, branding, signature assets, or copyrighted music. It should not scrape or fetch reference material; the user supplies a local file. Keep reference-derived data ephemeral unless the user saves the neutral trait profile. Output assets must come from the user or an appropriately licensed source.

## Privacy guarantees

- After the installer and optional model files have been acquired, import, analysis, editing, project save, and export work without a network connection.
- Source footage, audio, transcripts, briefs, reference-derived traits, project files, proxies, caches, and logs remain on local storage. No media or prompt is sent for remote inference. No telemetry or analytics is enabled by default.
- The app must not perform runtime network calls for editing/export. Update checks, if introduced, must be user-controlled, separately disclosed, and must not include project content.
- Clearly show project/model/cache paths and provide project/cache deletion controls. Do not claim encryption at rest; files are protected by the user's operating-system account and device configuration.
- Privacy acceptance is a network-deny test after installation/model acquisition, plus inspection that no project content is transmitted. Installer/model acquisition itself contacts disclosed vendor endpoints and is not described as air-gapped.

## Project and timeline data models

The native project is the canonical editable source. Use a versioned, human-auditable manifest (for example JSON) and content-addressed or fingerprinted media references. A project contains:

- `project`: stable ID, schema version, app version, creation/update timestamps, timebase, project settings, output profile, and autosave/recovery metadata.
- `assets[]`: stable ID, original path or managed project path, size, duration, video/audio streams, codec, frame rate/timebase, file fingerprint, relink status, proxy/cache paths, and user ownership/source notes.
- `brief`: original user text, structured constraints, target duration/aspect ratio, must-keep and must-remove items, and user revisions.
- `referenceProfile`: optional neutral trait vector with extraction method/version, user edits, and explicit saved/ephemeral status; never an identity or creator embedding.
- `transcript`: versioned segments/words with source asset, source-time in/out, text, confidence, speaker ordinal only if needed, and user corrections/protected-span flags.
- `analysisArtifacts[]`: typed/versioned silence, filler, scene, and framing candidates with source times, confidence, model/tool version, and regeneration status.
- `timeline`: ordered tracks and clips. Each clip references an asset ID, source in/out, timeline start, track, timebase, playback rate, transform/punch-in preset, audio gain/fades, and linked caption references. Transitions are limited to cut and simple fade.
- `suggestions[]`: proposed operations separate from accepted timeline state, including rationale, confidence, source ranges, status (pending/accepted/rejected/adjusted), and origin analysis version.
- `history`: undoable edit operations and autosave checkpoints.
- `export`: codec/container, resolution, frame rate, audio settings, caption mode/sidecar path, destination, and last-successful-render metadata.

Use integer frame/timebase ticks for timeline positions and high-resolution source timestamps for transcript alignment. The editor must define deterministic rounding between source timestamps and output frames. On VFR input, proxy/source mappings and audio sync must be explicitly validated.

## Local media and model strategy

- Keep media probing, proxy generation, decoding, and export behind replaceable local adapters. Evaluate FFmpeg and platform codecs only after licensing and redistribution review; pin versions and publish notices. Keep source resolution separate from preview resolution.
- Run transcription in an isolated local process using an optional, separately acquired speech model. Record model identity/version/license and support a no-model manual-edit path.
- Use a swappable local language-model adapter only to turn a brief into structured preferences and organize candidate transcript material. Keep deterministic, inspectable editing rules responsible for timeline operations. A small local LLM is optional; no LLM must be required to import, edit manually, or export.
- Keep reference-trait extraction local and constrained to the stated trait schema. Do not generate embeddings intended to identify people or match a creator.
- Model files are separately obtained and license-disclosed. Do not silently download models or update them at runtime. No remote AI backend is included in the MVP.
- Use bounded background concurrency, proxy-first preview, configurable model tiers, and CPU fallback. Show estimates and allow cancellation to prevent long jobs from appearing hung.

## Failure and recovery behavior

- **Missing or changed media:** Keep the project open in a degraded state, identify affected clips, and offer relink by path/fingerprint. Do not substitute a different file silently.
- **Interrupted proxy/analysis:** Mark the artifact incomplete, retain valid completed artifacts, and allow safe regeneration without changing accepted timeline edits.
- **Low disk space:** Estimate source/proxy/export space before work; pause or fail cleanly before overwriting user data. Offer cache cleanup only after showing which project/cache paths will be removed.
- **App crash or power loss:** Write versioned atomic project saves and autosave checkpoints after meaningful edits. On restart, recover the last valid checkpoint and report any lost interval; never replace the last valid project with a partial file.
- **Missing model or analysis error:** Manual editing and export remain available. Explain how to select/reacquire the model; preserve the transcript/timeline and allow retry.
- **Export failure/cancel:** Render to a temporary path, verify completion, then atomically move to the destination. Preserve an existing successful export and leave the accepted timeline unchanged.
- **Uncertain transcription/edit:** Mark confidence and preserve the original span by default. Never silently discard protected spans or rewrite technical claims.

## Measurable MVP success criteria

All criteria below are release gates to measure, not existing claims. Benchmark and record hardware, media, model version, and settings.

1. On the recommended configuration, a 20-minute 1080p30 single-speaker clip begins proxy generation within 10 seconds and completes local transcription within 5 minutes for supported English; report actual word-timing median error, target ≤250 ms on the validation corpus.
2. First-cut proposal is available within 8 minutes after analysis completes for that clip. Cancellation and progress remain responsive throughout.
3. Every suggested change can be inspected, rejected, undone, and restored from the original source range; no test workflow overwrites source media.
4. **Verified in persistence tests:** 20 forced-process-termination trials reopened with the latest accepted edit and transcript intact, with zero seconds of accepted work lost. This covers application process death; power loss and storage-device failure are not simulated.
5. A 10-minute 1080p30 project exports to H.264 MP4 with audio sync error ≤1 frame, captions matching the accepted transcript, and a playable final file; a failed export preserves the previous successful output.
6. After installer/model acquisition, a network-deny acceptance run completes import, transcription, proposal, edit, save, and export without network access or outbound project data.
7. Reference input yields only the stated neutral trait profile; test cases produce no identity, voice, likeness, branding, signature-asset, or copyrighted music output.
8. In a formative beta with at least five target users, at least four can complete review and export in under 20 minutes of hands-on work after analysis, without facilitator help. At least 70% of suggested pause/filler trims are accepted or need only minor adjustment; also report over-cut incidents to avoid incentivizing aggressive edits.

## Dependency-ordered implementation plan

1. **Media feasibility and licensing:** Decide Windows UI/runtime, media backend, codecs, storage layout, VFR policy, and model/FFmpeg acquisition and notices. Prototype media probing, preview decode, and export feasibility against representative files before committing to dependencies.
2. **Project foundation:** Implement versioned manifest, asset fingerprints, path/link/copy behavior, atomic save, recovery checkpoints, relinking, and cache lifecycle. Validate open/save/recovery independently of AI.
3. **Media review pipeline:** Add proxy/waveform/thumbnails, source-time mapping, preview playback, storage estimates, job progress/cancel, and missing media behavior.
4. **Manual timeline editor:** Add timeline tracks/clips, deterministic timebase, cut/trim/ripple/reorder/restore, captions, basic crop/audio controls, undo/redo, and linked transcript navigation. This is the usable foundation before generated edits.
5. **Local transcription:** Add optional process-isolated ASR, word timestamps/confidence, transcript correction/protected spans, retries, model selection, and measurable timing/accuracy validation.
6. **Brief and reference traits:** Add brief-to-structured-constraints parsing with user confirmation, then local reference trait extraction limited to the explicit schema. Keep no-model/manual workflow usable.
7. **Suggestion engine:** Generate explainable pause/filler trims and constrained first-cut proposals from transcript, brief, protected spans, and approved traits. Keep proposals separate from accepted edits and add review, accept/reject, undo, and restore behavior.
8. **Export and recovery hardening:** Add local H.264 export, SRT/burn-in captions, temporary output verification/atomic finalize, audio-sync checks, cancellation, and resume/error UX.
9. **Release validation:** Complete privacy/network-deny, licensing/notices, recovery, performance, accessibility/usability, representative footage, installation, and beta success gates. Do not claim measured latency or reliability before collecting evidence.

## Independent reviews and reconciled decisions

Three reviewers worked independently after repository inspection: a product designer, a technical architect, and a risk reviewer. Recommendations were reconciled as follows:

- **Windows support breadth:** Product and architecture preferred Windows 11 or Windows 10/11 x64; the risk reviewer cautioned that Windows was only a repository precedent. Decision: Windows 11 x64 is the first-release commitment, with Windows 10 considered during validation only. The media/model boundaries should remain portable.
- **Hardware floor:** Product/architecture proposed 16 GB RAM; risk review said 8 GB could be possible with small models and proxies, while favoring tested claims over guesses. Decision: specify 16 GB minimum, CPU-only support, GPU optional; benchmark lower configurations before expanding support.
- **Source count and cut sophistication:** Product proposed one or more clips and some hook/order restructuring; risk review recommended a single-camera, tightly bounded workflow. Decision: allow multiple user files in a project, but MVP first-cut assembly stays within one main source recording and conservative transcript-led cuts. No multi-camera, take-selection engine, or automatic narrative rewrite.
- **Music:** Product included a user-supplied/licensed music option; risk review highlighted distribution and copyright risk. Decision: a simple audio bed is allowed only for user-supplied or properly licensed tracks. No bundled library, reference-track copying, or music generation/cloning.
- **Creator references:** All reviewers agreed references must resolve to traits, not imitation. Decision: user-provided local reference only, visible and editable trait profile, ephemeral by default, with the prohibited identity/voice/likeness/branding/signature asset/copyrighted music behaviors stated as hard boundaries.
- **Local AI authority:** Architecture proposed optional local ASR and a small LLM; risk review emphasized privacy and edit authority. Decision: ASR is optional and process-isolated; an LLM is optional and may structure briefs, but deterministic rules propose timeline operations and every operation remains reviewable. Editing/export must work without a model.
- **Privacy language:** Risk review rejected an unqualified “nothing ever leaves the device” claim. Decision: promise offline processing/export after installation and model acquisition, no default telemetry or remote inference, disclose acquisition endpoints if introduced, and make no encryption-at-rest claim.
- **Interchange and licensing:** Architecture noted possible interchange; risk review cautioned against unverified round-trip and bundled codec/model obligations. Decision: native project is canonical; no interchange guarantee and no model/codec/music bundling until license review.

## Repository inspection note

Inspected the root `README.md`, `Directory.Build.props`, existing `docs/DESIGN.md`, `docs/VALIDATION.md`, `docs/ACCEPTANCE.md`, and desktop/core project files. They establish a .NET 8 Windows desktop baseline and document TalkATC's optional external whisper.cpp process and no-runtime-cloud approach. They do not establish video-specific architecture, library choices, or performance data. Existing unrelated untracked files were present before this specification was added and were left untouched.

## Persistence foundation progress

The separate `video-editor/` application foundation includes a Python SQLite project store with schema migrations, metadata-only `project.json`, stable UUID asset IDs, source fingerprints and portable paths, versioned timeline items with required source asset/source in/source out ranges, transcript word timing, analysis and artifact provenance, briefs/style traits, edit plans, captions, jobs, model records, exports, autosave history, recovery checkpoints, and last-project reopening. See [`PROJECT_STORAGE.md`](PROJECT_STORAGE.md). Media import supports explicit linked references or verified project-managed copies. Originals are never modified; changed-media replacement requires confirmation and invalidates derived artifacts while preserving the stable asset ID and timeline. Last-project reopening shows a best-effort missing/changed source status, and affected timeline clips remain visible with a relink warning. The editor preview shows a source-time-aware bounded summary of the generated waveform without transferring the full PCM asset to the UI.

## Brief and proposal implementation progress

The Editor parses labeled creative briefs deterministically on-device, lets the user review and edit visible constraints, saves versioned brief revisions, and queues a transcript-led proposal job. Proposals use source-time transcript groups, required/excluded/uncertain markers, and must-keep/must-remove phrases; uncertain passages are flagged, missing must-keep topics and unmet duration targets are explained, and each passage remains a separate pending operation. Accepting an operation is revision-guarded and changes only the selected timeline range; rejecting it does not mutate the timeline. Proposal artifacts record source, transcript artifact and review-state fingerprints, brief revision, and generator provenance. An optional preferred passage order creates a separate pending reorder operation only when each phrase maps uniquely to a kept passage; it remains disabled until all kept passages are accepted and applies only to their contiguous block at the end of the video track. Reordering preserves source ranges and linked caption timing and can be undone independently. Accepting a kept passage can create editable transcript captions, and a separate pending 110% punch-in suggestion may be accepted for protected passages. This first pass does not use a language model and only warns when the duration target is exceeded. A constrained local H.264/AAC renderer supports separate SRT export and optional burned-in captions when the installed FFmpeg build exposes the ASS/subtitles filter; both modes preserve the editable timeline. Each render checks FFprobe audio/video stream start-time alignment within one output frame, persists the measured offset in export history, and shows it in the Export panel. Full real-media audio-sync and reopen validation remain outstanding.

## Neutral reference-trait implementation progress

The brief panel can analyze a user-selected local MP4/MOV and returns an editable neutral profile. FFprobe/FFmpeg sample up to five minutes at one grayscale frame per second to estimate average shot length and cut-like visual change rate. Caption density, punch-in frequency, music energy, humor, and narrative structure remain unknown for explicit user input. Reference samples are temporary and deleted after analysis; the file is not imported, retained, or logged. Saving the traits is an explicit action that creates a brief revision with analyzer provenance. See [`video-editor/docs/CREATOR_REFERENCES.md`](CREATOR_REFERENCES.md).

## Local analysis implementation progress

Independent local analysis jobs produce source-timed suggestions for audio silence and levels, transcript speech regions and exact common filler tokens (`um`, `uh`, `erm`, `er`), shot changes, near-black and blur signals, representative thumbnails, motion, and possible repeated takes. Detector settings, source/transcript/proxy provenance, cancellation, per-detector cache identity, and review playback are integrated with the persistent job system. Filler grouping is limited to adjacent transcript tokens; suggestions preserve backend confidence when available and never change the timeline or remove footage. Timestamp, settings-cache invalidation, cancellation, false-positive guidance, and review UI coverage are documented in [`video-editor/docs/ANALYSIS.md`](ANALYSIS.md). Real-media/release performance and privacy gates remain unverified.

## Privacy test progress

Two Python service subprocess tests reject DNS and non-loopback connections. One covers core project operations; the other uses local FFmpeg/FFprobe and generated synthetic media to cover linked import, proxy jobs, the transcription job/artifact path, brief/proposal generation, project/timeline save, and H.264/AAC export while checking source bytes remain unchanged. The latter injects a test-only deterministic speech backend and readiness override, so it does not verify real Whisper inference, acquired-model integrity, or the packaged Tauri application's outbound traffic. Full release network-deny acceptance remains open. See [`video-editor/docs/PRIVACY_VALIDATION.md`](PRIVACY_VALIDATION.md).

## Real media integration progress

With explicitly configured local FFmpeg/FFprobe binaries, all eight optional proxy integration variants pass: ordinary audio, variable frame rate, rotated video, delayed audio, anamorphic pixels, silent video, nonzero timestamps, unusual file paths, and corrupt input. The opt-in 10-minute 1080p30 synthetic render acceptance also passes through clean and burned-caption jobs; it checks H.264/AAC, full decode, duration, stream-start sync within one frame, burned caption pixels versus the clean render, SRT text matching a fixture transcript, and unchanged source bytes. On the available Intel i9-12900K (16 cores/24 logical processors), source generation and both renders completed in 126.23 seconds with FFmpeg 8.1.3. A real CPU Whisper smoke test now also passes end to end using the full 11-second JFK fixture: the pinned tiny model returns the expected 22-word text, English detection, bounded source timestamps, artifact/cache reuse, and unchanged source bytes; the transcription job took 1.5–1.8 seconds across two runs on this host. These synthetic and short-fixture results do not establish performance on the minimum configuration, the 20-minute transcription target, independent word-timing accuracy, long-duration lip-sync drift, or caption accuracy on recognized speech. These checks are in [`test_proxy_ffmpeg.py`](../video-editor/service/tests/test_proxy_ffmpeg.py), [`test_render_ffmpeg.py`](../video-editor/service/tests/test_render_ffmpeg.py), and [`test_transcription_real.py`](../video-editor/service/tests/test_transcription_real.py).

An additional opt-in 20-minute 1080p30 CPU benchmark uses the local 11-second speech sample looped over a synthetic static image and imports through FFprobe. The host is x64 with an Intel i9-12900K (16 cores/24 logical processors) and 31.8 GiB RAM. With whisper.cpp binary SHA-256 `95e3c0b0e778ad9499eb0125f97c1dc437dd9eb4ea77050b043574f93c2631d` and pinned tiny model revision `5359861c739e955e79d9a303bcbc70fb988958b1`, the latest run began proxy generation 0.156 seconds after submission, transcribed 2,412 English words in 42.23 seconds, completed silence, filler-token, and shot-boundary jobs in 7.32 seconds, and committed a pending first-cut plan with one reviewable operation in 0.58 seconds; source generation took 37.72 seconds and source bytes remained unchanged. This verifies the core analysis-to-proposal job flow and timing on this fixture. Independent word-timing accuracy, representative-footage quality, and minimum-hardware performance remain unmeasured. The benchmark is in `test_transcription_real.py` and requires explicit local paths plus `LOCAL_CUT_RUN_20MIN_ASR=1`.

## Packaging notice progress

The locked Windows Cargo dependency notice generation now includes checked-in, SHA-256-verified license texts for the six crate source archives that lacked them, pinned to each crate's `.cargo_vcs_info.json` revision (MPL text comes from Mozilla's license steward). `npm run licenses:prepare` succeeds for 239 Windows Rust packages with zero missing notice texts and reports all six supplemental sources. This resolves notice-text availability, not legal approval: review attribution and distribution obligations, especially MPL source availability, and separately acquired FFmpeg/model licensing. The Tauri build still cannot complete on this host because the Windows SDK linker libraries are absent. Details are in [`video-editor/docs/PACKAGING.md`](PACKAGING.md).
