import TranscriptReview from "./TranscriptReview";
import AnalysisPage from "./AnalysisPage";
import TimelineEditor from "./TimelineEngineEditor";
import BriefProposalPanel from "./BriefProposalPanel";
import CacheMaintenancePanel from "./CacheMaintenancePanel";
import RevisionSuggestions from "./RevisionSuggestions";
import WrittenPlanReview from "./WrittenPlanReview";
import FootageSearch from "./FootageSearch";
import LocalRuntimePanel from "./LocalRuntimePanel";
import FirstRunChecks from './FirstRunChecks';
import PrivacyStoragePanel from './PrivacyStoragePanel';
import { useEffect, useRef, useState, type MouseEvent } from "react";
import { Activity, AudioLines, BriefcaseBusiness, ChevronDown, CircleHelp, Clapperboard, Cpu, FolderOpen, HardDrive, Layers3, Settings2, ShieldCheck, Sparkles, WifiOff, Upload, RefreshCw, ScanSearch } from "lucide-react";
import { desktopBridge, loadHealth, type DesktopBridge } from "../bridge";
import type { CapabilityProfile, HealthCheck, JobRecord, LastProjectResponse } from "../contracts";

type Section = "Projects" | "Editor" | "Plans" | "Search" | "Analysis" | "Jobs" | "Models" | "Settings";
const sections: { label: Section; icon: typeof FolderOpen }[] = [
  { label: "Projects", icon: FolderOpen },
  { label: "Editor", icon: Clapperboard },
  { label: "Plans", icon: Layers3 },
  { label: "Search", icon: ScanSearch },
  { label: "Analysis", icon: ScanSearch },
  { label: "Jobs", icon: Activity },
  { label: "Models", icon: Cpu },
  { label: "Settings", icon: Settings2 },
];

