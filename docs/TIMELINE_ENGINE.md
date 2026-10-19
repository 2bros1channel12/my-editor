# Nondestructive timeline engine

## Editing workflow

Open Editor after importing footage and generating proxies. Select a source, video/audio track, and source in/out range; insert at the playhead. Select a clip to trim, split at the playhead, delete, move to the playhead or another compatible track, reorder, adjust gain, apply simple fades, or add captions. Ripple edits shift later clips on the selected track and their linked captions. Split a clip before inserting a gap or rippling through its interior. Insert a one-second gap or shift a track at the playhead. Gaps are empty intervals, never fabricated source assets.

Scrub using the timeline slider or numeric playhead, adjust the visible time window and viewport, and select markers/section labels or transcript passages to seek. Transcript navigation maps the currently playing timeline occurrence back to its exact source range, including trimmed and repeated clips. Space toggles playback, arrows seek 0.1 seconds, S splits, Delete removes, Ctrl/Cmd+Z undoes, and Ctrl/Cmd+Shift+Z redoes. Text fields retain normal shortcuts.

In Plans, approve sections individually, then choose **Append approved sections to timeline**. Pending and rejected sections cannot be compiled. This separate action revalidates source evidence, current plan review version and project revision, appends the approved selections in reviewed order, preserves model and source provenance, and creates section markers. Repeated compilation does not duplicate selections. Undo removes the compiled timeline change without altering written review decisions.

## Model and storage

Integer microsecond ticks, half-open intervals, and normal-speed clips are the canonical timing model. Every media/caption item has a stable ID, asset ID, source in/out, track, timeline start/duration and source fingerprint provenance. Video, audio, and caption tracks have stable IDs. Captions remain linked to their source clip through trim/split/move/ripple/delete. Manual markers are absolute timeline points; generated section markers follow the earliest surviving section selection. Same-track media overlaps, invalid bounds, unsupported speed changes, invalid gain/fades, duplicate IDs, unknown command fields and unknown schema versions are rejected.

Version 2 of `settings.timelineEngine` contains tracks, clips, captions, markers and reversible command history. Existing SQLite `timeline_items` and `captions` remain a relational projection for export and source relinking. Existing projects bootstrap from that projection without copying media or rewriting source files. A change made by a legacy suggestion invalidates incompatible undo history and imports its current projection. The native database/manifest format is unchanged; newer engine document versions are rejected until an explicit migration exists.

Each service command requires `expectedRevision`. A single SQLite transaction validates, changes the document, updates the relational projection, writes the project revision and a checksummed autosave recovery point, then commits. Existing external checkpoint recovery protects the document and history too. Undo and redo restore validated before/after states with the same IDs; divergent history is rejected, and a new command clears redo. Up to 50 commands are retained. Snapshot history uses O(50 * timeline size) space, so very large projects remain a future optimization target.

RPCs: `timeline_get`, `timeline_command`, `timeline_undo`, `timeline_redo`, `timeline_compile`. Mutations return the canonical `{timeline}` document. The UI serializes requests using returned revisions; playback, scrub and zoom never save project state. Failed operations leave the prior committed timeline intact. Reopen/reload after a conflicting revision rather than overwriting someone else's work.

## Playback and rendering limits

Timeline/source/proxy conversion uses the existing proxy source offset and preserves original source timestamps. Proxy response generation guards prevent an older asset request from replacing a newer selection. Playback uses decoded media time during footage and a clock during black/silent video gaps. Missing or buffering video pauses source advancement. Audio tracks are synchronized local media elements; simple audio fades/gain and primary-video fade-through-black are previewed. Browser media timing is approximate, not a frame/sample-accurate final renderer.

The preview uses the first video track, with linked source audio and independent audio tracks; it does not composite multiple video layers. Browser volume caps gain above 0 dB during preview. Export retains requested gain and uses the existing FFmpeg renderer; that renderer rejects overlapping video layers and unsupported profiles rather than silently flattening them. Overlapping cross-dissolves, speed ramps and layered compositing are outside this MVP engine. Simple transitions are cuts and fades through black.

## Verification commands

```powershell
service/.venv/Scripts/python.exe -m pytest service/tests/test_timeline.py service/tests/test_timeline_rpc.py service/tests/test_planning.py service/tests/test_plan_review.py -q
npm test -- --run
npm run build
cargo test --manifest-path src-tauri/Cargo.toml --lib
```

The fixture tests cover operations, protected source boundaries, approval gates, undo/redo, persistent serialization/reopen, malformed input, synchronization/offsets, stale proxy responses, and large-timeline lookup/windowing. They do not substitute for native playback QA with actual VFR footage, device buffering, and audio drivers.

## Revision policies and v1 migration

Version 2 adds protected source ranges and suppressed suggestion categories to reversible snapshots. Version 1 documents/history migrate with empty policies on load. See [editing revisions](EDITING_REVISIONS.md) for reviewed changes, atomic status updates and undo safeguards.
