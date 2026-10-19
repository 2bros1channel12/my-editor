# Local model runtime and searchable footage

## Using the components

The Search page builds a local transcript index for the open project. Start with exact search, which needs no language or embedding model. To enable semantic search, configure an installed local embedding model, check **Include local semantic embeddings**, then rebuild. Results link to original source ranges and can seek the validated preview. Index builds appear in Jobs with progress, cancellation, retries and restart recovery. Models and Settings show the local runtime, selected generation/embedding models, and actionable unavailable/low-memory states. Models also offers a bounded, schema-validated transcript summary as a review suggestion; it performs no edit.

No model is downloaded automatically. Install and administer a local Ollama server and acquire suitable model files separately. Configure `%LOCALAPPDATA%/LocalCut/config.json`, then restart the app. The model names below are placeholders: use the exact installed names reported by your provider and set conservative RAM budgets for their model files and context sizes.

```json
{
  "modelRuntimeUrl": "http://127.0.0.1:11434",
  "modelRuntimeTimeoutSeconds": 30,
  "modelRuntimeModels": [
    {"name": "YOUR_INSTALLED_SMALL_GENERATION_MODEL", "task": "generation", "minimumRamBytes": 4294967296, "settings": {"temperature": 0, "num_ctx": 2048, "num_predict": 512}},
    {"name": "YOUR_INSTALLED_EMBEDDING_MODEL", "task": "embedding", "minimumRamBytes": 2147483648, "settings": {"num_ctx": 2048}}
  ]
}
```

`LOCAL_CUT_MODEL_URL` and `LOCAL_CUT_MODEL_TIMEOUT` override endpoint and timeout. Endpoints must be numeric loopback HTTP addresses with no credentials, extra path, query or fragment. Redirects, environment HTTP proxies, cloud-tagged models and inventory entries advertising remote models are rejected. There is no pull, download, remote fallback or model-supplied endpoint path. The external provider must itself be configured to perform local inference; a loopback connection alone cannot prove what an independently administered server does internally.

## Runtime boundary

`model_runtime.LocalProvider` is replaceable. `interfaces.InferenceProvider` aliases this protocol. The first adapter implements Ollama's installed-model inventory, schema-formatted chat, and batched embedding endpoints. It uses structured JSON HTTP payloads rather than command strings. See the primary [Ollama structured-output documentation](https://github.com/ollama/ollama/blob/main/docs/capabilities/structured-outputs.mdx) and [API specification](https://github.com/ollama/ollama/blob/main/docs/openapi.yaml) for the provider contract. A llama.cpp adapter can implement the same interface; it is not included in this slice.

Model selection intersects explicitly configured candidates with installed local inventory and records the installed digest as the model version. It selects the largest configured RAM budget that fits the memory capability profile; if available RAM is unknown it selects the smallest candidate with an advisory. The same OS memory probe feeds Settings, speech recognition and this runtime. Reported GPU inventory is not assumed to prove inference support: this initial path sets `num_gpu: 0`. Configured RAM budgets are estimates, not guarantees against memory pressure from other applications. The speech and language runtimes share an inference lock, and backend memory failures give a smaller-model/close-other-apps message.

Effective settings are allowlisted and bounded: temperature, context length, output token limit, seed, CPU thread count, and CPU-only GPU setting. Default requests explicitly set a 2,048-token context, at most four CPU threads, and (for generation) temperature zero, seed zero and a 512-token output limit; overrides and defaults are both recorded. Requests and responses are bounded to 4 MiB, timeouts to 120 seconds, and HTTP cancellation is checked at short intervals. Cancellation closes the client connection; the independent server may continue computation briefly. Custom provider implementations must honor bounded cancellation themselves.

Generation requires an application-owned JSON Schema and strict local validation with `jsonschema` Draft 2020-12. Provider schema hints do not replace validation. Fenced/trailing JSON, duplicate keys, nonfinite/overflow numbers, schema violations, remote schema references and tool calls are rejected. The public summary RPC uses a fixed closed schema, not a schema or task selected from imported footage. Embeddings must match input count, contain finite numeric nonzero vectors, and have consistent bounded dimensions. Model identity and effective settings are checked across execution; changed model tags cannot be reported under an earlier digest.

Successful results record provider/adapter version, model name/digest, effective settings, execution duration and task. Index snapshots retain batch execution provenance. Content-free rotating execution records are written to `<dataDir>/logs/model-executions.jsonl`; failures record codes and duration. Inputs, outputs, transcripts, filenames and reference text are excluded from these audit records. Runtime failure codes distinguish unavailable provider/model, timeout, invalid output and insufficient memory. Invalid output and memory failures are not silently retried with a different model.

## Immutable index storage

