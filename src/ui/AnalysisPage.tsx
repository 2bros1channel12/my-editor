import { useEffect, useRef, useState } from "react";
import { Activity, RefreshCw, ScanSearch } from "lucide-react";
import type { DesktopBridge } from "../bridge";
import type { AnalysisDetector, AnalysisResult, AnalysisWarning, JobRecord, LastProjectResponse } from "../contracts";

function signalLabel(item: AnalysisResult): string {
  const result = item.result;
  if (item.detectorId === "silence") {
    if (typeof result.meanQuietWindowRmsDbfs === "number" && typeof result.thresholdMarginDb === "number") {
      return `Mean quiet-window level ${result.meanQuietWindowRmsDbfs.toFixed(1)} dBFS RMS · ${result.thresholdMarginDb.toFixed(1)} dB below threshold · measured signal, not confidence`;
    }
    return "Measured silence level unavailable for this saved result";
  }
  if (item.detectorId === "thumbnails") return "Deterministic frame sample · no semantic score";
  if (item.detectorId === "audio_levels") {
    const rms = typeof result.rmsDbfs === "number" ? `${result.rmsDbfs.toFixed(1)} dBFS RMS` : "RMS unavailable";
    const peak = typeof result.peakDbfs === "number" ? `${result.peakDbfs.toFixed(1)} dBFS peak` : "peak unavailable";
    return `${rms} · ${peak}`;
  }
  if (item.detectorId === "near_black" && typeof result.darkPixelFraction === "number") return `Dark pixels ${Math.round(result.darkPixelFraction * 100)}%`;
  if (item.detectorId === "shot_boundaries" && typeof result.changeFraction === "number") return `Sampled frame change ${Math.round(result.changeFraction * 100)}%`;
  if (item.detectorId === "motion" && typeof result.meanFrameChange === "number") return `Mean pixel change ${Math.round(result.meanFrameChange * 100)}%`;
  if (item.detectorId === "blur" && typeof result.laplacianVariance === "number") return `Edge variance ${result.laplacianVariance.toFixed(1)}`;
  if (item.detectorId === "speech_regions" && typeof result.wordRatePerSecond === "number") return `${result.wordRatePerSecond.toFixed(1)} transcript words/s`;
  if (item.detectorId === "filler_words" && Array.isArray(result.terms)) return `Transcript match: ${result.terms.join(", ")} · exact token cue${item.confidence == null ? "" : ` · ASR token confidence ${Math.round(item.confidence * 100)}%, not detector confidence`}`;
  if (item.detectorId === "repeated_takes" && typeof result.textSimilarity === "number") return `Text similarity ${Math.round(result.textSimilarity * 100)}%`;
  if (item.confidence != null) return `Backend confidence ${Math.round(item.confidence * 100)}%`;
  return item.result.signalStrength == null ? "Signal unavailable" : `Detector signal ${Math.round(item.result.signalStrength * 100)}% · not calibrated confidence`;
}

function settingLabel(key: string): string {
  const known: Record<string, string> = {
    thresholdDbfs: "Silence entry threshold (dBFS)",
    hysteresisDb: "Hysteresis band (dB)",
    minimumMs: "Minimum quiet duration (ms)",
    mergeGapMs: "Merge gap (ms)",
  };
  return known[key] ?? key.replace(/[A-Z]/g, letter => ` ${letter.toLowerCase()}`);
}

function seekUnavailableReason(item: AnalysisResult): string | null {
  if (["changed_since_import", "unavailable", "missing_asset"].includes(item.sourceStatus ?? "")) {
    return "Seeking is disabled because the imported source is changed or unavailable. Relink the original, regenerate preview assets, and rerun this detector.";
  }
  if (item.validity && item.validity !== "integrity_ok") {
    const detail = item.validityReason ? ` ${item.validityReason}` : " Its input lineage is not current and validated.";
    return `Seeking is disabled for this saved result.${detail} Rerun this detector to review it against current media.`;
  }
  return null;
}

