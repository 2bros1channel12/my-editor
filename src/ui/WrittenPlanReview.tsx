import { useEffect, useRef, useState } from "react";
import type { DesktopBridge } from "../bridge";
import type { CreativeBrief, JobRecord, LastProjectResponse, WrittenPlanRecord } from "../contracts";

const seconds = (ticks: number) => (ticks / 1e6).toFixed(3);
type ReviewAction = "approve" | "reject" | "replace" | "reorder" | "lock" | "unlock";

export default function WrittenPlanReview({ bridge, project, onProjectRefresh }: { bridge: DesktopBridge; project: LastProjectResponse | null; onProjectRefresh?: () => Promise<void> }) {
  const [briefs, setBriefs] = useState<CreativeBrief[]>([]);
  const [briefId, setBriefId] = useState("");
  const [assetId, setAssetId] = useState("");
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<"exact" | "semantic">("exact");
  const [plans, setPlans] = useState<WrittenPlanRecord[]>([]);
  const [selectedPlanId,setSelectedPlanId] = useState('');
  const [jobs, setJobs] = useState<JobRecord[]>([]);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [playback, setPlayback] = useState("");
  const [alternatives, setAlternatives] = useState<Record<string, string>>({});
  const video = useRef<HTMLVideoElement>(null);
  const seekTime = useRef(0);
  const path = project?.path;
  const activePath = useRef(path);
  activePath.current = path;
  const generation = useRef(0);
  async function refresh() {
    if (!project) return;
    const token = generation.current;
    try {
      const [savedBriefs, savedPlans, queue] = await Promise.all([
        bridge.request("brief_list", { path: project.path }), bridge.request("planning_list", { path: project.path }),
        bridge.request("job_list", { projectId: project.projectId, limit: 50 }),
      ]);
      if (activePath.current !== project.path || token !== generation.current) return;
      setBriefs(savedBriefs.briefs); setBriefId(current => current || savedBriefs.briefs[0]?.id || "");
      setPlans(savedPlans.plans); setJobs(queue.jobs.filter(job => job.kind === "planning"));
    } catch (error) { if (activePath.current === project.path && token === generation.current) setMessage(String(error)); }
  }
  useEffect(() => {
    generation.current += 1;
    setBriefs([]); setBriefId(""); setPlans([]); setJobs([]); setQuery(""); setPlayback(""); setAlternatives({}); setMessage(""); setBusy(false);
    setAssetId(String(project?.state.assets[0]?.id ?? ""));
    void refresh();
    const timer = window.setInterval(() => void refresh(), 2500);
    return () => window.clearInterval(timer);
  }, [path, bridge]);
  const matchingPlans=plans.filter(item=>item.briefId===briefId&&item.plan.assetId===assetId);
  const record = matchingPlans.find(item=>item.id===selectedPlanId)??matchingPlans[0];
  const activeJobs = jobs.filter(job => ["queued", "running", "retrying"].includes(job.state));
  async function generate() {
    if (!project) return;
    const token = generation.current;
    setBusy(true);
    try {
      const result = await bridge.request("planning_generate", { path: project.path, briefId, assetId, ...(query.trim() ? { query: query.trim(), searchMode: mode } : {}) });
      if (token !== generation.current || activePath.current !== project.path) return;
      setMessage(result.deduplicated ? "An equivalent validated written plan is available." : "Written plan queued. Review it before constructing a timeline."); await refresh();
    } catch (error) { if (token === generation.current) setMessage(String(error)); }
    finally { if (token === generation.current) setBusy(false); }
  }
  async function generateOutputs(){if(!project)return;const token=generation.current;setBusy(true);try{const r=await bridge.request('planning_outputs',{path:project.path,briefId,assetId,...(query.trim()?{query:query.trim(),searchMode:mode}:{})});if(token!==generation.current)return;setMessage(`${r.jobs.length} separate written output plans queued. Review each version independently.`);await refresh();}catch(e){if(token===generation.current)setMessage(String(e));}finally{if(token===generation.current)setBusy(false);}}
  async function review(action: ReviewAction, sectionId?: string, candidateId?: string, order?: string[]) {
    if (!project || !record || record.stale) return;
    const token = generation.current;
    setBusy(true);
    try {
      const response = await bridge.request("planning_review", { path: project.path, planId: record.id, action, expectedReviewVersion: record.plan.reviewVersion, ...(sectionId ? { sectionId } : {}), ...(candidateId ? { candidateId } : {}), ...(order ? { order } : {}) });
      if (token !== generation.current || activePath.current !== project.path) return;
      setPlans(current => current.map(item => item.id === response.plan.id ? response.plan : item));
      setMessage("Written plan decision saved. The timeline has not changed.");
    } catch (error) { if (token === generation.current) { setMessage(String(error)); await refresh(); } }
    finally { if (token === generation.current) setBusy(false); }
  }
  async function compileApproved() {
    if (!project || !record || record.stale) return;
    const sectionIds = record.plan.sections.filter(section => section.status === "approved").map(section => section.id);
    if (!sectionIds.length) return;
    const token = generation.current;
    setBusy(true);
    try {
      const current = await bridge.request("timeline_get", { path: project.path });
      if (token !== generation.current || activePath.current !== project.path) return;
      await bridge.request("timeline_compile", { path: project.path, planId: record.id, sectionIds, expectedReviewVersion: record.plan.reviewVersion, expectedRevision: current.timeline.revision });
      if (token !== generation.current || activePath.current !== project.path) return;
      setMessage("Approved sections appended to the timeline. Open Editor to review the cut or undo it.");
      await onProjectRefresh?.();
    } catch (error) { if (token === generation.current) setMessage(String(error)); }
    finally { if (token === generation.current) setBusy(false); }
  }
  async function cancel(jobId: string) {
    const token = generation.current;
    const submittedPath = path;
    try {
      await bridge.request("job_cancel", { jobId });
      if (generation.current === token && activePath.current === submittedPath) await refresh();
    } catch (error) {
      if (generation.current === token && activePath.current === submittedPath) setMessage(String(error));
    }
  }
  async function seek(candidateId: string) {
    const candidate = record?.plan.candidates.find(item => item.id === candidateId);
    if (!project || !candidate) return;
    const token = generation.current;
    try {
      const preview = await bridge.request("transcript_list", { path: project.path, assetId: candidate.assetId });
      if (token !== generation.current || activePath.current !== project.path) return;
      if (!preview.playbackUrl) { setMessage("Generate a proxy to preview this passage."); return; }
      seekTime.current = Math.max(0, candidate.sourceInTicks / 1e6 - (preview.sourceMinusProxySeconds ?? 0)); setPlayback(preview.playbackUrl);
      if (video.current && video.current.getAttribute("src") === preview.playbackUrl) video.current.currentTime = seekTime.current;
    } catch (error) { if (token === generation.current) setMessage(String(error)); }
  }
  function move(index: number, direction: number) {
    if (!record || index + direction < 0 || index + direction >= record.plan.sections.length || record.plan.sections[index].locked || record.plan.sections[index + direction].locked) return;
    const order = record.plan.sections.map(section => section.id);
    [order[index], order[index + direction]] = [order[index + direction], order[index]];
    void review("reorder", undefined, undefined, order);
  }
  if (!project) return <section className="capability-panel"><h2>Written rough-cut plan</h2><p>Open a project and save a brief before planning.</p></section>;
  const disabled = busy || Boolean(record?.stale);
  function passage(candidateId: string) {
    const candidate = record?.plan.candidates.find(item => item.id === candidateId);
    if (!candidate) return <p>Source evidence unavailable.</p>;
    return <div><button className="text-button" onClick={() => void seek(candidateId)}>{seconds(candidate.sourceInTicks)}–{seconds(candidate.sourceOutTicks)}s · {candidate.assetId}</button><small>Source ticks: {candidate.sourceInTicks} to {candidate.sourceOutTicks} (1,000,000 ticks/second)</small><blockquote>{candidate.excerpt}</blockquote>{candidate.transcriptEdited && <div><small>Reviewed transcript text; original inference retained below.</small><blockquote>{candidate.originalText}</blockquote></div>}{candidate.uncertain && <p>Uncertain source timing: review playback.</p>}{candidate.flags.length > 0 && <p>Detector suggestions: {candidate.flags.join(", ")}</p>}</div>;
  }
  return <section className="capability-panel"><h2>Written rough-cut plan</h2><p>Review a source-backed hook and outline. Review decisions save this plan. Use the separate append action to compile approved sections into the timeline.</p>
    <div className="brief-constraints"><label>Planning brief<select value={briefId} onChange={event => { setBriefId(event.target.value); setAlternatives({}); setPlayback(""); }}>{briefs.map(brief => <option key={brief.id} value={brief.id}>Revision {brief.revision} · {brief.constraints.objective}</option>)}</select></label><label>Planning source<select value={assetId} onChange={event => { setAssetId(event.target.value); setAlternatives({}); setPlayback(""); }}>{project.state.assets.map(asset => <option key={String(asset.id)} value={String(asset.id)}>{String(asset.last_known_path ?? asset.original_uri)}</option>)}</select></label><label>Optional evidence search<input value={query} onChange={event => setQuery(event.target.value)} maxLength={3000}/></label><label>Planning search mode<select value={mode} onChange={event => setMode(event.target.value as "exact" | "semantic")}><option value="exact">Exact transcript text</option><option value="semantic">Local semantic search</option></select></label></div>
    <button className="button primary" disabled={busy || !briefId || !assetId || activeJobs.length > 0} onClick={() => void generate()}>Generate written plan</button><button className="button secondary" onClick={() => void refresh()}>Refresh written plans</button>
    {(briefs.find(b=>b.id===briefId)?.constraints.outputProfiles?.length??0)>1&&<button disabled={busy||!assetId||activeJobs.length>0} onClick={()=>void generateOutputs()}>Generate plans for every reviewed output</button>}
    {matchingPlans.length>1&&<label>Output plan version<select aria-label="Output plan version" value={record?.id??''} onChange={e=>{setSelectedPlanId(e.target.value);setAlternatives({});setPlayback('');}}>{matchingPlans.map(p=><option key={p.id} value={p.id}>{p.plan.outputProfile?.name??'Source format'} · output {(p.plan.outputIndex??0)+1} · revision {p.revision}</option>)}</select></label>}
    {record?.plan.outputProfile&&<p aria-label="Plan output constraints">{record.plan.outputProfile.name} · {record.plan.outputProfile.width} × {record.plan.outputProfile.height} · {record.plan.outputProfile.frameRate} fps · {record.plan.outputProfile.targetDurationSeconds}s target / {record.plan.outputProfile.maxDurationSeconds}s maximum · safe margins {Object.entries(record.plan.outputProfile.safeArea).map(([k,v])=>`${k} ${Math.round(v*100)}%`).join(', ')}</p>}
    {activeJobs.map(job => <article key={job.id}><p>{job.stage} · {Math.round(job.progress * 100)}% · {job.message}</p><progress max={1} value={job.progress} aria-label="Planning progress"/><button onClick={() => void cancel(job.id)}>Cancel planning</button></article>)}
    {jobs.filter(job => job.state === "failed").slice(0, 3).map(job => <p role="alert" key={job.id}>Planning failed: {String(job.error?.message ?? job.message ?? "Open Jobs for details, resolve the cause, and generate again.")}</p>)}
    {playback && <video ref={video} src={playback} controls aria-label="Written plan source preview" onLoadedMetadata={() => { if (video.current) video.current.currentTime = seekTime.current; }}/>}<p role="status">{message}</p>
    {!record && !activeJobs.length && <p>No written plan for this brief and source. Generate one after transcription and local model setup.</p>}{record && <div><h3>Plan revision {record.revision} · {record.status}</h3>{record.stale && <p role="alert">Plan is stale: {record.staleReason || "Source evidence changed. Generate a new written plan."}</p>}<p>Estimated selected duration: {seconds(record.plan.estimatedDurationTicks)}s{record.plan.targetDurationTicks !== null ? ` · target ${seconds(record.plan.targetDurationTicks)}s · difference ${seconds(record.plan.estimatedDurationTicks - record.plan.targetDurationTicks)}s` : ""}</p><p>Model: {record.plan.provenance.modelName} · {record.plan.provenance.modelVersion}</p>
      {record.plan.warnings.map((warning, i) => <p key={i} role="note">{warning}</p>)}<h3>Proposed hook</h3><p>{record.plan.hook?.reason}</p>{record.plan.hook?.candidateId ? passage(record.plan.hook.candidateId) : <p>No supported hook selected.</p>}
      <button className="button primary" disabled={disabled || !record.plan.sections.some(section => section.status === "approved")} onClick={() => void compileApproved()}>Append approved sections to timeline</button><h3>Unmet requirements</h3>{record.plan.unmetRequirements.length ? <ul>{record.plan.unmetRequirements.map((item, i) => <li key={i}>{record.plan.requirements.find(requirement => requirement.id === item.requirementId)?.description || item.requirementId}: {item.reason}</li>)}</ul> : <p>No unmet requirements reported; verify the evidence before approval.</p>}
      <ol aria-label="Written plan sections">{record.plan.sections.map((section, index) => <li key={section.id}><article className="proposal-operation"><h3>{section.title} · {section.status}{section.locked ? " · locked" : ""}</h3><p>{section.purpose}</p><p>{seconds(section.estimatedDurationTicks)}s · editorial confidence {Math.round(section.confidence * 100)}%</p>{section.selections.map(selection => <div key={selection.candidateId}>{passage(selection.candidateId)}<p>{selection.reason}</p><small>Editorial confidence {Math.round(selection.confidence * 100)}%: {selection.confidenceReason}</small></div>)}<p>Requirements supported: {section.requirementsSatisfied.map(id => record.plan.requirements.find(item => item.id === id)?.description || id).join("; ") || "None claimed"}</p>
        <div className="brief-actions"><button disabled={disabled || section.locked} onClick={() => void review("approve", section.id)}>Approve {section.title}</button><button disabled={disabled || section.locked} onClick={() => void review("reject", section.id)}>Reject {section.title}</button><button disabled={disabled} onClick={() => void review(section.locked ? "unlock" : "lock", section.id)}>{section.locked ? "Unlock" : "Lock"} {section.title}</button><button disabled={disabled || index === 0 || section.locked || record.plan.sections[index - 1]?.locked} onClick={() => move(index, -1)}>Move {section.title} up</button><button disabled={disabled || index === record.plan.sections.length - 1 || section.locked || record.plan.sections[index + 1]?.locked} onClick={() => move(index, 1)}>Move {section.title} down</button></div>
        <p>{section.alternativeNote}</p>{section.alternatives.map(selection => <details key={selection.candidateId}><summary>Alternative {selection.candidateId}</summary>{passage(selection.candidateId)}<p>{selection.reason}</p><small>{selection.confidenceReason}</small></details>)}{section.alternatives.length > 0 && <div><label>Replacement for {section.title}<select value={alternatives[section.id] || ""} onChange={event => setAlternatives(current => ({ ...current, [section.id]: event.target.value }))}><option value="">Choose alternative</option>{section.alternatives.map(item => <option key={item.candidateId} value={item.candidateId}>{record.plan.candidates.find(candidate => candidate.id === item.candidateId)?.excerpt || item.candidateId}</option>)}</select></label><button disabled={disabled || section.locked || !alternatives[section.id]} onClick={() => void review("replace", section.id, alternatives[section.id])}>Replace {section.title}</button></div>}
      </article></li>)}</ol></div>}
  </section>;
}