Each successful build registers a `footage-index` artifact at `cache/footage-index/<jobId>.sqlite3`. The file contains:

- `metadata`: project ID, source/review fingerprint, schema version 1, chunk version 2, pipeline version, creation time, embedding name/digest/provider/settings, vector dimensions, chunk count and batch execution provenance.
- `chunks`: stable passage ID, asset ID, source in/out microsecond ticks, case-folded literal search text, and JSON containing transcript ID, reviewed text, shot label, heuristic flags, detector coverage and optional normalized embedding vector.
- An asset/time index for range filtering.

The core project schema needs no migration: its existing artifact registry supplies provenance, checksums, atomic publication, cache lookup and cleanup. A generation is never updated in place. The previous committed generation remains intact during rebuilds; cancellation, invalid vectors, crashes and failed publication cannot replace it. Reads verify the artifact checksum, open SQLite read-only/immutable, and fall back to a prior intact artifact if the newest is corrupt. Schema/chunk/pipeline changes invalidate reuse. Explicit rebuilds get a fresh identity; ordinary builds deduplicate equivalent valid inputs.

Fingerprints include stable source IDs/hashes/durations, latest ready reviewed transcript segments **and words**, transcript artifact lineage, relevant committed detector results/settings/versions and artifact checksums, and current detector algorithm versions. Semantic cache identity additionally includes provider, model digest and effective settings. A changed transcript, source, detector run or embedding revision requires rebuild. Source availability, size and modification time are checked on status/search; these are not fresh media hashes. Workers hash sources with cancellation before and after building. Referenced transcript/detector artifacts are checksum-validated. A source changed while preserving all file metadata may evade a quick status check, but cannot pass a fresh worker hash.

## Search and source-time meaning

Exact search is case-insensitive literal substring matching against reviewed transcript chunks. SQL parameters keep quotes, wildcards, SQL/FTS syntax and hostile strings as content. Semantic search uses actual local embeddings and cosine similarity with the same provider/model revision/settings as the index. It streams candidates from SQLite and retains a bounded ranked result list; no token-hash approximation is labeled semantic search. Stopping the provider leaves exact search usable. A model/version mismatch or unavailable provider produces an actionable semantic-search error.

Explicit segment corrections take precedence. Otherwise reviewed word corrections are incorporated while retaining the segment's source bounds. Word-only or uncovered words form bounded phrases at real word boundaries; their first/last timestamps define the range. No finer timestamps are invented after a text edit. A passage longer than 2,000 characters fails actionably rather than being split at guessed times. Builds are limited to 50,000 chunks; queries to 3,000 characters and 100 results (the UI requests 30). Semantic embedding batches contain at most 16 passages. A conservative 256 MiB retained-vector memory budget rejects oversized builds actionably while preserving the prior index. Passage preparation checks cancellation as well as hashing, embedding and SQLite writes.

Filters intersect by asset, source interval, shot ID, silence overlap and near-black/blur flags. Source ranges are half-open: `chunk.in < filter.out && chunk.out > filter.in`, so touching endpoints do not overlap. Shot IDs label the shot containing the passage start; a transcript passage may span a coarse shot-boundary interval. The initial shot gets a stable ID only when shot analysis exists. Flags remain suggestions, not proof of defects. `silence: false` includes only passages with silence-analysis coverage, rather than treating missing analysis as clean audio. Quality filters require each requested flag.

## Untrusted content

Transcripts, filenames, metadata, references and search strings are never inserted into system instructions, tool definitions, SQL syntax, endpoint paths, executable commands or response schemas. Generation has a fixed system instruction and one serialized untrusted-content message. Embedding operations receive only content strings and have no instruction/tool channel. Snippets and summaries render as React text. Model results have no dispatch or automatic-edit path. JSON/schema validation is not a claim that model text is factual; summaries and matches remain review material.

## Development checks and limits

```powershell
service/.venv/Scripts/python.exe -m pip install -e 'service[test]'
service/.venv/Scripts/python.exe -m pytest service/tests/test_model_runtime.py service/tests/test_footage_index.py service/tests/test_runtime_service.py service/tests/test_service.py service/tests/test_jobs.py -q
npm test -- --run
npm run build
cargo test --manifest-path src-tauri/Cargo.toml --lib
```

Focused tests use mocked local providers/vectors and HTTP connections, plus real SQLite/project/job persistence. They cover hostile indexed strings, schema rejection, model failures, hardware selection, timestamp/filter boundaries, cancellation, corruption fallback, provenance and cache invalidation. Real model quality, representative long-footage performance, alternate adapters, GPU inference, and complete installed-app validation remain release work. The Python sidecar build now installs runtime dependencies and collects JSON Schema specification resources; its new dependency notices still require distribution review before release.
