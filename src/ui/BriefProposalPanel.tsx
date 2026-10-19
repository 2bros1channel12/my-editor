import { useEffect, useMemo, useRef, useState } from "react";
import { Check, FileText, RefreshCw, Sparkles, X } from "lucide-react";
import type { DesktopBridge } from "../bridge";
import type { CreativeBrief, EditProposal, JobRecord, LastProjectResponse, NarrativeStructureTrait, ReferenceTraitProfile, StyleRecipe, Transcript } from "../contracts";

const neutralTraits: ReferenceTraitProfile["traits"] = { pacing: "unknown", averageShotLengthSeconds: null, jumpCutFrequencyPerMinute: null, captionDensityPerMinute: null, punchInFrequencyPerMinute: null, musicEnergy: "unknown", humorLevel: "unknown", narrativeStructure: "unknown" };
const blank = { schemaVersion: 2 as const, platform: "unspecified", desiredOpening: "", desiredEnding: "", recipeId: "custom", recipeVersion: "1", audience: "", objective: "", tone: "", targetDurationMinutes: null as number | null, aspectRatio: "source", mustKeep: [] as string[], mustRemove: [] as string[], preferredOrder: [] as string[] };
const labels: Array<[keyof typeof blank, string]> = [["objective", "Video purpose"], ["audience", "Intended audience"], ["tone", "Tone"]];
const phrases = (values: string[]) => values.join("\n");

