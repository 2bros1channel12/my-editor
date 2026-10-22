# Local Cut project schema and storage

Reviewed model/transcript/project deletion and durable managed-copy ownership are documented in [privacy and storage controls](RELEASE_STORAGE.md). The optional `localcutManagedCopy`, `localcutManagedOriginalUri`, and `localcutManagedOriginalSha256` asset metadata fields identify verified app-created copies; absent ownership metadata is treated conservatively during deletion. These additive metadata fields require no SQLite schema migration.

## Format and authority

A native project is a directory with a `project.json` metadata sidecar and `project.sqlite3` database. The sidecar identifies the format, stable project ID, display name, version, timebase, and database filename. SQLite is the sole canonical store for editable/project state; JSON is not a second copy of the timeline. Metadata writes and the application-level recent-project pointer use same-directory temporary files, flush, and atomic replacement.

The project JSON format and database schema have independent versions. `project.json.formatVersion` describes the directory/sidecar format. SQLite `PRAGMA user_version` identifies the database schema, and `migrations_applied` records migration timestamps. Migrations run in order inside a transaction and roll back on failure. Newer unsupported versions are rejected with an actionable error rather than downgraded.

## Storage layout

```text
<name>.localcut/
  project.json                 # readable project identity and format metadata
  project.sqlite3               # canonical indexed project state (SQLite WAL while open)
  media/                        # explicit project-managed source copies
  cache/
    proxies/                    # regenerable proxy media
    waveforms/                  # regenerable waveform data
    thumbnails/                 # regenerable preview thumbnails
  models/                       # references only; model files are managed separately
  exports/                      # output files, never original inputs
  recovery/
    project-<id>-checkpoint-<revision>.sqlite3
    project-corrupt-<random>.sqlite3
```

The per-user application data directory contains `last-project.json`, config, and structured logs. The recent-project pointer stores a path relative to app data when possible, plus project ID, and is atomically replaced. Missing or invalid pointers simply leave the application without a recent project.

## SQLite entities

Schema v2 includes the following indexed tables (v1 project databases migrate transactionally on open):

- `projects`: ID, name, state revision, settings, update/last-opened times, and project timebase.
- `assets`: stable UUID, original URI/path mode, last-known path, size, mtime, SHA-256 fingerprint, duration/timebase, metadata, and relink status. Source paths are never used as IDs.
- `media_metadata`: probe metadata and the tool/version that produced it.
- `artifacts`: proxy/cache/output artifact path, source fingerprint, status/checksum, detector/generator ID/version, optional model ID/version/license, and configuration.
- `transcripts` and `transcript_words`: source asset, language, ASR/model provenance and fingerprint; every word has source in/out ticks, text, confidence, and correction/protected flags.
- `analyses`: typed source ranges and result JSON with detector/model provenance, confidence, status, and source fingerprint.
- `briefs` and `style_traits`: revisioned creative brief, structured constraints, and neutral style values with user/reference/default origin plus provenance.
- `edit_plans`: versioned suggestions and rationale with generator/model provenance.
- `timelines`, `timeline_items`, and `captions`: revisioned timelines and captions. Every timeline item has a required `asset_id`, source in/out ticks, and timeline start/duration ticks. Captions also retain source ranges through their timeline item. All ranges use integer ticks, never floating-point seconds.
- `jobs`: kind, lifecycle state, typed input/output JSON, stage/progress, attempts and retry policy, cancellation, idempotency key, committed output artifact, errors, and timestamps. `job_dependencies` stores prerequisite edges; `job_events` stores structured stage/lifecycle records.
- `models`: model role/name/version/source/license/checksum and optional local executable path.
- `exports`: timeline revision, destination relative to project, output format/settings, state/timestamps, checksum, and error.
- `autosaves`: bounded recent state snapshots with SHA-256 checksums, written in the same transaction as canonical state updates.
- `migrations_applied`: applied schema migration versions/timestamps.

Foreign keys are enabled on each connection, with WAL journaling, a busy timeout, and `synchronous=FULL`. Timeline time ranges are constrained in SQLite and validated before write. Original input references are registered by reading metadata and hashing; persistence never opens original media for writing, renames it, or deletes it.

## Paths and portability

Files inside the project directory use normalized relative POSIX paths. External linked source media uses a `file:` URI, a last-known absolute path for immediate relink, size/mtime, and SHA-256 fingerprint. If a linked file is moved, the stable asset ID stays the same; the app must request explicit relinking and verify the fingerprint rather than silently substituting media. The recent-project catalog uses relative paths when possible. Project movement is therefore portable for managed files; external linked sources may require relinking on another machine.

