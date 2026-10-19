# Reviewable suggestions and constrained revisions

## Workflow

Open **Editor**, assemble a timeline, and choose **Find editing suggestions**. Transcription and detector jobs must have completed for corresponding evidence to exist. Each card shows evidence/source timestamps and detector version, affected timeline interval, a proposed operation summary, estimated duration change, and before/after clip ranges. Operation details are expandable. Preview the before/after source sequence or an individual evidence passage, then explicitly accept or reject. Accepted changes have an Undo action; normal timeline undo/redo also updates the suggestion status atomically.

Nothing is applied when a request is typed or a suggestion is generated. After an accepted edit, regenerate proposals because their original timeline snapshot is stale. Undo requires later edits to be undone first. Missing or changed sources, modified briefs/transcripts/detectors, and changed protected ranges can require a new review rather than replaying stale changes.

## Limited grammar

Requests are single intents, case insensitive, with an optional final period and a maximum of 500 characters. No model is used to parse or execute requests.

| Request | Visible interpretation |
| --- | --- |
| Tighten pauses in the first minute. | Measured eligible silences within timeline 0-60s only. |
| Make the opening faster. | Silence tightening within the first compiled section, or first 30s when no section is available; exact scope is shown. |
| Keep every mention of pricing. | Protect exact original or reviewed transcript matches for the literal topic. Source protection follows all timeline occurrences and future uses. It does not restore already missing footage or infer synonyms. |
| Restore the longer explanation. | Select an accepted removal whose after-state still matches the current timeline; propose restoring its exact prior snapshot. No invented explanation or timestamp expansion. |
| Remove this suggestion everywhere. | Select a suggestion category; propose suppressing that category across this project. No footage deletion. Undo restores the prior policy. |
| Use fewer punch-ins. | Reset supported 110/125 percent crops to full source framing in the selected clip, or all existing punch-ins if no clip is selected. The exact list is shown. |

Other wording, negation, compound requests, shell/SQL/FFmpeg/JSON instructions, unsupported paths, and unresolved selections fail with actionable errors. Literal topic text is data. Transcripts, filenames, detector explanations and imported content never become instructions. The executor accepts only service-generated operations with closed schemas, never arbitrary model output or process arguments.

## Evidence and suggestions

- **Long silences:** current measured silence detector results, minimum 0.8s, retain 0.15s at both ends, and reject removal across a timed word. Pauses may carry meaning; review playback.
- **Possible false starts:** weak adjacent-segment repeated-prefix signals, plus explicitly labeled filler signals. Neither establishes that speech is dispensable.
- **Possible repeated takes:** transcript/timing detector evidence for both bounded source ranges. The proposed candidate is not declared worse; review both evidence previews before choosing.
- **Visual quality:** near-black/blur flags propose source removal while retaining later placements. This can remove speech; terminal removal can shorten the timeline. No automatic quality deletion occurs.
- **Pacing:** compare current duration with the saved target and uninterrupted clip lengths with the average-shot preference, preserving section labels. An average is guidance, not a maximum.
- **Unmet requirements:** exact transcript coverage checks against saved required topics, with saved brief evidence. Missing literal evidence does not prove semantic absence. These cards require manual review and have no automatic insertion/removal; acceptance is disabled.

Required transcript ranges, exact required-topic matches, explicit protected mentions, and locked approved-plan sections veto generated removals. Restoration also checks exclusions and retained required/protected coverage. Independent audio/video tracks block automatic ripple removals that could desynchronize them; use manual synchronized edits instead. Overlapping alternatives remain separate proposals and cannot be cumulatively accepted from a stale preview.

## Persistence, transactions, and undo

Native `edit_plans` records with `planType: timeline-revision` store immutable reviewed before/after snapshots, operations, evidence fingerprints, review status and duration estimate. These records are isolated from legacy executable proposal endpoints. Generation deduplicates identical pending proposals for the same revision and caps current pending proposals at 200.

The timeline engine document is now **version 2**, with `policies.protectedRanges` and `policies.suppressedCategories` included in every reversible snapshot. Version 1 documents and both history stacks receive empty policies in an explicit in-memory migration, persisted with the next edit. Original IDs, clips, captions and undo/redo remain intact. The SQLite/manifest format is unchanged.

Acceptance holds one SQLite write transaction, checks project revision plus source/analysis/transcript/brief fingerprints, validates the exact reviewed diff, applies one reversible operation batch, and commits timeline, indexed projection, status and recovery journal together. Partial errors roll back everything. Split/caption IDs generated during preview remain stable after acceptance and undo/redo; canonical comparison accounts for JSON key order without trusting proposed source bounds. Undo checks the recorded diff against its history entry. Generic timeline undo/redo and card undo synchronize status and policies in the same commit. Source media is never rewritten.

RPCs: `revision_list`, `revision_generate`, `revision_propose`, `revision_decide`. Direct timeline operations added are `remove_range`, `crop`, `protect_ranges`, and `suppress_category`; all remain strictly validated.

## Preview and limits

Previews play bounded source references sequentially using validated proxy offsets. Individual repeated-take evidence can be previewed even when absent from the timeline. Supported punch-in crops appear in before/after previews. The preview does not compose gaps, separate audio tracks, or layered video into a final mix; those placements are listed in the diff and remain reviewable in Editor/export. Native playback remains subject to browser timing and device QA.

This is a constrained revision vocabulary, not a general conversational editing agent. No arbitrary rewriting, invented speech, semantic promise of requirement fulfillment, free-form code execution, or automatic FFmpeg invocation is implemented. Full before/after snapshots trade storage for exact review/undo; very large suggestion sets need future incremental storage work.

## Verification

```powershell
service/.venv/Scripts/python.exe -m pytest service/tests/test_revisions.py service/tests/test_revision_rpc.py service/tests/test_revision_attacks.py service/tests/test_timeline.py service/tests/test_timeline_rpc.py -q
npm test -- --run
npm run build
cargo test --manifest-path src-tauri/Cargo.toml --lib
```

Tests cover closed grammar and malicious suffixes, timestamp/source boundaries, real artifact integrity, altered indexed evidence, stale/duplicate acceptance, protected mentions, independent tracks, caption splits, transaction rollback, policy undo, generic undo/redo statuses, exact restoration, migration, and inert UI rendering with bounded proxy previews.

## Reconciled subagent reviews

The grammar designer proposed deterministic single-intent requests and explicit context for restoration/suppression. The adversarial reviewer required closed operations, current evidence checks, atomic batches, precise handling of repeated occurrences and reversible policy changes. Final decisions use the fixed grammar, evidence-bound diffs, source protections and locks, versioned policies, conservative manual advisories, and a separate explicit acceptance step.
