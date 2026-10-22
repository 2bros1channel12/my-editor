# Local Cut release readiness — 2026-10-21

Status: **development preview; MVP release gates remain open.** The persistent goal remains active. Windows 11 x64 is the first target, with a 4-core AVX2 CPU, 16 GB RAM and SSD minimum; 32 GB RAM is recommended. Functional tests on the development host do not validate the minimum configuration.

## Supported workflow

Create/open a native project; link or explicitly copy MP4/MOV sources; inspect hardware/media dependencies; generate proxies; run installed local Whisper transcription and configurable analysis; search exact transcript text (semantic search requires a configured local embedding provider); save a structured brief; generate/review a written plan; compile approved sections; make undoable manual or constrained conversational edits; generate editable captions; apply gain, normalization and optional noise reduction; reopen saved edits; render H.264/AAC MP4 and SRT; export editable OTIO. Sources remain references and final rendering reads the originals.

The authored local sample uses generated test-pattern video and tone audio, CC0-1.0 sample content, an explicitly authored demo transcript, a brief, and an editable timeline. Its text is not presented as actual speech inference. Create it from the first-run setup panel after FFmpeg/FFprobe are available.

## Privacy and storage controls

- Telemetry, crash upload and media upload are absent/disabled; no account or cloud inference is required. Network status reports Local Cut model-download jobs for the current project, not OS-wide traffic.
- Speech model downloads are explicit, cancellable persistent jobs using pinned revision, expected size and SHA-256. Models shows source URL, MIT license metadata, version, size, integrity and RAM guidance. Downloads do not install the separate Whisper executable. Local generation/embedding providers have their own model and network controls.
- Settings/Models shows project, transcript, cache and model storage. Proxy/detector cleanup retains the existing exact-path confirmation. Model removal, transcript purge and entire-project deletion require an inventory-bound review token and no active jobs. Transcript purge retains editable captions, briefs, plans and edit evidence; it is not a secure-erasure guarantee.
- Project deletion removes project-local managed copies, models, inference, cache, exports and recovery data while preserving external linked originals. Ambiguous legacy or linked sources physically inside a project must be moved/relinked first. Symlinks and junctions fail closed. An interrupted filesystem deletion identifies its exact quarantined folder for cleanup; backups made by users or the OS are outside the application's control.
- Managed copies now carry explicit app ownership metadata. Filename/path shape alone does not prove that media can be deleted. Local playback tickets are revoked after project deletion.

## Verification evidence

Final follow-up on 2026-10-22: settled-source Python regression **520 passed, 14 skipped**; UI **93 passed**; Rust boundary **8 passed**. Production build, NSIS packaging and enhanced bundled-service smoke passed. Eight real FFmpeg proxy variants and the explicit long synthetic ASR/render gates passed separately; optional skips in the general suite do not stand in for those runs.

See [release QA](RELEASE_QA.md) for reproducible commands, fixture identities, performance results and limits. The integrated 13-stage service test uses real FFmpeg, all ten detectors, exact search, explicit fixture ASR/planning adapters, approved-plan compilation, undo/redo, restart/reopen, captioned audio-finished MP4 and official-adapter-validated OTIO. Real local Whisper and process-level external-network-denied tests are separate checks. Failed render, disk-space, cancellation, retry, cache integrity, restart and adversarial-content tests are included.

The privacy reviewer found that project-relative source paths did not distinguish managed copies from original files. Durable managed-copy metadata and conservative legacy deletion checks resolve that ambiguity for new copies. The packaging reviewer found missing Python dependency notices; the packaging pipeline now gathers them from the actual sidecar environment. The QA reviewer separated fixture pipeline coverage from actual model quality and packaged-app privacy claims. These evidence limits remain explicit release gates.

## Package and onboarding

Follow-up 2026-10-22: prepared [isolated Windows validation tooling](SANDBOX_VALIDATION.md). The host has no `WindowsSandbox.exe` or available current-user signing certificate. Script parsing, generated read-only input/dedicated writable-output mappings, disabled-network configuration, exact installer hash, and the guest-only execution guard were verified locally. The installed-executable override of the smoke runner passed against the packaged sidecar. **No Sandbox installation or OS-level privacy pass is claimed.** The installer artifact/hash below remains unchanged.

