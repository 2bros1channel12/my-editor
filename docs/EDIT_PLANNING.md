# Local written edit planning

Open **Plans**, select a saved structured brief and imported recording with a reviewed transcript, optionally enter an exact or semantic footage-index search, then generate a written plan. Configure an installed local generation model in Settings first. Semantic searches also require a local embedding model and current index.

## Evidence and validation

The persistent `planning` job snapshots the saved brief, transcript edits and original inference, required/excluded/uncertain markers, analysis, source identity, and optional index search. The model receives these as untrusted content with a closed JSON schema. Its response selects catalog candidate IDs, never executable operations, file paths, invented quotations, or timestamps. The service resolves IDs to immutable asset IDs and integer source microseconds, computes durations and requirement coverage, and records model/provider/version/settings/execution time.

Unknown candidates, extra output fields, nonfinite confidence, invalid or overlapping source ranges, excluded material, missing required ranges, invalid hooks, and missing low-confidence alternatives are rejected before commit. Protected ranges that cannot be represented by complete eligible transcript passages fail with an actionable error. Quality and silence flags remain advisory. Transcript text is evidence for review, not proof of exactly what was spoken; edited passages retain original inference for comparison.

## Review and persistence

Plans are versioned JSON in the existing SQLite `edit_plans` table, distinguished by `planType: written-rough-cut`. The original generated artifact remains in the job cache. Reviews transactionally update the written record with an optimistic `reviewVersion`. Users can approve, reject, replace from unused alternatives, reorder sections, and lock or unlock sections. Locks prevent rejection, replacement and moving the locked section. Changed source, brief, transcript, analysis or search evidence requires fresh generation. New generation creates a separate record and preserves existing reviews.

Approval alone never constructs a timeline. The separate append action compiles only approved sections through the validated timeline engine. Written plans are filtered out of legacy executable proposals and rejected by their acceptance endpoint. Source files and existing timelines remain unchanged. Cancellation, validation-before-commit, restart recovery and artifact cleanup use the persistent job system.

## Current scope and limits

One source recording per plan; explicit approved-section compilation is available. No unattended timeline construction, invented B-roll, or cross-asset plan assembly. Candidate catalogs are limited to 150 passages and 60,000 transcript characters; larger recordings require a narrower supported workflow in a later slice. Material-to-avoid phrases conservatively filter matching passages; semantic compliance, hook quality, humor, narrative claims and confidence require human review. Local model quality and hardware performance have not been benchmarked by fixture tests. The full brief guides the model; tracked requirement IDs cover required topics, purpose, desired opening/ending, and protected source ranges. Target duration is displayed as a comparison, not guaranteed or enforced by automatic trimming.

## Verification

`service/.venv/Scripts/python.exe -m pytest service/tests/test_planning.py service/tests/test_plan_review.py service/tests/test_planning_rpc.py -q`

`npm test -- --run` and `npm run build` validate the typed desktop interface and review behavior. Tests use deterministic local runtime fixtures and hostile transcript text, without model downloads or inference.

## Reconciled design reviews

The editor favored a coherent hook, section purposes, selection reasons, duration estimates and alternatives rather than a flat clip list. The structured-output review required an ID-only response and service-derived source times and totals. The skeptical review prioritized omissions, misleading transcript edits, semantic overclaiming and uncertain footage. Final decisions combine the outline with a closed candidate catalog, hard protected-range validation, original inference display, explicit unmet requirements, mandatory alternatives where available, and human approval. Stylistic confidence remains an editorial judgment. Initial plans use one recording and never execute media operations.
