# Output formats and local reframing

## Workflow

In Editor, choose an output recipe, review resolution, frame rate, target/maximum duration and four safe-area margins, then save. Horizontal, vertical, square, original-aspect and custom profiles are supported. Presets are editable starting points, not statements of current platform upload requirements. Custom aspect is specified by width and height.

Create named output versions to fork the current timeline. Switching versions saves the current timeline, captions, undo history and profile before restoring another. Versions share stable source asset IDs; original media is neither copied nor altered. Natural-language output requests propose profiles for explicit review. Multiple requested outputs create separate versions and can create separate written planning jobs; they do not imply distinct highlights have already been selected or approved.

Select a video clip, inspect its source and output crop, and run local subject analysis. Review the proposed camera path before applying it. Choose a temporary subject ID and rerun analysis to follow that subject. Keep-all mode fits the full source with padding when a group cannot fit the requested crop. Move/resize the crop at the playhead to save a keyframe. Lock, disable, fit, undo and redo are available. Split a clip to control a smaller timeline section. Stacked composition uses two manually reviewed regions of the same source, useful for a recording already containing gameplay and a camera inset.

## Storage and execution

The three reviews favored different priorities: tracking quality, responsive inspection, and avoiding silent loss of a second person or tutorial content. The final choice uses replaceable lightweight local detection with explicit proposal review, source-time keyframes rather than proxy-time camera paths, and padded keep-all fallback rather than forcing every scene to fill a vertical frame. The preview remains a muted inspection surface synchronized to the main timeline clock; it does not introduce a second playback clock.

- `settings.outputProfile`: closed, versioned export constraints. The timeline engine contains no platform-specific rules.
- `settings.outputVariants`: versioned independent timeline snapshots, associated plan IDs and profiles. Active edits are captured on switching/forking; the active timeline is autosaved normally.
- `clip.properties.reframe`: enabled/locked flags, crop/fit mode, optional subject ID, source-time normalized keyframes, optional stacked regions and detector provenance. Source timestamps survive trim/split operations.
- `cache/reframing/*.json`: independently validated persistent-job outputs, keyed by source hash, source range, profile, subject choice and detector version. Detection never edits the timeline itself.

Local OpenCV Haar faces, HOG people and conservative visual saliency provide spatial suggestions. IDs describe spatial continuity within an analysis, not identity. Sparse decoding is capped at 128 samples per clip; smoothing and a velocity limit reduce crop jitter. Low-confidence samples center crop. No remote inference or face recognition is used. FFmpeg receives validated application-generated numeric filters through argument arrays; large graphs use a local filter script. Crops preserve aspect; fit and stacked panels use padding.

Export uses the saved profile, enforces its maximum duration and validates the encoded result. Preview rendering caps the long edge at 1280. Caption generation shortens lines for the selected format; burned captions are reflowed to the active profile's asymmetric safe areas. Target duration is a planning target, not an instruction to silently trim an export. A profile-bound written plan must match the saved profile before compilation.

## Verification and limits

Focused tests cover horizontal/vertical conversion, selected/multiple subjects, movement smoothing, confidence fallback, stacked filters, safe margins, separate versions sharing originals, source bounds, cache provenance, cancellation, crop undo/split/reopen, and a real persistent-job portrait export. Synthetic one-second 320×180/180×320 filter checks took 0.077–0.111 seconds including probing on this machine. One blank 640×360 OpenCV detection took 0.0223 seconds. These are plumbing measurements, not face-tracking accuracy or long-video throughput benchmarks.

The tracker does not reliably identify an active speaker from speech. Faces in profile, occlusion, fast motion, crossings, screen content and small camera insets need manual review; spatial IDs can switch. Saliency is a visual signal, not semantic object understanding. Sparse sampling can miss brief movements. A keep-all fit keyframe conservatively fits the entire clip during rendering. Two independent source assets are not automatically synchronized/composited by the stacked-region tool. Automated highlights remain separate written plans requiring review. Safe-area guides cannot guarantee that every platform interface or every important visual is unobscured. Rebuild the desktop package to distribute these changes; earlier installers do not include them. The existing MVP release gates remain open.

## Changed files and verification commands

Service additions: `export_profiles.py`, `output_variants.py`, `output_service.py`, `reframing.py`, `reframe_engine.py`. Integrated with `briefs.py`, `planning.py`, `plan_review.py`, `timeline.py`, `render.py`, `finishing.py`, `finishing_captions.py`, `jobs.py`, `server.py` and `service/pyproject.toml`.

UI additions: `OutputProfilesPanel.tsx`, `ReframingPanel.tsx`, `ReframingPreview.tsx`, `reframingPreview.ts`. Integrated with `TimelineEngineEditor.tsx`, `ExportPanel.tsx`, `BriefProposalPanel.tsx`, `WrittenPlanReview.tsx`, `src/contracts.ts`, `contracts/protocol.schema.json`, `src-tauri/src/service.rs`, and README. Other pre-existing working tree changes were retained.

New focused tests: `service/tests/test_output_profiles.py`, `test_output_rpc.py`, `test_output_planning.py`, `test_reframing.py`, `test_reframe_render.py`; UI `tests/OutputProfilesPanel.test.tsx`, `ReframingPanel.test.tsx`, `ReframingPreview.test.tsx`, `reframingPreview.test.ts`.

Commands from the repository: `npm test`, `npm run build`, `cargo test --manifest-path src-tauri/Cargo.toml`. From `service/`: `.venv/Scripts/python.exe -m pytest -q` and the focused output/reframing/render/planning/timeline suites. Full service regression: 556 passed, 14 skipped in 96.78 seconds before final safe-area adjustments; focused regression after those adjustments: 100 passed. Final UI: 108 passed; Rust bridge: 9 passed; production build passed. Skipped service tests require optional external models or environments and are not evidence of real-world tracking accuracy. SRT is rewrapped for the profile but cannot encode safe-area positioning; use burned captions for controlled placement.