Final installer: **14,827,971 bytes (14.14 MiB)**; SHA-256 `87cc0b960a2e5d2b0b0d70dff242d48fbfe1f8a492d8bba47c9e993234c89888`. The final build took 146.90 seconds. Enhanced bundled-service smoke completed in 4.66 seconds and verified project create/reopen, generated sample, persisted timeline/captions, OTIO export, a normalized 480p30 H.264 preview through the persistent queue, and clean shutdown. This is direct bundled-service execution, not a clean-machine installed-GUI test.

The build produces `src-tauri/target/release/bundle/nsis/Local Cut_0.1.0_x64-setup.exe`. See [packaging](PACKAGING.md) for final build/checksum details and sidecar smoke results. The package bundles the Python service; an installed user does not need Python. FFmpeg, FFprobe and model/runtime executables/weights remain separate prerequisites. First-run setup checks the local service, writable storage, hardware profile and media tools, then directs the user to guided speech-model selection. The current package is unsigned.

## Remaining release blockers

Audit against the specification's eight numbered success criteria:

| Criterion | Current evidence | Release status |
| --- | --- | --- |
| 1. 20-minute proxy/transcription latency and word timing | Looped-speech fixture starts proxy in 0.069 s and transcribes in 38.00 s on this host. Independent median word-boundary error is unmeasured. | Partial |
| 2. First-cut proposal within eight minutes | Synthetic deterministic proposal commits in 0.49 s after analysis; persistent job progress/cancellation tests pass. Representative planning quality is unmeasured. | Partial |
| 3. Review, reject, undo and source preservation | Schema/operation/undo tests and the 13-stage fixture verify source bounds, approved compilation, reversible commands and unchanged source SHA-256. | Verified for tested workflows |
| 4. Forced process death recovery | Persistence suite includes the specification's 20 termination trials; reopen/recovery is also exercised in the integrated workflow. Power loss is outside this evidence. | Verified for process-death fixtures |
| 5. Ten-minute playable captioned export | Real 1080p30 clean/burned exports pass decode, duration, source preservation and stream endpoint alignment. Independent content-level lip sync remains unmeasured. | Partial |
| 6. Installed offline workflow | Python service audit-hook denial and bundled-service smoke pass separately. OS-level denial of the complete installed GUI/native processes is untested. | Partial |
| 7. Neutral references only | Reference/recipe and adversarial-output tests cover neutral traits; the product exposes no cloning or copyrighted-music acquisition operation. | Verified for implemented reference paths |
| 8. Five-user usability and trim acceptance | No formative-beta participant or acceptance-rate evidence has been collected. | Unverified |

Passing a fixture does not remove the representative-footage and packaging gates below.

1. Clean Windows 11 installation/uninstallation, non-administrator behavior, WebView2 availability and native playback on a fresh machine; code signing and release distribution process.
2. Full packaged GUI workflow under OS-level outbound network denial and traffic observation, including native media/inference processes. Python audit-hook tests do not establish this.
3. Representative talking-head/tutorial corpus with independently annotated word timing, lip sync, selection and caption quality. Synthetic patterns and looped speech measure pipeline throughput only.
4. Minimum-hardware and representative long multi-clip performance; a five-user formative study and the specification's cut-acceptance/usability success criteria.
5. Third-party license obligations/distribution review, including FFmpeg build choices, external model/backend licenses and source availability obligations. Notice collection does not constitute this review.

Known functional limits: one sequential video composition, no overlapping video or cross-dissolves/speed effects, MP4/MOV music containers, single-pass loudness targeting, no diarization, and no portable guarantee for OTIO caption/gain/fade effects (stored as Local Cut metadata). Semantic/planning model quality depends on the explicitly configured local provider. GPU inventory is best-effort and does not prove runtime acceleration.
