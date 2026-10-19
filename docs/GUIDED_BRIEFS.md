# Guided briefs and editable project workflow

## Working slice

In the desktop app, Projects offers **New project**, **Open project**, and **Reopen last**. Open project selects an existing native project folder and loads its validated SQLite state, including recovery information. It becomes the recent project. Cancelling the picker or choosing an invalid project leaves the current project open. Moving to Editor retains the selected project.

Import linked MP4/MOV media, then use Editor to add source ranges to the video track. Trim, split, move earlier/later, delete, undo and redo operate on timeline references. Changes autosave through `project_save`; prior timeline snapshots are recovered through `timeline_history`. Source asset IDs and original in/out timestamps remain attached to every clip. Originals are never rewritten. A failed save keeps the local draft available with a retry action. Reopened projects recover saved timeline state and bounded undo snapshots; redo remains session-local.

FFprobe is needed to inspect imported media; FFmpeg is needed to generate preview assets. Manual range editing uses imported metadata and does not require a transcript or a speech model. Generated previews enable source playback. Full nonlinear editing, drag-based timeline manipulation, multicamera editing, overlays, speed changes, and cross-dissolves remain outside this slice.

## Guided constraints

Editor accepts an optional free-form brief or guided fields. A video purpose is sufficient to start a guided draft; free-form text is not required. Save creates a new immutable brief revision. Select an existing saved revision to load and edit it, then save another revision.

| Question | Saved constraint |
| --- | --- |
| Video purpose / what viewers should learn or do | `objective` |
| Intended audience | `audience` |
| Platform | `platform` |
| Target duration in minutes | `targetDurationMinutes` |
| Required topics or moments, one per line | `mustKeep` |
| Material to avoid, one per line | `mustRemove` |
| Desired opening and ending | `desiredOpening`, `desiredEnding` |
| Tone | `tone` |
| Optional explicit passage order | `preferredOrder` |
| Output framing | `aspectRatio` |
| Pacing | `styleTraits.pacing` |
| Average shot length / cuts per minute | `styleTraits.averageShotLengthSeconds`, `styleTraits.jumpCutFrequencyPerMinute` |
| Caption cues per minute | `styleTraits.captionDensityPerMinute` |
| Punch-ins / zooms per minute | `styleTraits.punchInFrequencyPerMinute` |
| Music energy | `styleTraits.musicEnergy` |
| Explanation/humor balance | `styleTraits.humorLevel` |
| Narrative structure | `styleTraits.narrativeStructure` |

Unknown style categories and null frequencies mean no preference. Zero means none. Platform never silently changes aspect ratio. Structured constraints use `schemaVersion: 2`; existing brief revisions remain readable with defaults for absent fields. No database migration is needed because brief constraints already live in versioned JSON inside `briefs.constraints_json`. All saved neutral traits are also indexed in `style_traits` with origin, generator ID, and generator version for the existing planner.

## Generic recipes and inspiration

Four local recipe starters are available: **Fast educational**, **Calm documentary**, **Conversational commentary**, and **Concise product review**. Applying a recipe replaces only the style draft, preserving purpose, audience, topics, duration, opening and ending. Every value can be edited. The saved constraints record recipe ID/version and the approved values, including changes made by the user.

An optional creator name can accompany the proposal request. A name alone cannot establish editing traits: the offline implementation proposes the selected generic recipe and states this limitation. It does not claim measured or learned facts about a named creator. No lookup or network request occurs. The separate inspiration field remains in memory and is excluded from the proposal response, saved constraints, traits, planner inputs and request logs. Ordinary brief notes may still mention a creator as a footage topic; those user-authored notes are retained as entered.

The draft displays neutral traits for review and editing. **I reviewed and edited these neutral editing traits** must be checked before saving. Changing traits, inspiration, recipe, parsed input or project resets review. The service independently requires literal `styleReviewed: true` whenever saving style values and validates all origins against the same neutral trait allowlist. The stored constraints record `styleReviewed: true` after validation. This records a user acknowledgment, not independent proof of review. Identity, voice, likeness, branding, signature graphics/phrases/assets, and music-download URL trait keys are not supported. Music energy is a preference; no music is acquired.

Optional local reference analysis feeds the same editable draft. Both save actions retain reference-derived provenance and require review. Raw reference media is not imported or retained by this workflow.

## Planner and protocol

The existing edit planner reads the saved brief revision. Required/avoided topics, target duration, preferred order, pacing and punch-in frequency currently influence its reviewable proposals. Platform, opening/ending, tone, caption density, cut frequency, music energy, humor and narrative structure are persisted and carried into the plan as guidance; automatic operations implementing all of those preferences are outside this slice. No new proposal is applied automatically.

New typed service calls are `project_open {path}`, `style_recipe_catalog`, and `style_recipe_propose {recipeId, inspiration?}`. `brief_save` accepts expanded constraints and the explicit `styleReviewed` acknowledgment. React types, protocol schema and the Tauri method/result allowlist are aligned.

## Verification

Focused suites cover recipe immutability and privacy, guided persistence, review enforcement, legacy briefs, planner lineage, selected-project reopening, timeline edits/history, save retries, and source preservation. Run from `video-editor/`:

```powershell
service/.venv/Scripts/python.exe -m pytest service/tests/test_guided_briefs.py service/tests/test_style_recipes.py service/tests/test_suggestions.py service/tests/test_service.py service/tests/test_reference_traits.py service/tests/test_persistence.py -q
npm test -- --run
npm run build
cargo test --manifest-path src-tauri/Cargo.toml --lib
```

The Rust command requires the Windows MSVC/SDK environment. Automated UI tests use a mocked desktop bridge; validating the packaged installer and representative real-media playback remains separate release work.