export default function App({ bridge }: { bridge?: DesktopBridge }) {
  const [timelineEpoch, setTimelineEpoch] = useState(0);
  const [section, setSection] = useState<Section>("Projects");
  const [health, setHealth] = useState<Record<string, HealthCheck> | null>(null);
  const [checking, setChecking] = useState(false);
  const [lastProject, setLastProject] = useState<LastProjectResponse | null>(null);
  const [projectMessage, setProjectMessage] = useState("Looking for a recent project…");
  const [capabilities, setCapabilities] = useState<CapabilityProfile | null>(null);
  const [importMessage, setImportMessage] = useState("");
  const [jobs, setJobs] = useState<JobRecord[]>([]);
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const helpTrigger = useRef<HTMLButtonElement | null>(null);
  const helpClose = useRef<HTMLButtonElement | null>(null);
  const [transcriptTargetAssetId, setTranscriptTargetAssetId] = useState<string | undefined>();
  const [newProjectName, setNewProjectName] = useState("Untitled project");
  const [pendingReplacement, setPendingReplacement] = useState<{ assetId: string; candidatePath: string } | null>(null);
  const activeBridge = bridge ?? desktopBridge;

  function navigateFromAnalysis(nextSection: "Projects" | "Editor", assetId?: string) {
    if (nextSection === "Editor") setTranscriptTargetAssetId(assetId);
    setSection(nextSection);
  }

  function openHelp(event: MouseEvent<HTMLButtonElement>) {
    helpTrigger.current = event.currentTarget;
    setHelpOpen(true);
  }

  function closeHelp() {
    setHelpOpen(false);
    window.setTimeout(() => helpTrigger.current?.focus(), 0);
  }

  async function refresh() {
    setChecking(true);
    const result = await loadHealth(bridge);
    setHealth(result as unknown as Record<string, HealthCheck>);
    setChecking(false);
  }

  async function reopenProject() {
    try {
      const result = await (bridge ?? desktopBridge).request("project_open_last") as LastProjectResponse;
      setLastProject(result);
      setProjectMessage("");
    } catch {
      setLastProject(null);
      setProjectMessage("Create a project to import footage and start editing.");
    }
  }

  async function refreshAfterRevision() { await reopenProject(); setTimelineEpoch(value => value + 1); }
  async function createSample() {
    setProjectMessage('Creating the local synthetic sample…');
    const sample = await activeBridge.request('sample_create');
    const opened = await activeBridge.request('project_open', {path:sample.path});
    setLastProject(opened); setTimelineEpoch(n=>n+1); setProjectMessage(sample.notice);
  }

  async function createProject() {
    const name = newProjectName.trim();
    if (!name) { setProjectMessage("Enter a project name."); return; }
    if (/[\\/:*?\"<>|]/.test(name) || name.endsWith(".") || name.endsWith(" ")) { setProjectMessage("Choose a name without Windows path characters or a trailing dot or space."); return; }
    if (!activeBridge.selectProjectParent) { setProjectMessage("Project folder selection is available in the desktop app."); return; }
    try {
      const parent = await activeBridge.selectProjectParent(); if (!parent) return;
      const path = `${parent.replace(/[\\/]+$/, "")}/${name}.localcut`;
      await activeBridge.request("project_create", { path, name, settings: { projectName: name } });
      setNewProjectOpen(false); await reopenProject();
      setImportMessage("Project created locally. Import MP4 or MOV footage to begin.");
    } catch (error) { setProjectMessage(error instanceof Error ? error.message : "Project could not be created."); }
  }

  async function openProject() {
    if (!activeBridge.selectProjectInput) {
      setImportMessage("Opening a native project folder is available in the desktop app.");
      return;
    }
    try {
      const path = await activeBridge.selectProjectInput();
      if (!path) return;
      const result = await activeBridge.request("project_open", { path });
      setLastProject(result);
      setProjectMessage("");
      setImportMessage("Project opened locally. Continue editing its saved brief and timeline in Editor.");
    } catch (error) {
      setImportMessage(error instanceof Error ? error.message : "The selected folder could not be opened as a Local Cut project.");
    }
  }

  async function importMedia(storageMode: "linked" | "managed" = "linked") {
    const activeBridge = bridge ?? desktopBridge;
    if (!lastProject || !activeBridge.selectMediaInputs) return;
    const inputs = await activeBridge.selectMediaInputs();
    if (!inputs?.length) return;
    try {
      const result = await activeBridge.request("media_import", { path: lastProject.path, inputs, storageMode }) as { imported: unknown[]; errors: Array<{ path: string; code: string; message: string }> };
      const duplicateCount = result.imported.filter((item) => (item as { status?: string }).status === "duplicate").length;
      setImportMessage(`${result.imported.length - duplicateCount} ${storageMode === "managed" ? "copied into project" : "linked in place"} · ${duplicateCount} duplicate${duplicateCount === 1 ? "" : "s"} · ${result.errors.length} error${result.errors.length === 1 ? "" : "s"}${result.errors.length ? ` — ${result.errors.map((error) => `${error.path}: ${error.message}`).join("; ")}` : ""}`);
    } catch (error) {
      setImportMessage(error instanceof Error ? error.message : "Media import failed.");
    }
    await reopenProject();
  }

  async function relinkMedia(assetId: string, candidatePath?: string, allowChanged = false) {
    if (!lastProject) return;
    const selected = candidatePath ?? await activeBridge.selectRelinkInput?.();
    if (!selected) return;
    try {
      const result = await activeBridge.request("media_relink", { path: lastProject.path, assetId, candidatePath: selected, allowChanged }) as { status: string };
      if (result.status === "confirmation_required") { setPendingReplacement({ assetId, candidatePath: selected }); return; }
      setPendingReplacement(null);
      setImportMessage(result.status === "replaced" ? "Source replaced. Existing generated analysis, transcript and preview artifacts were marked stale; regenerate them before use." : "Source relinked. Its content fingerprint matched the imported media.");
      await reopenProject();
    } catch (error) { setImportMessage(error instanceof Error ? error.message : "Media relink failed."); }
  }

  async function generateProxy(assetId: string) {
    if (!lastProject) return;
    try {
      const result = await activeBridge.request("proxy_generate", { path: lastProject.path, assetId }) as { deduplicated?: boolean; state: string };
      setImportMessage(result.deduplicated ? "Preview assets already available or queued." : "Preview generation queued. Progress is available in Jobs.");
      await refreshJobs();
    } catch (error) {
      setImportMessage(error instanceof Error ? error.message : "Preview generation failed.");
    }
  }

  async function refreshCapabilities() {
    try { setCapabilities(await (bridge ?? desktopBridge).request("capabilities") as CapabilityProfile); }
    catch { setCapabilities(null); }
  }

  async function refreshJobs() {
    try {
      const result = await activeBridge.request("job_list", lastProject ? { projectId: lastProject.projectId, limit: 30 } : { limit: 30 }) as { jobs: JobRecord[] };
      setJobs(result.jobs);
    } catch { setJobs([]); }
  }

  useEffect(() => { void refresh(); void reopenProject(); void refreshCapabilities(); }, [bridge]);
  useEffect(() => { if (section === "Jobs") void refreshJobs(); }, [section, lastProject?.projectId]);
  useEffect(() => {
    if (section !== "Jobs") return;
    const timer = window.setInterval(() => void refreshJobs(), 1500);
    return () => window.clearInterval(timer);
  }, [section, lastProject?.projectId]);
  useEffect(() => {
    if (!helpOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === "Escape") closeHelp(); };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [helpOpen]);

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">Skip to main content</a>
      <aside className="sidebar">
        <div className="brand"><div className="brand-mark"><AudioLines size={20} strokeWidth={2.2} /></div><span>local cut</span><span className="brand-beta">BETA</span></div>
        <div className="workspace-label">WORKSPACE</div>
        <button className="workspace-select"><span className="workspace-avatar">E</span><span className="workspace-name">Eli’s workspace<small>Personal library</small></span><ChevronDown size={15} /></button>
        <div className="nav-label">LIBRARY</div>
        <nav aria-label="Main navigation">
          {sections.map(({ label, icon: Icon }) => <button key={label} type="button" aria-current={section === label ? "page" : undefined} className={`nav-item ${section === label ? "selected" : ""}`} onClick={() => { if (label === "Editor") setTranscriptTargetAssetId(undefined); setSection(label); }}><Icon size={18} strokeWidth={1.8} /><span>{label}</span>{label === "Jobs" && <span className="nav-counter">{jobs.filter((job) => ["queued", "running", "retrying"].includes(job.state)).length}</span>}</button>)}
        </nav>
        <div className="sidebar-bottom">
          <div className="privacy-card"><div className="privacy-icon"><ShieldCheck size={16} /></div><div><b>Your media stays yours</b><span>Editing and inference stay local. Model downloads are explicit.</span></div><WifiOff size={14} className="privacy-wifi" /></div>
          <button className="help-link" aria-haspopup="dialog" onClick={openHelp}><CircleHelp size={16} /><span>Help & shortcuts</span></button>
          <div className="user-row"><div className="user-avatar">E</div><div className="user-name">Eli <small>Local account</small></div><button className="icon-button" aria-label="Account options"><Settings2 size={16} /></button></div>
        </div>
      </aside>

      <main id="main-content" className="main-panel" tabIndex={-1}>
        <header className="topbar"><div className="breadcrumbs"><span>Workspace</span><span className="crumb-divider">/</span><b>{section}</b></div><div className="topbar-right"><div className="local-badge" title="Media, projects, and inference remain on this device. Explicit model downloads contact their disclosed download host."><span /> LOCAL PROCESSING</div><button className="top-icon" aria-label="Help & shortcuts" aria-haspopup="dialog" onClick={openHelp}><CircleHelp size={17} /></button><div className="top-avatar">E</div></div></header>
        {helpOpen && <div className="help-backdrop"><section className="capability-panel help-dialog" role="dialog" aria-modal="true" aria-labelledby="help-title" onKeyDown={event => { if (event.key === "Tab") { event.preventDefault(); helpClose.current?.focus(); } }}><div className="timeline-edit-head"><h2 id="help-title">Help & shortcuts</h2><button ref={helpClose} className="button secondary" onClick={closeHelp} autoFocus>Close help</button></div><p>Use the Editor to review the transcript, adjust source ranges, and save a nondestructive timeline.</p><h3>Timeline shortcuts</h3><dl><div><dt>Undo last timeline edit</dt><dd><kbd>Ctrl</kbd>/<kbd>⌘</kbd> + <kbd>Z</kbd></dd></div><div><dt>Redo timeline edit</dt><dd><kbd>Ctrl</kbd>/<kbd>⌘</kbd> + <kbd>Shift</kbd> + <kbd>Z</kbd></dd></div></dl><p>Shortcuts are ignored while you type in a text or number field.</p></section></div>}
        {lastProject?.recovery && <aside className="recovery-warning" role="alert"><b>Project recovered from checkpoint · revision {lastProject.recovery.checkpointRevision}</b><span>{lastProject.recovery.message} Last valid project update: {new Date(lastProject.recovery.checkpointUpdatedAt).toLocaleString()}. The exact missing-work interval cannot be determined. Review the recovered timeline and transcript before continuing.</span></aside>}
        {section==='Projects'&&<FirstRunChecks health={health} profile={capabilities} onCheck={()=>{void refresh();void refreshCapabilities();}} onModels={()=>setSection('Models')} onSample={createSample}/>}
        {(section==='Settings'||section==='Models')&&<PrivacyStoragePanel bridge={activeBridge} project={lastProject} onDeleted={()=>{setLastProject(null);setJobs([]);setTimelineEpoch(n=>n+1);setProjectMessage('Project deleted. External linked originals were preserved.');setSection('Projects');}}/>}
        {section === "Projects" ? <HealthPage health={health} checking={checking} refresh={refresh} onOpenSettings={() => setSection("Settings")} lastProject={lastProject} projectMessage={projectMessage} importMessage={importMessage} onReopenProject={reopenProject} onOpenProject={() => void openProject()} onImportMedia={importMedia} onRelinkMedia={relinkMedia} pendingReplacement={pendingReplacement} onCancelReplacement={() => setPendingReplacement(null)} onConfirmReplacement={() => pendingReplacement && void relinkMedia(pendingReplacement.assetId, pendingReplacement.candidatePath, true)} onGenerateProxy={generateProxy} onCreateProject={() => setNewProjectOpen(true)} creatingProject={newProjectOpen} projectName={newProjectName} setProjectName={setNewProjectName} createProject={() => void createProject()} onCancelCreate={() => setNewProjectOpen(false)} /> : section === "Settings" ? <><SettingsPage profile={capabilities} refresh={refreshCapabilities} /><LocalRuntimePanel bridge={activeBridge} project={lastProject} /><CacheMaintenancePanel bridge={activeBridge} project={lastProject} /></> : section === "Jobs" ? <JobsPage jobs={jobs} refresh={refreshJobs} onCancel={async (id) => { await activeBridge.request("job_cancel", { jobId: id }); await refreshJobs(); }} onRetry={async (id) => { await activeBridge.request("job_retry", { jobId: id }); await refreshJobs(); }} /> : section === "Plans" ? <WrittenPlanReview bridge={activeBridge} project={lastProject} onProjectRefresh={reopenProject}/> : section === "Search" ? <FootageSearch bridge={activeBridge} project={lastProject}/> : section === "Analysis" ? <AnalysisPage bridge={activeBridge} project={lastProject} onNavigate={navigateFromAnalysis} /> : section === "Editor" ? <><BriefProposalPanel bridge={activeBridge} project={lastProject} onProjectRefresh={refreshAfterRevision}/><RevisionSuggestions bridge={activeBridge} project={lastProject} onProjectRefresh={refreshAfterRevision}/><TimelineEditor key={timelineEpoch} bridge={activeBridge} project={lastProject} onProjectRefresh={reopenProject}/><TranscriptReview bridge={activeBridge} project={lastProject} initialAssetId={transcriptTargetAssetId}/></> : <><LocalRuntimePanel bridge={activeBridge} project={lastProject}/><TranscriptReview bridge={activeBridge} project={lastProject} modelsOnly /></>}
      </main>
    </div>
  );
}

function HealthPage({ health, checking, refresh, onOpenSettings, lastProject, projectMessage, importMessage, onReopenProject, onOpenProject, onImportMedia, onRelinkMedia, pendingReplacement, onCancelReplacement, onConfirmReplacement, onGenerateProxy, onCreateProject, creatingProject, projectName, setProjectName, createProject, onCancelCreate }: { health: Record<string, HealthCheck> | null; checking: boolean; refresh: () => void; onOpenSettings: () => void; lastProject: LastProjectResponse | null; projectMessage: string; importMessage: string; onReopenProject: () => void; onOpenProject: () => void; onImportMedia: (mode?: "linked" | "managed") => Promise<void>; onRelinkMedia: (assetId: string, path?: string, allowChanged?: boolean) => Promise<void>; pendingReplacement: { assetId: string; candidatePath: string } | null; onCancelReplacement: () => void; onConfirmReplacement: () => void; onGenerateProxy: (assetId: string) => void; onCreateProject: () => void; creatingProject: boolean; projectName: string; setProjectName: (name: string) => void; createProject: () => void; onCancelCreate: () => void }) {
  const rows = [
    { key: "ui", name: "Desktop interface", sub: "React workspace", icon: Layers3, source: "Built in" },
    { key: "service", name: "Local service", sub: "Python process", icon: Activity, source: "Service" },
    { key: "ffmpeg", name: "FFmpeg", sub: "Media tools", icon: Clapperboard, source: "System" },
    { key: "storage", name: "Local storage", sub: "Project & cache folder", icon: HardDrive, source: "Device" },
  ];
  const ready = health ? rows.filter(({ key }) => key !== "ffmpeg").every(({ key }) => health[key]?.available) : false;
  return (
    <div className="page-content">
      <div className="page-heading"><div><div className="eyebrow">YOUR PRIVATE STUDIO</div><h1>Good morning, Eli <span className="heading-sparkle">✳</span></h1><p>Your workspace is ready when you are.</p></div><div className="heading-actions"><button className="button secondary" onClick={onOpenSettings}><Settings2 size={16} /> Settings</button><button className="button primary" onClick={onCreateProject}><Sparkles size={16} /> New project</button></div></div>

      {creatingProject && <section className="capability-panel create-project"><h3>Create a local project</h3><p>Choose a name and a folder. Local Cut creates a new <code>.localcut</code> project folder there.</p><label>Project name<input aria-label="Project name" value={projectName} onChange={event => setProjectName(event.target.value)}/></label><p role="status">{projectMessage}</p><div className="heading-actions"><button className="button secondary" onClick={onCancelCreate}>Cancel</button><button className="button primary" onClick={createProject}>Choose folder and create</button></div></section>}

      <section className="welcome-card"><div className="welcome-copy"><div className="welcome-kicker"><span className="kicker-line" /> A QUIETER WAY TO CREATE</div><h2>Your next great cut<br />starts <em>right here.</em></h2><p>Bring in a lesson or talking-head recording. Local Cut will help shape a first cut while your footage stays on your device.</p><button className="button welcome-button" onClick={onCreateProject}>Start a project</button><div className="welcome-foot"><ShieldCheck size={14} /> Private by design <span>·</span> Media stays local; model downloads are explicit</div></div><div className="welcome-visual" aria-hidden="true"><div className="visual-sun"/><div className="visual-horizon"/><div className="visual-ridge ridge-back"/><div className="visual-ridge ridge-front"/><div className="visual-play"><span /></div><div className="visual-caption">A LITTLE SPACE TO THINK</div><div className="visual-orbit orbit-one"/><div className="visual-orbit orbit-two"/><div className="visual-grain"/></div></section>

      <section className="health-section"><div className="section-heading"><div><h3>System health</h3><p>Media processing and inference run locally. Model downloads require an explicit action.</p></div><button className="text-button" onClick={refresh} disabled={checking}><Activity size={15} className={checking ? "spin" : ""} /> {checking ? "Checking…" : "Refresh checks"}</button></div>
        <div className="health-grid">{rows.map(({ key, name, sub, icon: Icon, source }) => <HealthTile key={key} icon={<Icon size={17} />} name={name} sub={sub} status={health?.[key]} source={source} />)}</div>
        <div className={`health-summary ${ready ? "all-ready" : "needs-setup"}`}><div className="summary-icon">{ready ? <ShieldCheck size={17} /> : <Activity size={17} />}</div><span>{ready ? "All systems ready" : "A few things need attention"}</span><span className="summary-detail">{ready ? "You can start a new project." : "The interface is up. Check the service and media tools below."}</span><span className="summary-dot" /></div>
      </section>
      <section className="recent-section"><div className="section-heading"><div><h3>Recent projects</h3><p>Your work is saved locally on this device.</p></div><div className="heading-actions"><button className="button secondary" onClick={onOpenProject}><FolderOpen size={14} /> Open project</button><button className="text-button muted" onClick={onReopenProject}>Reopen last <span>↻</span></button>{lastProject && <><button className="button secondary" onClick={() => void onImportMedia("linked")}><Upload size={14} /> Link MP4 / MOV</button><button className="button secondary" onClick={() => void onImportMedia("managed")}><Upload size={14} /> Copy into project</button></>}</div></div>{importMessage && <p role="status" className="import-result">{importMessage}</p>}{lastProject?.state ? <div className="project-card"><div className="empty-icon"><BriefcaseBusiness size={19} /></div><div className="project-card-copy"><b>{String(lastProject.state.settings.projectName ?? "Recent project")}</b><span>{lastProject.state.assets.length} source assets · revision {lastProject.state.revision}</span></div><span className="project-path">{lastProject.path}</span></div> : <div className="empty-projects"><div className="empty-icon"><BriefcaseBusiness size={19} /></div><div><b>Your projects will live here</b><span>{projectMessage}</span></div></div>}</section>
      {pendingReplacement && <section className="capability-panel" role="alert"><h3>Confirm changed source</h3><p>The selected file does not match the original fingerprint. Replacing it keeps the stable asset ID and timeline ranges, but marks generated transcripts, previews and analyses stale. Existing timeline source ranges must fit the replacement duration.</p><div className="heading-actions"><button className="button secondary" onClick={onCancelReplacement}>Keep current source</button><button className="button primary" onClick={onConfirmReplacement}>Replace and invalidate generated artifacts</button></div></section>}
      {lastProject?.state.assets.map((asset) => { const sourceStatus = String(asset.sourceStatus ?? "unknown"); const statusLabel = sourceStatus === "available" ? "Source available" : sourceStatus === "changed_since_import" ? "Source changed since import" : sourceStatus === "unavailable" ? "Source missing or unavailable" : "Source status unchecked"; return <div className={`project-card media-project-card ${sourceStatus}`} key={String(asset.id)}><span className="project-path">{String(asset.original_uri)}</span><b className="media-source-status">{statusLabel}</b><button className="button secondary" onClick={() => void onRelinkMedia(String(asset.id))}>Relink source…</button><button className="button secondary" onClick={() => onGenerateProxy(String(asset.id))}>Generate preview assets</button></div>; })}
      <footer className="page-footer"><span>LOCAL CUT <b>0.1.0</b></span><span><span className="footer-lock">⌑</span> Your files never leave this device</span><span>Built for thoughtful work <span className="footer-star">✳</span></span></footer>
    </div>
  );
}

function HealthTile({ icon, name, sub, status, source }: { icon: React.ReactNode; name: string; sub: string; status?: HealthCheck; source: string }) {
  const available = status?.available ?? false;
  const detail = status?.detail ?? "Checking availability…";
  return <article className="health-tile"><div className="tile-top"><div className="tile-icon">{icon}</div><span className={`status-pill ${available ? "online" : status ? "offline" : "checking"}`}><i />{available ? "Available" : status ? "Not ready" : "Checking"}</span></div><h4>{name}</h4><p>{sub}</p><div className="tile-divider"/><div className="tile-bottom"><span>{source}</span><span className="tile-detail" title={detail}>{available ? (status?.version ?? detail) : detail}</span></div></article>;
}

function JobsPage({ jobs, refresh, onCancel, onRetry }: { jobs: JobRecord[]; refresh: () => void; onCancel: (id: string) => Promise<void>; onRetry: (id: string) => Promise<void> }) {
  const visible = jobs.slice(0, 20);
  return <div className="page-content"><div className="page-heading"><div><div className="eyebrow">LOCAL WORK QUEUE</div><h1>Jobs</h1><p>Background tasks and their latest progress.</p></div><button className="button secondary" onClick={refresh}><RefreshCw size={14} /> Refresh</button></div><section className="jobs-panel" aria-label="Background jobs">{visible.length ? visible.map((job) => <article className="job-row" key={job.id}><div className="job-heading"><b>{job.kind.replaceAll("_", " ")}</b><span className={`job-state state-${job.state}`}>{job.state}</span></div><div className="job-stage">{job.stage}{job.message ? ` · ${job.message}` : ""}</div><progress value={job.progress} max={1} aria-label={`${job.kind} progress`} /><div className="job-meta"><span>{Math.round(job.progress * 100)}%{job.attempts ? ` · attempt ${job.attempts}/${job.maxAttempts}` : ""}</span>{job.state === "running" || job.state === "queued" || job.state === "retrying" ? <button className="text-button muted" aria-label={`Cancel ${job.kind.replaceAll("_", " ")} job ${job.id.slice(0,8)}`} onClick={() => void onCancel(job.id)}>Cancel</button> : job.state === "failed" || job.state === "cancelled" ? <button className="text-button muted" aria-label={`Retry ${job.kind.replaceAll("_", " ")} job ${job.id.slice(0,8)}`} onClick={() => void onRetry(job.id)}>Retry</button> : null}</div>{job.error && <small className="job-error">{String(job.error.message ?? job.error.code ?? "Job failed")}</small>}</article>) : <div className="empty-projects"><div className="empty-icon"><Activity size={19} /></div><div><b>No background jobs yet</b><span>Proxies, transcription, analysis, indexing, model downloads and rendering will appear here.</span></div></div>}</section></div>;
}

function SettingsPage({ profile, refresh }: { profile: CapabilityProfile | null; refresh: () => void }) {
  const bytes = (value: number | null | undefined) => value == null ? "Unknown" : `${(value / 1024 ** 3).toFixed(1)} GB`;
  return <div className="page-content"><div className="page-heading"><div><div className="eyebrow">LOCAL DEVICE</div><h1>Settings</h1><p>Hardware and media-tool capabilities for future model and proxy choices.</p></div><button className="button secondary" onClick={refresh}><RefreshCw size={14} /> Refresh profile</button></div>{!profile ? <div className="empty-projects"><b>Capability profile unavailable</b><span>The local service could not collect this device’s profile.</span></div> : <><div className="capability-grid"><CapabilityCard title="Operating system" value={`${profile.platform.system} ${profile.platform.release}`} detail={`${profile.platform.machine} · ${profile.platform.pythonBits}`} /><CapabilityCard title="CPU" value={profile.cpu.brand ?? "Processor"} detail={`${profile.cpu.logicalCores ?? "Unknown"} logical cores · ${profile.cpu.flags.length ? profile.cpu.flags.slice(0, 8).join(", ") : "instruction flags unknown"}`} /><CapabilityCard title="System memory" value={bytes(profile.memory.totalBytes)} detail={`${bytes(profile.memory.availableBytes)} available · ${profile.memory.source}`} /><CapabilityCard title="Storage" value={`${bytes(profile.disk.freeBytes)} free`} detail={`${bytes(profile.disk.totalBytes)} total · app data volume`} /></div><div className="capability-panel"><h3>Graphics devices</h3>{profile.gpus.length ? profile.gpus.map((gpu, index) => <div className="capability-row" key={`${gpu.name}-${index}`}><b>{gpu.name}</b><span>{gpu.vendor} · {bytes(gpu.dedicatedVramBytes)} VRAM · {gpu.source}</span></div>) : <p>GPU and VRAM are unknown (no reliable vendor query tool was available).</p>}</div><div className="capability-panel"><h3>Media tools</h3><ToolRow name="FFmpeg" tool={profile.mediaTools.ffmpeg} /><ToolRow name="FFprobe" tool={profile.mediaTools.ffprobe} /><p>{profile.mediaTools.encoderListingSemantics}</p></div><div className="capability-panel"><h3>Suggested starting profile</h3><div className="capability-row"><b>{profile.recommendations.modelSizeClass} model · {profile.recommendations.proxyPreset} proxy</b><span>{profile.recommendations.confidence} recommendation</span></div><p>Recommendations are conservative estimates. Encoders are advertised by FFmpeg; actual device/driver support is not validated.</p></div></>}</div>;
}

function CapabilityCard({ title, value, detail }: { title: string; value: string; detail: string }) { return <article className="capability-card"><span>{title}</span><b>{value}</b><small>{detail}</small></article>; }
function ToolRow({ name, tool }: { name: string; tool: Record<string, unknown> }) { const encoders = Array.isArray(tool.encoders) ? tool.encoders as string[] : []; const decoders = Array.isArray(tool.decoders) ? tool.decoders as string[] : []; const hwaccels = Array.isArray(tool.hwaccels) ? tool.hwaccels as string[] : []; return <div className="capability-row tool-capability"><b>{name}</b><span>{tool.available ? String(tool.version ?? "Available") : "Unavailable"}{name === "FFmpeg" && tool.available ? <small>Encoders: {encoders.join(", ") || "none reported"}<br />Decoders: {decoders.join(", ") || "none reported"}<br />Hardware acceleration: {hwaccels.join(", ") || "none reported"}</small> : null}</span></div>; }
