import { useCallback, useEffect, useRef, useState } from "react";
import { Download, FileText, RefreshCw } from "lucide-react";
import type { DesktopBridge } from "../bridge";
import type { ExportRecord, JobRecord, LastProjectResponse } from "../contracts";

export default function ExportPanel({ bridge, project, hasClips, hasCaptions }: { bridge: DesktopBridge; project: LastProjectResponse | null; hasClips: boolean; hasCaptions: boolean }) {
  const [exports, setExports] = useState<ExportRecord[]>([]);
  const [message, setMessage] = useState("");
  const [jobId, setJobId] = useState("");
  const [progress, setProgress] = useState(0);
  const [burnCaptions, setBurnCaptions] = useState(false);
  const sequence = useRef(0);

  const refresh = useCallback(async () => {
    const current = ++sequence.current;
    if (!project?.path) { setExports([]); return; }
    try {
      const response = await bridge.request("export_list", { path: project.path }) as { exports?: ExportRecord[] } | null;
      if (current === sequence.current) setExports(Array.isArray(response?.exports) ? response.exports : []);
    } catch (error) {
      if (current === sequence.current) setMessage(error instanceof Error ? error.message : "Could not load export history.");
    }
  }, [bridge, project?.path]);

  useEffect(() => { setJobId(""); void refresh(); }, [refresh]);
  useEffect(() => {
    if (!jobId || !project) return;
    let alive = true;
    const poll = async () => {
      try {
        const result = await bridge.request("job_list", { projectId: project.projectId, limit: 100 }) as { jobs?: JobRecord[] } | null;
        if (!alive) return;
        const job = (Array.isArray(result?.jobs) ? result.jobs : []).find(row => row.id === jobId);
        if (!job) return;
        setProgress(job.progress);
        setMessage(`${job.stage}${job.message ? ` · ${job.message}` : ""} · ${Math.round(job.progress * 100)}%`);
        if (["completed", "failed", "cancelled"].includes(job.state)) {
          setJobId("");
          if (job.state === "completed") { setMessage(burnCaptions ? "H.264 export with burned-in captions validated and saved." : "H.264 export validated and saved in the project exports folder."); await refresh(); }
          else if (job.state === "failed") setMessage(String(job.error?.message ?? "Export failed. The existing successful exports were preserved."));
          else setMessage("Export cancelled. Partial output was discarded.");
        }
      } catch (error) { if (alive) setMessage(error instanceof Error ? error.message : "Export status unavailable."); }
    };
    void poll(); const timer = window.setInterval(() => void poll(), 1000);
    return () => { alive = false; window.clearInterval(timer); };
  }, [bridge, jobId, project, refresh]);

  async function exportVideo() {
    if (!project?.path || !hasClips) return;
    setMessage("Preparing validated timeline export…");
    try {
      const result = await bridge.request("export_generate", { path: project.path, burnCaptions }) as { id?: string | null; state: string; deduplicated?: boolean; output?: { relativePath?: string } };
      if (result.id && ["queued", "running", "retrying"].includes(result.state)) { setProgress(0); setJobId(result.id); }
      else if (result.state === "completed") { setMessage(`Export ready${result.output?.relativePath ? ` · ${result.output.relativePath}` : ""}`); await refresh(); }
      else setMessage(`Export ${result.state}.`);
    } catch (error) { setMessage(error instanceof Error ? error.message : "Export could not be queued."); }
  }

  async function exportSubtitles() {
    if (!project?.path) return;
    try {
      const result = await bridge.request("export_srt", { path: project.path }) as { relativePath: string; captionCount: number };
      setMessage(`${result.captionCount} captions saved as ${result.relativePath}.`); await refresh();
    } catch (error) { setMessage(error instanceof Error ? error.message : "SRT export failed."); }
  }

  async function cancel() {
    if (!jobId) return;
    try { await bridge.request("job_cancel", { jobId }); setMessage("Cancellation requested…"); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Could not cancel the export."); }
  }

  return <section className="capability-panel export-panel" aria-label="Export timeline">
    <div className="timeline-edit-head"><div><h3>Export</h3><p>Render the accepted video timeline locally to H.264 MP4. Originals and the editable project stay unchanged.</p></div><button className="button secondary" onClick={() => void refresh()} aria-label="Refresh export history"><RefreshCw size={14}/> Refresh</button></div>
    <aside className="export-limit-note" aria-label="Current render limits"><b>Current render limits:</b> video clips must be sequential and play at normal speed. Overlapping clips, speed changes, overlays, and cross-dissolves are not rendered.</aside>
    <label className="export-caption-mode"><input type="checkbox" disabled={!hasCaptions} checked={burnCaptions} onChange={event => setBurnCaptions(event.target.checked)}/> Burn accepted captions into MP4 (requires FFmpeg libass support){!hasCaptions && <small>Add timeline captions to enable.</small>}</label>
    <div className="timeline-actions"><button className="button primary" disabled={!hasClips || !!jobId} onClick={() => void exportVideo()}><Download size={14}/> Export H.264 MP4</button><button className="button secondary" disabled={!project} onClick={() => void exportSubtitles()}><FileText size={14}/> Export captions as SRT</button>{jobId && <button className="button secondary" onClick={() => void cancel()}>Cancel export</button>}</div>
    {message && <p role="status" className="export-message">{message}</p>}
    {jobId && <progress className="export-progress" max={1} value={progress} aria-label="Export in progress"/>}
    {exports.length > 0 && <div className="export-history" aria-label="Completed exports">{exports.map(item => { const avOffset = Number(item.settings?.audioVideoStartOffsetSeconds); const avValidated = item.settings?.audioVideoStartAlignmentValidated === true && Number.isFinite(avOffset); const endOffset = Number(item.settings?.audioVideoEndOffsetSeconds); const endValidated = item.settings?.audioVideoEndAlignmentValidated === true && Number.isFinite(endOffset); const formatOffset = (value: number) => new Intl.NumberFormat(undefined, { maximumFractionDigits: 1, signDisplay: "always" }).format(value * 1000); return <div className="capability-row" key={item.id}><b>{item.format.toUpperCase()}{item.settings?.burnCaptions === true ? " · CAPTIONS BURNED IN" : ""} · {item.status}</b><span title={item.relativePath}>{item.relativePath} · {item.completedAt ? new Date(item.completedAt).toLocaleString() : "in progress"}{avValidated ? ` · A/V start ${formatOffset(avOffset)} ms (within one frame)` : ""}{endValidated ? ` · A/V end ${formatOffset(endOffset)} ms (within one frame)` : ""}</span></div>; })}</div>}
  </section>;
}
