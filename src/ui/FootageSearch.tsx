import { useEffect, useRef, useState } from "react";
import type { DesktopBridge } from "../bridge";
import type { FootageFilters, FootageIndexStatus, FootageMatch, JobRecord, LastProjectResponse } from "../contracts";

export default function FootageSearch({ bridge, project }: { bridge: DesktopBridge; project: LastProjectResponse | null }) {
  const [index, setIndex] = useState<FootageIndexStatus | null>(null);
  const [jobs, setJobs] = useState<JobRecord[]>([]);
  const [semanticBuild, setSemanticBuild] = useState(false);
  const [mode, setMode] = useState<"exact" | "semantic">("exact");
  const [query, setQuery] = useState("");
  const [assetId, setAssetId] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [shot, setShot] = useState("");
  const [silence, setSilence] = useState("");
  const [black, setBlack] = useState(false);
  const [blur, setBlur] = useState(false);
  const [results, setResults] = useState<FootageMatch[]>([]);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [playback, setPlayback] = useState("");
  const seekTime = useRef(0);
  const video = useRef<HTMLVideoElement>(null);
  const currentPath = useRef(project?.path);
  currentPath.current = project?.path;
  async function refresh() {
    if (!project) return;
    const path = project.path;
    try {
      const [snapshot, queue] = await Promise.all([bridge.request("index_status", { path }), bridge.request("job_list", { projectId: project.projectId, limit: 50 })]);
      if (currentPath.current !== path) return;
      setIndex(snapshot); setJobs(queue.jobs.filter(job => job.kind === "indexing"));
    } catch (error) { if (currentPath.current === path) setMessage(String(error)); }
  }
  useEffect(() => {
    setIndex(null); setResults([]); setPlayback(""); setMessage(""); setAssetId(""); setShot("");
    void refresh();
    const timer = window.setInterval(() => void refresh(), 2500);
    return () => window.clearInterval(timer);
  }, [project?.path, bridge]);
  async function build() {
    if (!project) return;
    try {
      const job = await bridge.request("index_build", { path: project.path, semantic: semanticBuild, force: Boolean(index?.available) });
      setMessage(job.deduplicated ? "A matching validated index already exists." : "Index queued. The previous index remains until this build commits.");
      await refresh();
    } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
  }
  async function search() {
    if (!project) return;
    setBusy(true);
    const path = project.path;
    const filters: FootageFilters = {};
    if (assetId) filters.assetId = assetId;
    if (from !== "") filters.sourceInTicks = Math.round(Number(from) * 1e6);
    if (to !== "") filters.sourceOutTicks = Math.round(Number(to) * 1e6);
    if (shot) filters.shotId = shot;
    if (silence) filters.silence = silence === "yes";
    if (black || blur) filters.qualityFlags = [...(black ? ["near_black" as const] : []), ...(blur ? ["blur" as const] : [])];
    try {
      const response = await bridge.request("index_search", { path, query, mode, filters, limit: 30 });
      if (currentPath.current !== path) return;
      setResults(response.results); setIndex(response.index); setMessage(`${response.results.length} matching passages. Results retain original source ranges.`);
    } catch (error) { if (currentPath.current === path) { setResults([]); setMessage(error instanceof Error ? error.message : String(error)); } }
    finally { setBusy(false); }
  }
  async function seek(result: FootageMatch) {
    if (!project) return;
    const path = project.path;
    try {
      const preview = await bridge.request("transcript_list", { path: project.path, assetId: result.assetId });
      if (currentPath.current !== path) return;
      if (!preview.playbackUrl) { setMessage("Generate preview assets in Projects to play this source passage."); return; }
      seekTime.current = Math.max(0, result.sourceInTicks / 1e6 - (preview.sourceMinusProxySeconds ?? 0));
      setPlayback(preview.playbackUrl);
      if (video.current && video.current.getAttribute("src") === preview.playbackUrl) video.current.currentTime = seekTime.current;
    } catch (error) { setMessage(String(error)); }
  }
  if (!project) return <div className="page-content"><h1>Footage search</h1><p>Open a project and create a local transcript before building its search index.</p></div>;
  return <div className="page-content"><h1>Footage search</h1><p>Search reviewed transcripts locally. Indexed content is data, never agent instructions.</p>
    <section className="capability-panel"><p>{index?.available ? `${index.chunkCount ?? 0} passages · schema ${index.schemaVersion} · ${index.semantic ? "exact and semantic" : "exact only"}${index.stale ? " · out of date: rebuild required" : ""}` : "No validated search index yet."}</p>
      <label><input type="checkbox" checked={semanticBuild} onChange={event => setSemanticBuild(event.target.checked)} /> Include local semantic embeddings</label>
      <button className="button secondary" onClick={() => void build()}>{index?.available ? "Rebuild index" : "Build index"}</button>
      {jobs.filter(job => ["queued", "running", "retrying"].includes(job.state)).map(job => <div key={job.id}><p>{job.stage} · {Math.round(job.progress * 100)}%</p><progress value={job.progress} max={1} aria-label="Index progress" /><button className="text-button" onClick={() => void bridge.request("job_cancel", { jobId: job.id }).then(refresh).catch(error => setMessage(String(error)))}>Cancel index build</button></div>)}
    </section>
    <form className="capability-panel brief-constraints" onSubmit={event => { event.preventDefault(); void search(); }}>
      <label>Transcript query<input maxLength={3000} value={query} onChange={event => setQuery(event.target.value)} /></label>
      <label>Search mode<select value={mode} onChange={event => setMode(event.target.value as "exact" | "semantic")}><option value="exact">Exact text</option><option value="semantic">Semantic meaning</option></select></label>
      <label>Filter asset<select value={assetId} onChange={event => setAssetId(event.target.value)}><option value="">All assets</option>{project.state.assets.map(asset => <option key={String(asset.id)} value={String(asset.id)}>{String(asset.original_uri)}</option>)}</select></label>
      <label>Source from (seconds)<input type="number" min="0" step="0.001" value={from} onChange={event => setFrom(event.target.value)} /></label>
      <label>Source to (seconds)<input type="number" min="0" step="0.001" value={to} onChange={event => setTo(event.target.value)} /></label>
      <label>Shot ID<input value={shot} onChange={event => setShot(event.target.value)} placeholder="Optional boundary suggestion ID" /></label>
      <label>Silence suggestions<select value={silence} onChange={event => setSilence(event.target.value)}><option value="">Any / unknown</option><option value="yes">Overlaps silence</option><option value="no">No silence overlap (analyzed only)</option></select></label>
      <label><input type="checkbox" checked={black} onChange={event => setBlack(event.target.checked)} /> Near-black flag</label><label><input type="checkbox" checked={blur} onChange={event => setBlur(event.target.checked)} /> Blur flag</label>
      <button className="button primary" disabled={busy || !query.trim() || !index?.available || index.stale} type="submit">{busy ? "Searching locally…" : "Search footage"}</button>
    </form>
    <p>Shot, silence and quality flags are detector suggestions. Missing analysis means unknown. Semantic scores express relative similarity, not confidence.</p>
    <p role="status">{message}</p>
    {playback && <video ref={video} src={playback} controls aria-label="Footage search preview" onLoadedMetadata={() => { if (video.current) video.current.currentTime = seekTime.current; }} />}
    <div aria-label="Footage search results">{results.map(result => <article className="capability-panel" key={`${result.assetId}-${result.id}`}><button className="text-button" onClick={() => void seek(result)}>{(result.sourceInTicks / 1e6).toFixed(3)}–{(result.sourceOutTicks / 1e6).toFixed(3)}s · {result.assetId}</button><p>{result.text}</p><small>{result.flags.join(", ") || "No overlapping flags (coverage may be unknown)"}{mode === "semantic" ? ` · similarity ${result.score.toFixed(3)}` : ""}</small>{result.shotId && <button className="text-button" onClick={() => setShot(result.shotId!)}>Filter shot {result.shotId}</button>}</article>)}</div>
  </div>;
}