export default function BriefProposalPanel({ bridge, project, onProjectRefresh }: { bridge: DesktopBridge; project: LastProjectResponse | null; onProjectRefresh: () => Promise<void> }) {
  const [text, setText] = useState("");
  const [constraints, setConstraints] = useState({ ...blank });
  const [briefs, setBriefs] = useState<CreativeBrief[]>([]);
  const [briefId, setBriefId] = useState("");
  const [assetId, setAssetId] = useState("");
  const [transcripts, setTranscripts] = useState<Transcript[]>([]);
  const [transcriptId, setTranscriptId] = useState("");
  const [playbackUrl,setPlaybackUrl]=useState("");
  const [proxyOffset,setProxyOffset]=useState(0);
  const [plans, setPlans] = useState<EditProposal[]>([]);
  const [jobs, setJobs] = useState<JobRecord[]>([]);
  const [message, setMessage] = useState("");
  const [createCaptions, setCreateCaptions] = useState(true);
  const [referenceProfile, setReferenceProfile] = useState<ReferenceTraitProfile | null>(null);
  const [referenceBusy, setReferenceBusy] = useState(false);
  const [styleReviewed, setStyleReviewed] = useState(false);
  const [inspiration, setInspiration] = useState("");
  const [recipes, setRecipes] = useState<StyleRecipe[]>([]);
  const [selectedRecipe, setSelectedRecipe] = useState("fast-educational");
  const [styleTraits, setStyleTraits] = useState<ReferenceTraitProfile["traits"]>({ ...neutralTraits });
  const [styleExplanation, setStyleExplanation] = useState("Choose your own neutral editing preferences, or start with a generic recipe.");
  useEffect(() => { void bridge.request("style_recipe_catalog").then(result => setRecipes(result.recipes ?? [])).catch(() => setRecipes([])); }, [bridge]);
  const video=useRef<HTMLVideoElement>(null);
  const path = project?.path;
  const activePath = useRef(path);
  activePath.current = path;
  const selectedPlan = useMemo(() => plans.find(plan => plan.briefId === briefId), [plans, briefId]);
  const currentTranscripts=transcripts.filter(item=>!item.status||item.status==="ready");
  const staleTranscriptCount=transcripts.filter(item=>item.status==="stale").length;

  async function refresh() {
    if (!path || !project) return;
    try {
      const [briefResult, planResult, jobResult] = await Promise.all([
        bridge.request("brief_list", { path }) as Promise<{ briefs: CreativeBrief[] }>,
        bridge.request("proposal_list", { path }) as Promise<{ plans: EditProposal[] }>,
        bridge.request("job_list", { projectId: project.projectId, limit: 100 }) as Promise<{ jobs: JobRecord[] }>,
      ]);
      if (activePath.current !== path) return;
      setBriefs(briefResult.briefs); setPlans(planResult.plans); setJobs(jobResult.jobs.filter(job => job.kind === "suggestion"));
      if (!briefId && briefResult.briefs[0]) setBriefId(briefResult.briefs[0].id);
    } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
  }
  useEffect(() => { setAssetId(String(project?.state.assets[0]?.id ?? "")); }, [project?.projectId]);
  useEffect(() => { if (!path || !assetId) { setTranscripts([]); return; }
    void bridge.request("transcript_list", { path, assetId }).then(result => { if (activePath.current !== path) return; const data=result as {transcripts:Transcript[];playbackUrl?:string;sourceMinusProxySeconds?:number}; setTranscripts(data.transcripts ?? []); setPlaybackUrl(data.playbackUrl ?? ""); setProxyOffset(data.sourceMinusProxySeconds ?? 0); }).catch(error => setMessage(String(error)));
    setTranscriptId("");
  }, [path, assetId, bridge]);
  useEffect(() => { setBriefs([]); setPlans([]); setBriefId(""); setText(""); setConstraints({ ...blank }); setReferenceProfile(null); setStyleTraits({ ...neutralTraits }); setStyleReviewed(false); setInspiration(""); setStyleExplanation("Choose your own neutral editing preferences, or start with a generic recipe."); void refresh(); }, [path, bridge]);
  useEffect(() => { if (!path) return; const timer = window.setInterval(() => void refresh(), 2500); return () => window.clearInterval(timer); }, [path, project?.projectId, bridge]);

  async function parse() {
    try {
      const result = await bridge.request("brief_parse", { briefText: text }) as { constraints: CreativeBrief["constraints"] };
      if (activePath.current !== path) return;
      setStyleTraits({ ...neutralTraits, ...(result.constraints.styleTraits ?? {}) }); setReferenceProfile(null); setInspiration(""); setStyleReviewed(false); setConstraints({ ...blank, ...result.constraints, mustKeep: result.constraints.mustKeep ?? [], mustRemove: result.constraints.mustRemove ?? [], preferredOrder: result.constraints.preferredOrder ?? [] }); setMessage("Parsed locally. Review and edit the constraints before saving.");
    } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
  }
  async function saveBrief() {
    if (!path || !styleReviewed) return;
    try {
      const result = await bridge.request("brief_save", { path, briefText: text, constraints: { ...constraints, styleTraits, styleReviewed: true }, styleReviewed: true, ...(referenceProfile?.generatorId === "localcut-neutral-reference-traits" ? { traits: styleTraits, traitOrigin: "reference-derived" as const } : {}) }) as { brief: CreativeBrief };
      if (activePath.current !== path) return;
      setBriefId(result.brief.id); await refresh(); setMessage("Brief saved locally as a new revision.");
    } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
  }
  async function analyzeReference() {
    if (!bridge.selectReferenceInput) { setMessage("Choose a reference from the desktop app to analyze it locally."); return; }
    try {
      const referencePath = await bridge.selectReferenceInput();
      if (!referencePath) return;
      setReferenceBusy(true); setMessage("Sampling the selected reference locally. The file will not be imported or retained.");
      const result = await bridge.request("reference_analyze", { referencePath }) as ReferenceTraitProfile;
      if (activePath.current !== path) return;
      setReferenceProfile(result); setStyleTraits(result.traits); setStyleReviewed(false); setConstraints(previous => ({ ...previous, recipeId: "custom", recipeVersion: "1" })); setMessage("Neutral editing traits are ready to review. They remain in memory until you save them to a brief.");
    } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
    finally { setReferenceBusy(false); }
  }
  async function saveReferenceProfile() {
    if (!path || !referenceProfile || !styleReviewed) return;
    try {
      const result = await bridge.request("brief_save", { path, briefText: text, constraints: { ...constraints, styleTraits, styleReviewed: true }, traits: styleTraits, traitOrigin: "reference-derived", styleReviewed: true }) as { brief: CreativeBrief };
      if (activePath.current !== path) return;
      setBriefId(result.brief.id); setReferenceProfile(profile => profile ? { ...profile, ephemeral: false } : profile); await refresh(); setMessage("Reference traits and current brief saved as a new local brief revision.");
    } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
  }
  function profileForBrief(brief: CreativeBrief): ReferenceTraitProfile | null {
    const stored = brief.traits ?? {};
    if (!Object.keys(stored).length) return null;
    const values = Object.fromEntries(Object.entries(stored).map(([key, trait]) => [key, trait.value])) as Partial<ReferenceTraitProfile["traits"]>;
    const provenance = Object.values(stored)[0];
    const narrativeOptions: NarrativeStructureTrait[] = ["unknown", "chronological", "problem-solution", "question-answer", "step-by-step", "demonstration-first", "hook-explanation-summary", "story-led-lesson", "before-after"];
    return {
      generatorId: provenance.generatorId,
      generatorVersion: provenance.generatorVersion,
      analyzedSeconds: 0,
      sampleFrames: 0,
      explanation: "Saved neutral trait profile. Measurements remain editable and are not identity or creator embeddings.",
      ephemeral: false,
      traits: {
        pacing: values.pacing ?? "unknown",
        averageShotLengthSeconds: values.averageShotLengthSeconds ?? null,
        jumpCutFrequencyPerMinute: values.jumpCutFrequencyPerMinute ?? null,
        captionDensityPerMinute: values.captionDensityPerMinute ?? null,
        punchInFrequencyPerMinute: values.punchInFrequencyPerMinute ?? null,
        musicEnergy: values.musicEnergy ?? "unknown",
        humorLevel: values.humorLevel ?? "unknown",
        narrativeStructure: narrativeOptions.includes(values.narrativeStructure as NarrativeStructureTrait) ? values.narrativeStructure as NarrativeStructureTrait : "unknown",
      },
    };
  }
  function updateReferenceTrait<K extends keyof ReferenceTraitProfile["traits"]>(key: K, value: ReferenceTraitProfile["traits"][K]) {
    setStyleReviewed(false); setStyleTraits(previous => ({ ...previous, [key]: value })); setReferenceProfile(profile => profile ? { ...profile, traits: { ...profile.traits, [key]: value } } : profile);
  }
  async function applyRecipe() {
    try {
      const proposal = await bridge.request("style_recipe_propose", { recipeId: selectedRecipe, ...(inspiration.trim() ? { inspiration: inspiration.trim() } : {}) });
      if (activePath.current !== path) return;
      setStyleTraits(proposal.traits); setStyleExplanation(proposal.explanation); setStyleReviewed(false); setReferenceProfile(null);
      setConstraints(previous => ({ ...previous, recipeId: proposal.recipeId, recipeVersion: proposal.recipeVersion }));
      setMessage("Review and edit the proposed neutral traits before saving.");
    } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
  }
  async function generate() {
    if (!path || !assetId || !briefId || !transcriptId) return;
    try { await bridge.request("proposal_generate", { path, assetId, briefId, transcriptId }); setMessage("Proposal queued. It will appear after validation and commit."); await refresh(); }
    catch (error) { setMessage(error instanceof Error ? error.message : String(error)); }
  }
  async function decide(plan: EditProposal, suggestionId: string, decision: "accepted" | "rejected" | "pending") {
    if (!path || !project) return;
    try {
      await bridge.request("proposal_decide", { path, planId: plan.id, suggestionId, decision, expectedRevision: project.state.revision, createCaptions });
      if (decision === "accepted" || decision === "pending") await onProjectRefresh();
      await refresh(); setMessage(decision === "accepted" ? "Suggestion applied to the timeline and saved." : decision === "pending" ? "Suggestion undone." : "Suggestion rejected; the timeline was not changed.");
    } catch (error) { setMessage(error instanceof Error ? error.message : String(error)); await onProjectRefresh(); }
  }

  function update<K extends keyof typeof blank>(key: K, value: (typeof blank)[K]) { setConstraints(previous => ({ ...previous, [key]: value })); }
  function seek(sourceTicks:number) { if (video.current) video.current.currentTime=Math.max(0,sourceTicks/1e6-proxyOffset); video.current?.scrollIntoView({block:"center",behavior:"smooth"}); }
  if (!project) return <section className="capability-panel"><h2>Creative brief</h2><p>Create or open a project before saving a brief or proposing edits.</p></section>;
  const recentJob = jobs.find(job => job.payload?.assetId === assetId && ["queued", "running", "retrying"].includes(job.state));
  return <section className="capability-panel brief-panel">
    <div className="section-heading"><div><div className="eyebrow">REVIEWABLE FIRST CUT</div><h2><FileText size={19}/> Creative brief and proposal</h2><p>Build a guided brief, review its editing preferences, then save a version for the edit planner.</p></div><button className="button secondary" onClick={() => void refresh()}><RefreshCw size={14}/> Refresh</button></div>
    <label>Optional natural-language brief<textarea aria-label="Creative brief" value={text} onChange={event => setText(event.target.value)} placeholder="Audience: new developers&#10;Objective: explain how to debug a React render&#10;Tone: clear and practical&#10;Must keep: state updates, profiler&#10;Must remove: setup chatter" rows={4}/></label>
    <div className="brief-actions"><button className="button secondary" disabled={!text.trim()} onClick={() => void parse()}>Parse locally</button><button className="button primary" disabled={!styleReviewed || !constraints.objective.trim()} onClick={() => void saveBrief()}>Save brief</button></div>
    <div className="brief-constraints">
      {labels.map(([key, label]) => <label key={key}>{label}<input value={constraints[key] as string} onChange={event => update(key, event.target.value as never)}/></label>)}
      <label>Target duration (minutes)<input type="number" min="0.25" max="180" step="0.25" value={constraints.targetDurationMinutes ?? ""} onChange={event => update("targetDurationMinutes", event.target.value ? Number(event.target.value) : null)}/></label>
      <label>Platform<select value={constraints.platform} onChange={event => update("platform", event.target.value)}>{["unspecified", "youtube", "youtube-shorts", "tiktok", "instagram-reels", "course", "other"].map(value => <option key={value}>{value}</option>)}</select></label>
      <label>Desired opening<textarea value={constraints.desiredOpening} onChange={event => update("desiredOpening", event.target.value)} placeholder="For example: show the finished result, then introduce the problem"/></label>
      <label>Desired ending<textarea value={constraints.desiredEnding} onChange={event => update("desiredEnding", event.target.value)} placeholder="For example: recap the steps and suggest a next action"/></label>
      <label>Aspect ratio<select value={constraints.aspectRatio} onChange={event => update("aspectRatio", event.target.value)}>{["source", "16:9", "9:16", "1:1", "4:3"].map(value => <option key={value}>{value}</option>)}</select></label>
      <label>Required topics or moments (one per line)<textarea value={phrases(constraints.mustKeep)} onChange={event => update("mustKeep", event.target.value.split(/\n/).map(value => value.trim()).filter(Boolean))}/></label>
      <label>Material to avoid (one per line)<textarea value={phrases(constraints.mustRemove)} onChange={event => update("mustRemove", event.target.value.split(/\n/).map(value => value.trim()).filter(Boolean))}/></label>
      <label>Preferred passage order<textarea aria-label="Preferred passage order" value={phrases(constraints.preferredOrder)} placeholder="For example: opening, main demonstration, recap" onChange={event => update("preferredOrder", event.target.value.split(/\n/).map(value => value.trim()).filter(Boolean))}/><small>Optional. Enter one phrase per line. Each phrase must uniquely match a kept transcript passage.</small></label>
    </div>
    <section className="reference-profile">
      <h3>Editing style</h3>
      <label>Generic recipe<select aria-label="Generic recipe" value={selectedRecipe} onChange={event => { setSelectedRecipe(event.target.value); setStyleReviewed(false); }}>{(recipes.length ? recipes : [{ id: "fast-educational", name: "Fast educational" }, { id: "calm-documentary", name: "Calm documentary" }, { id: "conversational-commentary", name: "Conversational commentary" }, { id: "concise-product-review", name: "Concise product review" }]).map(recipe => <option key={recipe.id} value={recipe.id}>{recipe.name}</option>)}</select></label>
      <label>Optional creator inspiration<input value={inspiration} onChange={event => { setInspiration(event.target.value); setStyleReviewed(false); }} placeholder="A name is only an optional prompt for neutral traits"/></label>
      <p>Creator names are temporary. Review generic pacing, cuts, captions and story preferences; exact imitation, cloned voice or likeness, copied branding, signature assets or phrases, and music acquisition are not offered.</p>
      <button className="button secondary" onClick={() => void applyRecipe()}>Propose neutral traits</button>
      <p>{styleExplanation}</p>
      <div className="reference-trait-grid">
        {(["pacing", "musicEnergy", "humorLevel", "narrativeStructure"] as const).map(key => <label key={key}>{{ pacing: "Pacing", musicEnergy: "Music energy", humorLevel: "Explanation and humor balance", narrativeStructure: "Narrative structure" }[key]}<select value={styleTraits[key]} onChange={event => updateReferenceTrait(key, event.target.value as never)}>{(key === "pacing" ? ["unknown", "slow", "moderate", "fast"] : key === "narrativeStructure" ? ["unknown", "chronological", "problem-solution", "question-answer", "step-by-step", "demonstration-first", "hook-explanation-summary", "story-led-lesson", "before-after"] : ["unknown", "low", "moderate", "high"]).map(value => <option key={value} value={value}>{value === "unknown" ? "No preference" : key === "humorLevel" ? { low: "Mostly explanation", moderate: "Balanced explanation and humor", high: "More humor" }[value] : value}</option>)}</select></label>)}
        {(["averageShotLengthSeconds", "jumpCutFrequencyPerMinute", "captionDensityPerMinute", "punchInFrequencyPerMinute"] as const).map(key => <label key={key}>{{ averageShotLengthSeconds: "Average shot length (seconds)", jumpCutFrequencyPerMinute: "Cut frequency (per minute)", captionDensityPerMinute: "Caption density (cues per minute)", punchInFrequencyPerMinute: "Punch-in or zoom frequency (per minute)" }[key]}<input type="number" min="0" step="0.1" placeholder="No preference" value={styleTraits[key] ?? ""} onChange={event => updateReferenceTrait(key, event.target.value === "" ? null : Number(event.target.value))}/></label>)}
      </div>
      <label><input type="checkbox" checked={styleReviewed} onChange={event => setStyleReviewed(event.target.checked)}/> I reviewed and edited these neutral editing traits</label>
      <small>Review is required again after changing traits, inspiration, recipe or project. Music energy is guidance only. Story and style preferences are saved for the planner; current proposals use required/avoided topics, duration, passage order, pacing and punch-ins.</small>
    </section>
    <section className="reference-profile">
      <div className="section-heading"><div><h3>Optional local reference traits</h3><p>Choose a lawful local clip. Only neutral editing measurements are extracted. Reference media is not imported, retained, or used to identify people.</p></div><button className="button secondary" disabled={referenceBusy} onClick={() => void analyzeReference()}>{referenceBusy ? "Analyzing…" : "Analyze reference"}</button></div>
      {referenceProfile && <>
        <p>{referenceProfile.explanation} Sampled {referenceProfile.sampleFrames} frames over {Math.round(referenceProfile.analyzedSeconds)} seconds. Analyzer {referenceProfile.generatorId} v{referenceProfile.generatorVersion}.</p>
        <div className="brief-actions"><button className="button primary" disabled={!styleReviewed || !constraints.objective.trim()} onClick={() => void saveReferenceProfile()}>Save profile to a new brief revision</button><button className="button secondary" onClick={() => { setReferenceProfile(null); setStyleTraits({ ...neutralTraits }); setStyleReviewed(false); setMessage("Unsaved reference traits cleared."); }}>Clear unsaved profile</button><small>Saving stores only the editable trait values and analyzer provenance.</small></div>
      </>}
    </section>
    <div className="proposal-generate">
      <label>Saved brief<select aria-label="Saved brief" value={briefId} onChange={event => { const id = event.target.value; setBriefId(id); const item = briefs.find(brief => brief.id === id); if (item) { setText(item.briefText); setConstraints({ ...blank, ...item.constraints, mustKeep: item.constraints.mustKeep ?? [], mustRemove: item.constraints.mustRemove ?? [], preferredOrder: item.constraints.preferredOrder ?? [] }); const profile = profileForBrief(item); setReferenceProfile(profile); setStyleTraits(item.constraints.styleTraits ?? profile?.traits ?? { ...neutralTraits }); setStyleReviewed(false); setInspiration(""); } }}>{briefs.map(brief => <option key={brief.id} value={brief.id}>Revision {brief.revision} · {brief.constraints.objective || brief.briefText.slice(0, 70)}</option>)}</select></label>
      <label>Source<select value={assetId} onChange={event => setAssetId(event.target.value)}>{project.state.assets.map(asset => <option key={String(asset.id)} value={String(asset.id)}>{String(asset.last_known_path ?? asset.original_uri)}</option>)}</select></label>
      <label>Transcript<select value={transcriptId} onChange={event => setTranscriptId(event.target.value)}><option value="">Choose transcript</option>{currentTranscripts.map(transcript => <option key={transcript.id} value={transcript.id}>{transcript.language} · {transcript.words.length} words · {transcript.modelId}</option>)}</select></label>
      {staleTranscriptCount>0&&<p role="alert">{staleTranscriptCount} transcript{staleTranscriptCount===1?' was':'s were'} created from replaced source media and cannot be used for a new proposal. Rerun transcription.</p>}
      <button className="button primary" disabled={!briefId || !assetId || !transcriptId || Boolean(recentJob)} onClick={() => void generate()}><Sparkles size={15}/> Propose first cut</button>
    </div>
    <label className="caption-proposal-choice"><input type="checkbox" checked={createCaptions} onChange={event => setCreateCaptions(event.target.checked)}/> Create editable transcript captions when I accept kept passages</label>
    {playbackUrl && <video aria-label="Proposal source preview" ref={video} src={playbackUrl} controls />}
    {recentJob && <article className="job-row"><div className="job-heading"><b>{recentJob.stage || "Proposal job"}</b><span className={`job-state state-${recentJob.state}`}>{recentJob.state}</span></div><p>{recentJob.message}</p><progress value={recentJob.progress} max={1}/><button aria-label={`Cancel ${recentJob.kind.replaceAll("_", " ")} job ${recentJob.id.slice(0,8)}`} onClick={async () => { await bridge.request("job_cancel", { jobId: recentJob.id }); await refresh(); }}>Cancel</button></article>}
    {selectedPlan && <div className="proposal-review"><div className="section-heading"><div><h3>Proposal · {selectedPlan.status}</h3><p>Created by {selectedPlan.plan ? "local transcript and brief rules" : "local proposer"}. Accepting applies only the selected operation.</p></div></div>
      {selectedPlan.plan.warnings?.map((warning, index) => <p className="proposal-warning" key={index}>{warning.message}</p>)}
      {selectedPlan.plan.styleGuidance && <div className="proposal-style-guidance" role="note"><b>Saved neutral pacing preference: {selectedPlan.plan.styleGuidance.value}</b><p>{selectedPlan.plan.styleGuidance.explanation}</p><small>Trait source: {selectedPlan.plan.styleGuidance.origin} · {selectedPlan.plan.styleGuidance.generatorId} v{selectedPlan.plan.styleGuidance.generatorVersion}</small></div>}
      {selectedPlan.plan.punchInGuidance && <div className="proposal-style-guidance" role="note"><b>Saved neutral punch-in frequency: {selectedPlan.plan.punchInGuidance.value}/min</b><p>{selectedPlan.plan.punchInGuidance.explanation}</p><small>Trait source: {selectedPlan.plan.punchInGuidance.origin} · {selectedPlan.plan.punchInGuidance.generatorId} v{selectedPlan.plan.punchInGuidance.generatorVersion}</small></div>}
      {selectedPlan.plan.operations.map(operation => {
        const include=operation.type==="include_source_range"; const punch=operation.type==="punch_in"; const reorder=operation.type==="reorder_passages";
        const dependencies=reorder ? (operation.requiresSuggestionIds as string[] | undefined) ?? [] : punch ? [String(operation.relatedIncludeSuggestionId ?? "")] : [];
        const relatedAccepted=dependencies.every(id=>selectedPlan.plan.operations.some(candidate=>candidate.id===id && candidate.status==="accepted"));
        return <article className="proposal-operation" key={operation.id}>
        <div><b>{include ? "Keep passage" : punch ? "Optional 110% punch-in" : reorder ? "Reorder kept passages" : "Exclude passage"}</b><p>{operation.text || operation.rationale}</p><small><button className="text-button" onClick={()=>seek(operation.sourceInTicks)}>{(operation.sourceInTicks / 1e6).toFixed(2)}–{(operation.sourceOutTicks / 1e6).toFixed(2)}s</button> · {operation.protected ? "protected topic" : punch ? "visual emphasis" : reorder ? "brief requested order" : "review"}{operation.timingUncertain ? " · uncertain timing" : ""}{operation.confidence == null ? " · confidence unavailable" : ` · ${Math.round(operation.confidence * 100)}% transcript confidence`}</small><p>{operation.rationale}</p>{reorder && Array.isArray(operation.orderedPassages) && <ol className="proposal-order-list">{(operation.orderedPassages as Array<{suggestionId:string;sourceInTicks:number;sourceOutTicks:number;text:string}>).map(passage=><li key={passage.suggestionId}><button className="text-button" onClick={()=>seek(passage.sourceInTicks)}>{(passage.sourceInTicks/1e6).toFixed(2)}–{(passage.sourceOutTicks/1e6).toFixed(2)}s</button> · {passage.text || "Kept passage"}</li>)}</ol>}{reorder&&!relatedAccepted&&<small>Accept all listed kept passages first. Reordering remains a separate review decision.</small>}{punch&&!relatedAccepted&&<small>Accept its kept passage first. The punch-in remains a separate decision.</small>}</div>
        <div className="brief-actions">{operation.status === "pending" ? <><button aria-label="Accept suggestion" className="button secondary" disabled={!relatedAccepted} onClick={() => void decide(selectedPlan, operation.id, "accepted")}><Check size={15}/> Accept</button><button aria-label="Reject suggestion" className="button secondary" onClick={() => void decide(selectedPlan, operation.id, "rejected")}><X size={15}/> Reject</button></> : operation.status === "accepted" ? <><span>Applied</span><button className="button secondary" onClick={() => void decide(selectedPlan, operation.id, "pending")}>Undo</button></> : <span>Rejected</span>}</div>
      </article>;
      })}
    </div>}
    <p role="status">{message}</p>
  </section>;
}