On last-project reopen, the service adds a quick source-availability status to each asset using existence, size, and modification time only; it does not hash large media on startup. Projects labels available, changed, and missing sources. Timeline clips referencing changed or missing assets show a degraded-state warning and point the user to Projects for relinking. This status is advisory: media jobs still verify the full SHA-256 before and after processing, and a size/mtime mismatch alone never silently replaces an asset.

## Atomicity, autosave, and recovery

Editable state uses one `BEGIN IMMEDIATE` transaction: validate input, append a timeline revision and mark it current, upsert supplied metadata/export/model/brief records by stable ID, update project revision, add a checksummed autosave, then commit. Job state uses dedicated queue transactions and does not create timeline revisions. Existing history is retained unless explicitly removed by a future deletion operation. A failed operation rolls back without changing the last valid state. JSON metadata and recent-project files are written to same-directory temporary files, flushed, and atomically replaced. SQLite backups use `Connection.backup()` into a temporary file, followed by atomic rename; live WAL databases are never copied directly.

The persistence suite includes 20 subprocess hard-termination trials. In each trial, the child opens a project, commits a newly accepted timeline edit through the production autosave path, then is killed while holding a separate uncommitted SQLite transaction. All 20 trials reopen with the latest accepted edit and transcript intact, the autosave revision present, and SQLite integrity valid; the measured loss of accepted work is zero seconds in these process-termination trials. This satisfies the defined 20-trial process-death recovery criterion. It does not simulate power loss, storage-device failure, or establish the real-world edit cadence between autosaves. When a checkpoint is restored, the project-open response includes its revision and last-update timestamp. The desktop shows a persistent warning for that open session that later changes may be missing, asks the user to review transcript and timeline state, and states that the exact loss interval is unknown.

The autosave journal keeps the latest 20 revision snapshots. A new project immediately receives an external revision-0 SQLite backup. After meaningful state edits, the store creates another consistent SQLite backup when the latest one is ten revisions behind or at least 30 seconds old. A completed backup is atomically published only after integrity and project-identity checks; the newest five are retained. Backup failure is written to the structured local log and does not roll back an already committed edit; the next edit retries when due. `ProjectStore.checkpoint()` can also create an immediate revisioned recovery copy. On open, the store checks schema and database integrity. If the main database is corrupt, it preserves the damaged generation under `recovery/`, restores the newest valid matching checkpoint, revalidates integrity and project ID, and continues. If recovery cannot find a valid checkpoint, the project is reported as corrupt and original media remains untouched. Cache artifacts are disposable and carry source fingerprints and generator provenance; missing or stale caches can be regenerated without modifying the timeline/source. The application catalog records the last project and startup attempts to reopen it.

## Persistent background jobs

The app data directory contains a separate `jobs.sqlite3` queue database, which coordinates configured global concurrency across all open projects. An OS-level lock on `jobs.lock` permits only one service process to own recovery and scheduling; the OS releases it if the process exits unexpectedly. Project-local `jobs`, `job_dependencies`, and `job_events` rows mirror each job’s state and provenance. Each worker claims a queued job under a short SQLite transaction; handlers do their long work outside database transactions. Dependencies release only after parents complete; failed or cancelled prerequisites fail dependents with a causal error.

Handlers implement typed input validation, `run(context, input)`, output validation, and a `retry_safe` declaration. Extension points are registered for proxy, transcription, analysis, model download, indexing, and rendering. A handler receives cooperative cancellation checks, progress/stage reporting, and a job-owned staging directory. Retry policies are bounded (maximum ten attempts) and apply only to explicit transient errors from retry-safe handlers, with exponential backoff. Configure `jobConcurrency` from 1 to 16 in `config.json`, or use `LOCAL_CUT_JOB_CONCURRENCY`.

Equivalent work is keyed by project, kind, canonical payload, source fingerprint, pipeline/generator/model versions, and relevant configuration. A completed artifact is reused only if the project database records it as ready and the file checksum matches. Outputs must be nonempty, remain inside the job staging root, and resolve to a path inside the project. The service validates the output, atomically moves it to the final project-relative path, then records artifact provenance and job completion. If the process stops between the project commit and queue acknowledgement, startup verifies the committed artifact and reconciles the queue. Otherwise, interrupted retry-safe jobs below their attempt limit return to retrying; non-retry-safe or exhausted work fails as interrupted. Cancelled and interrupted jobs clean only the exact staging directory named by a recorded job ID; unknown directories and committed outputs are preserved.