export default function AnalysisPage({ bridge, project, onNavigate }: { bridge: DesktopBridge; project: LastProjectResponse | null; onNavigate?: (section: "Projects" | "Editor", assetId?: string) => void }) {
  const [catalog, setCatalog] = useState<AnalysisDetector[]>([]);
  const [detectorId, setDetectorId] = useState("");
  const [runFilter,setRunFilter]=useState("latest");
  const [assetId, setAssetId] = useState("");
  const [settings, setSettings] = useState<Record<string, number>>({});
  const [results, setResults] = useState<AnalysisResult[]>([]);
  const [warnings, setWarnings] = useState<AnalysisWarning[]>([]);
  const [sourceStatus,setSourceStatus]=useState<AnalysisResult["sourceStatus"]>("unknown");
  const [prerequisites,setPrerequisites]=useState<{proxyReady:boolean;hasAudio:boolean;transcriptReady:boolean;detail?:string}>({proxyReady:false,hasAudio:false,transcriptReady:false});
  const [jobs, setJobs] = useState<JobRecord[]>([]);
  const [playback, setPlayback] = useState("");
  const [offset, setOffset] = useState(0);
  const [message, setMessage] = useState("");
  const video = useRef<HTMLVideoElement>(null);
  const refreshSequence = useRef(0);
  const active = catalog.find(item => item.id === detectorId);
  const path = project?.path;

  async function refresh(forceIntegrity = false) {
    const sequence=++refreshSequence.current;
    if (!path) return;
    try {
      const [analysis, queue, transcript] = await Promise.all([
        bridge.request("analysis_list", { path, assetId, ...(forceIntegrity ? { verifyIntegrity: true } : {}) }) as Promise<{ results: AnalysisResult[]; warnings?: AnalysisWarning[]; sourceStatus?: AnalysisResult["sourceStatus"]; prerequisites?: typeof prerequisites }>,
        bridge.request("job_list", { projectId: project!.projectId, limit: 50 }) as Promise<{ jobs: JobRecord[] }>,
        assetId ? bridge.request("transcript_list", { path, assetId }) as Promise<{ playbackUrl?: string; sourceMinusProxySeconds?: number }> : Promise.resolve({ playbackUrl: undefined, sourceMinusProxySeconds: undefined }),
      ]);
      if (sequence !== refreshSequence.current) return;
      setResults(analysis.results); setWarnings(analysis.warnings ?? []); setSourceStatus(analysis.sourceStatus ?? "unknown"); setPrerequisites(analysis.prerequisites ?? {proxyReady:false,hasAudio:false,transcriptReady:false}); setJobs(queue.jobs); setPlayback(transcript.playbackUrl ?? ""); setOffset(transcript.sourceMinusProxySeconds ?? 0);
    } catch (error) { if (sequence === refreshSequence.current) setMessage(error instanceof Error ? error.message : String(error)); }
  }
  useEffect(() => { void bridge.request("analysis_catalog").then(value => { const rows = (value as { detectors?: AnalysisDetector[] }).detectors; const safeRows = Array.isArray(rows) ? rows : []; setCatalog(safeRows); setDetectorId(old => old || safeRows[0]?.id || ""); }).catch(error => setMessage(String(error))); }, [bridge]);
  useEffect(() => { setAssetId(String(project?.state.assets[0]?.id ?? "")); setRunFilter("latest"); }, [project?.projectId]);
  useEffect(() => { setResults([]); setSourceStatus("unknown"); setPrerequisites({proxyReady:false,hasAudio:false,transcriptReady:false}); setSettings({}); setRunFilter("latest"); void refresh(); }, [path, assetId, bridge]);
  useEffect(() => { if (!path) return; const timer = window.setInterval(() => void refresh(), 2500); return () => window.clearInterval(timer); }, [path, assetId, bridge]);

  async function run() {
    if (!path || !active || !assetId) return;
    try { const result = await bridge.request("analysis_run", { path, assetId, detector: active.id, settings }); const job = result as { deduplicated?: boolean; state?: string };
      setMessage(job.deduplicated ? "A matching validated result already exists." : "Detector queued. Results appear after validation and commit."); await refresh();
    } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
  }
  function seek(ticks: number) { if (video.current) video.current.currentTime = Math.max(0, ticks / 1e6 - offset); video.current?.scrollIntoView?.({ block: "center", behavior: "smooth" }); }

  if (!project) return <div className="page-content"><h1>Footage analysis</h1><p>Open a project and import footage to review local detector suggestions.</p>{onNavigate && <button className="button secondary" onClick={() => onNavigate("Projects")}>Open Projects</button>}</div>;
  const detectorResults = results.filter(item => item.detectorId === detectorId);
  const detectorWarnings = warnings.filter(item => item.detectorId === detectorId);
  const runs = Array.from(new Map(detectorResults.map(item => [item.artifactId ?? item.id, { id: item.artifactId ?? item.id, createdAt: item.createdAt }])).values()).sort((a,b) => b.createdAt.localeCompare(a.createdAt));
  const latestRunId = runs[0]?.id;
  const visible = detectorResults.filter(item => runFilter === "all" || (runFilter === "latest" ? (item.artifactId ?? item.id) === latestRunId : (item.artifactId ?? item.id) === runFilter));
  const related = jobs.filter(job => job.kind === "analysis" && job.payload?.assetId === assetId);
  const prerequisiteMessage = !assetId ? "Import MP4 or MOV footage before running analysis." : sourceStatus === "changed_since_import" || sourceStatus === "unavailable" || sourceStatus === "missing_asset" ? "Relink or reimport the source before running detectors." : sourceStatus === "unknown" ? "Checking source and preview requirements…" : !prerequisites.proxyReady ? "Generate preview assets in Projects before running a detector." : active?.needsAudio && !prerequisites.hasAudio ? "This source has no usable audio-only preview; choose a detector that does not require audio." : active?.needsTranscript && !prerequisites.transcriptReady ? "Create a current transcript in Editor before running this detector." : "";
  const prerequisiteAction = !assetId || ["changed_since_import", "unavailable", "missing_asset"].includes(sourceStatus ?? "")
    ? { section: "Projects" as const, label: !assetId ? "Open Projects to import footage" : "Open Projects to relink source" }
    : assetId && sourceStatus !== "unknown" && !prerequisites.proxyReady
      ? { section: "Projects" as const, label: "Open Projects to generate preview assets" }
      : active?.needsTranscript && !prerequisites.transcriptReady
        ? { section: "Editor" as const, label: "Open Editor to create a transcript" }
        : null;
  const canRun = Boolean(assetId && active && !prerequisiteMessage);
  return <div className="page-content analysis-page">
    <div className="page-heading"><div><div className="eyebrow">LOCAL SIGNALS</div><h1>Footage analysis</h1><p>Run small detectors independently. Every result is a review suggestion; nothing is removed or changed automatically.</p></div><button className="button secondary" onClick={() => void refresh(true)}><RefreshCw size={14} /> Refresh</button></div>
    <section className="capability-panel analysis-controls">
      <label>Source footage <select aria-label="Analysis source footage" value={assetId} onChange={event => setAssetId(event.target.value)}>{project.state.assets.map(asset => <option key={String(asset.id)} value={String(asset.id)}>{String(asset.original_uri)}</option>)}</select></label>
      <label>Detector <select aria-label="Detector" value={detectorId} onChange={event => { setDetectorId(event.target.value); setSettings({}); setRunFilter("latest"); }}>{catalog.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
      <p>{active?.id === "silence" ? "Starts a candidate below the entry threshold. Hysteresis keeps it active through the higher exit level, and the merge gap bridges short interruptions. This is an audio-energy cue, not speech detection." : active?.needsAudio ? "Requires generated proxy audio. Silence and audio levels are energy measurements; they cannot confirm whether speech is present." : active?.needsVideo ? "Samples the validated proxy video. Results are review cues and may reflect motion, lighting, or framing." : active?.id === "filler_words" ? "Requires a local transcript with word timestamps. Exact um, uh, erm, and er token matches can be ASR mistakes or intentional hesitations." : active?.needsTranscript ? "Requires a local transcript with word timestamps. Transcript matches are possible speech, not a verified detection." : "Runs locally on this device."}</p>
      {active && Object.entries(active.settings).map(([key, [defaultValue, min, max]]) => <label className="analysis-setting" key={key}>{settingLabel(key)}<input aria-label={key} type="number" min={min} max={max} step={Number.isInteger(min) && Number.isInteger(max) ? 1 : 0.05} value={settings[key] ?? defaultValue} onChange={event => setSettings(previous => ({ ...previous, [key]: Number(event.target.value) }))} /><small>{min}–{max}</small></label>)}
      <button className="button primary" disabled={!canRun} onClick={() => void run()}><ScanSearch size={15} /> Run this detector</button>
      {prerequisiteMessage && <div className="analysis-prerequisite" role="status"><p>{prerequisiteMessage}</p>{prerequisiteAction && onNavigate && <button className="button secondary" onClick={() => onNavigate(prerequisiteAction.section, prerequisiteAction.section === "Editor" ? assetId : undefined)}>{prerequisiteAction.label}</button>}</div>}
      <p role="status">{message}</p>
    </section>
    {(sourceStatus === "changed_since_import" || sourceStatus === "unavailable" || sourceStatus === "missing_asset") && <div className="analysis-source-warning" role="alert"><b>{sourceStatus === "changed_since_import" ? "Source changed since import" : "Source is unavailable"}</b><span>These saved detector results are retained for review but may no longer match the selected source. Relink or reimport the correct media, regenerate preview assets, and rerun affected detectors before relying on them.</span></div>}
    {sourceStatus === "metadata_matches_import" && <p className="analysis-source-freshness">Source status checks file size and modification time. Detector jobs verify the source fingerprint before and after processing; this page does not rehash large recordings during refresh.</p>}
    {related.map(job => <article className="job-row" key={job.id}><div className="job-heading"><b>{String(job.payload?.detector ?? "analysis").replaceAll("_", " ")}</b><span className={`job-state state-${job.state}`}>{job.state}</span></div><div className="job-stage">{job.stage}{job.message ? ` · ${job.message}` : ""}</div><progress value={job.progress} max={1} aria-label={`${String(job.payload?.detector ?? "analysis").replaceAll("_", " ")} progress`}/><div className="job-meta"><span>{Math.round(job.progress * 100)}%</span>{["queued", "running", "retrying"].includes(job.state) && <button aria-label={`Cancel ${String(job.payload?.detector ?? "analysis").replaceAll("_", " ")} job ${job.id.slice(0,8)}`} onClick={async () => { await bridge.request("job_cancel", { jobId: job.id }); await refresh(); }}>Cancel</button>}</div>{job.error && <small className="job-error">{String(job.error.message ?? "Detector failed")}</small>}</article>)}
    {playback ? <video aria-label="Source preview" ref={video} src={playback} controls /> : <div className="empty-projects"><Activity size={17}/><div><b>Preview playback unavailable</b><span>Generate preview assets in Projects, then reopen this panel.</span></div></div>}
    <section className="analysis-results"><div className="section-heading"><div><h3>{active?.name ?? "Suggestions"}</h3><p>{visible.length} suggestion{visible.length === 1 ? "" : "s"} shown · {runs.length} cached run{runs.length === 1 ? "" : "s"}</p></div><label className="analysis-run-filter">Result run<select aria-label="Result run" value={runFilter} onChange={event => setRunFilter(event.target.value)}><option value="latest">Latest run</option><option value="all">All cached runs</option>{runs.map((run,index)=><option key={run.id} value={run.id}>{new Date(run.createdAt).toLocaleString()} · {run.id.slice(0,8)}{index===0?" · latest":""}</option>)}</select></label></div>
      {detectorWarnings.map(warning => <div className="analysis-source-warning" role="alert" key={`${warning.detectorId}:${warning.message}`}>{warning.message}</div>)}
      {visible.length ? visible.map(item => { const result = item.result; const seekBlocked = seekUnavailableReason(item); return <article className="analysis-result" key={item.id}>
        {item.thumbnailUrl && <img src={item.thumbnailUrl} alt="Uniformly sampled proxy frame" />}
        <div className="analysis-result-copy"><div className="job-heading"><b>{result.detectorName || item.detectorName.replace("localcut-", "").replaceAll("_", " ")}</b><small>{result.detectorVersion || item.detectorVersion}</small></div>
          <p>{result.explanation}</p><div className="analysis-range"><button disabled={Boolean(seekBlocked)} title={seekBlocked ?? undefined} aria-label={item.detectorId === "thumbnails" ? `At ${(item.sourceInTicks / 1e6).toFixed(2)}s` : `${(item.sourceInTicks / 1e6).toFixed(2)}–${(item.sourceOutTicks / 1e6).toFixed(2)}s`} onClick={() => seek(item.sourceInTicks)}>{item.detectorId === "thumbnails" || result.sampleIsPoint === true ? `At ${(item.sourceInTicks / 1e6).toFixed(2)}s` : `${(item.sourceInTicks / 1e6).toFixed(2)}–${(item.sourceOutTicks / 1e6).toFixed(2)}s`}</button>
          {typeof result.matchedSourceInTicks === "number" && <button disabled={Boolean(seekBlocked)} title={seekBlocked ?? undefined} onClick={() => seek(result.matchedSourceInTicks!)}>Compare with {(result.matchedSourceInTicks / 1e6).toFixed(2)}–{((result.matchedSourceOutTicks ?? 0) / 1e6).toFixed(2)}s</button>}
          <span>{signalLabel(item)}{item.confidence == null ? item.detectorId === "silence" && typeof result.meanQuietWindowRmsDbfs !== "number" ? " · rerun this detector for a measured level" : item.detectorId === "silence" || item.detectorId === "audio_levels" ? " · measurement only; confidence not provided" : " · confidence unavailable" : item.detectorId === "speech_regions" ? ` · ASR backend confidence ${Math.round(item.confidence * 100)}%` : item.detectorId === "filler_words" ? "" : ` · confidence ${Math.round(item.confidence * 100)}%`}</span></div></div>
        {seekBlocked && <p className="analysis-source-warning" role="status">{seekBlocked}</p>}
        {item.validity && item.validity !== "integrity_ok" && <div className="analysis-source-warning" role={item.validity === "invalid" ? "alert" : "status"}><b>{item.validity === "stale" ? item.validityReason?.startsWith("This result was created by detector version") ? "Detector version changed" : "Saved result uses stale inputs" : item.validity === "invalid" ? "Saved result artifact is invalid" : "Result lineage unavailable"}</b><span>{item.validityReason}</span></div>}
        <details className="analysis-provenance"><summary>Run settings and provenance</summary><code>{Object.entries(item.config ?? {}).map(([key,value])=>`${key}: ${String(value)}`).join(" · ")}</code><small>Source fingerprint {item.sourceFingerprint.slice(0,12)}… · artifact {item.artifactId?.slice(0,12) ?? "unavailable"}{item.validity === "integrity_ok" ? " · artifact checksum and recorded lineage checked" : ""}</small></details>
        {typeof result.matchedText === "string" && <div className="analysis-match"><b>Compared transcript passages</b><div><small>{String(result.textSimilarity == null ? "Text similarity unavailable" : `Text match ${Math.round(Number(result.textSimilarity) * 100)}%`)}</small><p>{String(result.text ?? "")}</p></div><div><small>{String(result.durationSimilarity == null ? "Duration similarity unavailable" : `Duration match ${Math.round(Number(result.durationSimilarity) * 100)}%`)}</small><p>{result.matchedText}</p></div></div>}
      </article>; }) : <div className="empty-projects"><div className="empty-icon"><ScanSearch size={18}/></div><div><b>No saved results</b><span>Run this detector after generating preview assets or a transcript if required.</span></div></div>}
    </section>
  </div>;
}
