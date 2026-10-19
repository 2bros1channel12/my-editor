import { useEffect, useRef, useState } from "react";
import type { DesktopBridge } from "../bridge";
import type { LastProjectResponse, RuntimeStatus } from "../contracts";

export default function LocalRuntimePanel({ bridge, project }: { bridge: DesktopBridge; project: LastProjectResponse | null }) {
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const [message, setMessage] = useState("");
  const [summary, setSummary] = useState("");
  const [busy, setBusy] = useState(false);
  const [assetId, setAssetId] = useState("");
  const activeSource = useRef("");
  activeSource.current = `${project?.path ?? ""}:${assetId}`;
  async function refresh() {
    try { setStatus(await bridge.request("runtime_status")); }
    catch (error) { setMessage(String(error)); }
  }
  useEffect(() => { void refresh(); }, [bridge]);
  useEffect(() => { setAssetId(String(project?.state.assets[0]?.id ?? "")); setSummary(""); setMessage(""); }, [project?.path]);
  async function summarize() {
    if (!project || !assetId) return;
    setBusy(true); setSummary("");
    const source = activeSource.current;
    try {
      const result = await bridge.request("runtime_summarize", { path: project.path, assetId });
      if (activeSource.current !== source) return;
      setSummary(result.data.summary);
      setMessage(`${result.provenance.modelName} · ${result.provenance.modelVersion} · ${result.provenance.executionMs.toFixed(0)} ms · review suggestion`);
    } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  }
  return <section className="capability-panel" aria-label="Local model runtime">
    <div className="section-heading"><h3>Local model runtime</h3><button className="button secondary" onClick={() => void refresh()}>Refresh runtime</button></div>
    <p>{status?.available ? `Connected to ${status.provider} at ${status.endpoint ?? "loopback"}.` : status?.detail ?? "Checking the local provider…"}</p>
    <p>Uses configured, installed local models. No automatic downloads or remote fallback. Configure modelRuntimeModels and modelRuntimeUrl in the local config file; see docs/MODEL_RUNTIME_AND_INDEX.md.</p>
    {(["generation", "embedding"] as const).map(task => { const selected = status?.selections?.[task]; return <div className="capability-row" key={task}><b>{task === "generation" ? "Structured JSON model" : "Local embedding model"}</b><span>{selected?.available ? `${selected.name} · ${selected.version}` : selected?.detail ?? "No available model"}{selected?.memoryAdvisory && <small>{selected.memoryAdvisory}</small>}</span></div>; })}
    {project && <><label>Summary source<select aria-label="Summary source" value={assetId} onChange={event => { setAssetId(event.target.value); setSummary(""); }}>{project.state.assets.map(asset => <option key={String(asset.id)} value={String(asset.id)}>{String(asset.original_uri)}</option>)}</select></label><button className="button secondary" disabled={busy || !assetId || !status?.selections?.generation?.available} onClick={() => void summarize()}>{busy ? "Generating locally…" : "Suggest transcript summary"}</button></>}
    {summary && <p role="note">{summary}</p>}
    <p role="status">{message}</p>
  </section>;
}