The queue uses a dedicated SQLite database so one job transition does not create unrelated timeline revisions. It and the project job mirror are separate databases; startup reconciliation handles the output-commit acknowledgement gap. Handlers must cooperatively poll cancellation. Proxy, transcription, model-download, analysis, proposal, and rendering handlers are implemented; the generic indexing extension point is reserved but not yet registered. See PROXY_PIPELINE.md for process containment, project-local staging, publication journaling and timestamp mapping.

## Interfaces and current limitations

`ProjectPersistence` defines the boundary. `ProjectStore` implements project create/open, asset registration, media metadata, generated artifact/transcript/analysis recording, timeline/settings autosave, checkpoint recovery, and recent-project reopen. MP4/MOV import now uses FFprobe to inspect container, video/audio streams, duration, dimensions, frame rates, timebase, rotation, and indicative variable-frame-rate evidence. It stores a reference to the source; it never copies or modifies originals. Folder scans skip hidden entries and symlink directories, sort candidates deterministically, deduplicate paths, and return per-file errors. Repeated imports resolve by normalized path or content hash while retaining stable asset IDs.

The Projects screen offers two explicit import modes: link media in place, or copy a managed byte-for-byte source into `<project>/media/`. Managed copies are written to a temporary sibling, flushed, hashed against the source before and after copying, and atomically renamed before the asset is registered. The selected original is never opened for writing. Both modes deduplicate by normalized source path/content fingerprint and keep a stable asset ID.

Relinking first verifies the selected MP4/MOV against the stored SHA-256. A content mismatch requires a separate confirmation in the UI; replacement is rejected if existing timeline source ranges would exceed the new duration or if a queued/running job references the asset. On confirmed replacement, the stable asset ID and timeline remain, but ready proxy/transcript/analysis artifacts are marked stale and the UI directs the user to regenerate them. The prior transcript remains stored for reference but is not presented as a current transcript. The replacement is linked in place; it is never copied over the prior file. Old managed files are retained as unreferenced project media rather than automatically deleted.

The app capability profile is a best-effort snapshot of OS/architecture, CPU, RAM, NVIDIA GPUs where `nvidia-smi` is available, FFmpeg/FFprobe versions and advertised codec/hardware lists, and free space on the app-data volume. Each field reports a source or unknown state. FFmpeg listings describe build capabilities only; actual device/driver operation is not probed. Proxy/model suggestions are conservative estimates; current transcription selects a model from RAM and invokes a replaceable local Whisper-compatible backend. Local H.264/AAC rendering is implemented, including user-supplied audio beds. Real-device performance, GPU operation, and export audio-sync acceptance remain release-validation work.

Directory artifacts use a deterministic recursive checksum and reject symlinks. Invalid cache checksums mark artifacts stale. The queue stores full input envelopes in `input_json` and outstanding rename intents in `publications`. Active staging is project-local; legacy app-data staging is cleaned during recovery. See [proxy pipeline](PROXY_PIPELINE.md).

## Schema v3: transcription review

Schema v3 adds immutable inference segments with separate editable text, word-to-segment references, analysis audio source origins, transcript artifact references, and required/excluded/uncertain source-range markers. Existing transcript words receive a review segment during migration. A job handler may supply a commit_output hook to register indexed information in the same transaction as validated artifact publication. Transcription uses that hook so a completed job cannot exist without its indexed transcript. See TRANSCRIPTION.md for units, model provenance, review behavior, recovery, and verification commands.

Settings offers a review-before-delete cleanup for registered proxy and detector-analysis artifacts under the current project's cache/proxies/ and cache/analysis/ paths. It shows each exact path and its size, blocks cleanup while project jobs are queued/running/retrying, and requires a second explicit confirmation against a snapshot token. The service revalidates that token, constrains deletion to nonsymlink paths below the project cache, and marks artifacts and analysis rows stale before removing files. Original and managed source media, transcript inference and corrections, proposals, exports, models, project history, and recovery checkpoints are preserved. If deletion is interrupted, stale files can be reviewed and removed on a later attempt; stale artifacts are never reused as ready cache.

## Timeline engine document v1

The transactional editor stores versioned tracks, source-referenced clips, captions, markers, and up to 50 reversible commands in `settings.timelineEngine`. The existing timeline and caption tables are updated in the same commit as the document and autosave journal. No source-less gap rows or manifest/database format change is required. See [timeline engine](TIMELINE_ENGINE.md) for validation, recovery, source provenance, and compatibility behavior.

## Timeline document v2 and revision suggestions

Timeline document v2 adds source protection and suggestion suppression policies to undo/redo snapshots; v1 histories migrate without discarding edits. Review proposals use isolated `edit_plans` records with `planType: timeline-revision`. Acceptance and undo update status, document, relational projection, revision and recovery journal atomically. See [editing revisions](EDITING_REVISIONS.md).
