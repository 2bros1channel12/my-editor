import { useEffect, useState } from "react";
import type { DesktopBridge } from "../bridge";
import type { LastProjectResponse, ProjectCacheSnapshot } from "../contracts";

export default function CacheMaintenancePanel({ bridge, project }: { bridge: DesktopBridge; project: LastProjectResponse | null }) {
  const [snapshot, setSnapshot] = useState<ProjectCacheSnapshot | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const bytes = (value: number) => value < 1024 ** 3 ? (value / 1024 ** 2).toFixed(1) + " MB" : (value / 1024 ** 3).toFixed(2) + " GB";

  useEffect(() => { setSnapshot(null); setConfirming(false); setMessage(""); }, [project?.path]);

  async function review() {
    if (!project) return;
    setBusy(true); setMessage(""); setConfirming(false);
    try { setSnapshot(await bridge.request("project_cache_info", { path: project.path }) as ProjectCacheSnapshot); }
    catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  }

  async function clear() {
    if (!project || !snapshot?.confirmationToken) return;
    setBusy(true); setMessage("");
    try {
      const result = await bridge.request("project_cache_clear", { path: project.path, confirmationToken: snapshot.confirmationToken }) as {
        clearedCount: number; clearedBytes: number; failures?: Array<{ path: string; error: string }>; snapshot: ProjectCacheSnapshot;
      };
      setSnapshot(result.snapshot); setConfirming(false);
      setMessage(result.failures?.length
        ? "Removed " + result.clearedCount + " items; " + result.failures.length + " could not be removed. Review the remaining paths."
        : "Removed " + result.clearedCount + " items (" + bytes(result.clearedBytes) + ").");
    } catch (error) {
      setConfirming(false); setMessage(error instanceof Error ? error.message : String(error));
      try { setSnapshot(await bridge.request("project_cache_info", { path: project.path }) as ProjectCacheSnapshot); } catch { /* Keep the actionable error. */ }
    } finally { setBusy(false); }
  }

  return <section className="capability-panel cache-maintenance" aria-label="Project cache maintenance">
    <h3>Project cache</h3>
    <p>Review the generated preview and detector files before clearing them. Originals, managed source media, transcripts and corrections, exports, models, project history, and recovery data are preserved.</p>
    <div className="heading-actions">
      <button className="button secondary" disabled={!project || busy} onClick={() => void review()}>{busy ? "Checking cache…" : snapshot ? "Refresh cache list" : "Review project cache"}</button>
    </div>
    {!project && <small>Open a project to manage its generated cache.</small>}
    {snapshot && <>
      <div className="capability-row"><b>{snapshot.artifactCount} generated items · {bytes(snapshot.totalBytes)}</b><span>{snapshot.projectPath}</span></div>
      <p>Protected locations: {snapshot.protectedPaths.join(", ")}.</p>
      {snapshot.activeJobs.length > 0 && <p role="alert">Wait for active project jobs to finish: {snapshot.activeJobs.map(job => job.kind + " (" + job.state + ")").join(", ")}.</p>}
      {snapshot.entries.length > 0 && <details className="cache-path-list" open>
        <summary>Exact paths in this review ({snapshot.entries.length})</summary>
        <ul>{snapshot.entries.map(item => <li key={item.artifactId}><code>{item.path}</code><span>{bytes(item.sizeBytes)} · {item.kind} · {item.status}</span></li>)}</ul>
      </details>}
      {snapshot.entries.length === 0 && snapshot.activeJobs.length === 0 && <p>No registered preview or detector cache files need clearing.</p>}
      {confirming && snapshot.canClear && <div role="alert" className="cache-confirm">
        <b>Confirm cache deletion</b>
        <p>Delete the {snapshot.artifactCount} listed generated files ({bytes(snapshot.totalBytes)}) from this project? This cannot be undone; previews and detector results will need to be regenerated.</p>
        <div className="heading-actions">
          <button className="button secondary" disabled={busy} onClick={() => setConfirming(false)}>Keep cache</button>
          <button className="button danger" disabled={busy} onClick={() => void clear()}>Delete listed cache files</button>
        </div>
      </div>}
      {!confirming && snapshot.canClear && <button className="button secondary" onClick={() => setConfirming(true)}>Review deletion confirmation</button>}
    </>}
    {message && <p role="status">{message}</p>}
  </section>;
}
