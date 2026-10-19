import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, ArrowUp, Scissors, Undo2, Redo2, Plus, Trash2, Save } from "lucide-react";
import type { DesktopBridge } from "../bridge";
import type { LastProjectResponse, PersistedProjectState, WaveformPreview } from "../contracts";
import ExportPanel from "./ExportPanel";

type Item = PersistedProjectState["timelineItems"][number];
type Caption = { id: string; timelineItemId: string; text: string; language: string; style: Record<string, unknown> };
type Snapshot = { items: Item[]; captions: Caption[] };
const uid = () => crypto.randomUUID();
const duration = (item: Item) => item.sourceOutTicks - item.sourceInTicks;
const stable = (items: Item[]) => [...items].sort((a, b) => a.timelineStartTicks - b.timelineStartTicks || a.trackId.localeCompare(b.trackId));
function placeLinkedItem(item: Item, parent: Item): Item {
  const sourceDuration = parent.sourceOutTicks - parent.sourceInTicks;
  if (sourceDuration <= 0) return item;
  const scale = parent.timelineDurationTicks / sourceDuration;
  return {
    ...item,
    timelineStartTicks: parent.timelineStartTicks + Math.round((item.sourceInTicks - parent.sourceInTicks) * scale),
    timelineDurationTicks: Math.max(1, Math.round((item.sourceOutTicks - item.sourceInTicks) * scale)),
  };
}
function hasAudioStream(asset: Record<string, unknown>): boolean {
  try {
    const metadata = typeof asset.metadata_json === "string" ? JSON.parse(asset.metadata_json) as Record<string, unknown> : asset.metadata_json as Record<string, unknown> | undefined;
    return Array.isArray(metadata?.audio) && metadata.audio.length > 0;
  } catch { return false; }
}

export default function TimelineEditor({ bridge, project, onProjectRefresh }: { bridge: DesktopBridge; project: LastProjectResponse | null; onProjectRefresh?: () => Promise<void> }) {
  const assets = project?.state?.assets ?? [];
  const [assetId, setAssetId] = useState("");
  const [items, setItems] = useState<Item[]>([]);
  const [captions, setCaptions] = useState<Caption[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [sourceIn, setSourceIn] = useState(0);
  const [sourceOut, setSourceOut] = useState(0);
  const [bedAssetId, setBedAssetId] = useState("");
  const [bedSourceIn, setBedSourceIn] = useState(0);
  const [bedSourceOut, setBedSourceOut] = useState(0);
  const [bedTimelineStart, setBedTimelineStart] = useState(0);
  const [bedGainDb, setBedGainDb] = useState(-18);
  const [bedFadeInSeconds, setBedFadeInSeconds] = useState(1);
  const [bedFadeOutSeconds, setBedFadeOutSeconds] = useState(1);
  const [gainDb, setGainDb] = useState(0);
  const [fadeInSeconds, setFadeInSeconds] = useState(0);
  const [fadeOutSeconds, setFadeOutSeconds] = useState(0);
  const [videoFadeInSeconds, setVideoFadeInSeconds] = useState(0);
  const [videoFadeOutSeconds, setVideoFadeOutSeconds] = useState(0);
  const [captionText, setCaptionText] = useState("");
  const [videoUrl, setVideoUrl] = useState("");
  const [waveform, setWaveform] = useState<WaveformPreview | null>(null);
  const [sourceOffset, setSourceOffset] = useState(0);
  const [message, setMessage] = useState("");
  const [past, setPast] = useState<Snapshot[]>([]);
  const [future, setFuture] = useState<Snapshot[]>([]);
  const [savePending, setSavePending] = useState(false);
  const [saveFailed, setSaveFailed] = useState(false);
  const latestSnapshot = useRef<Snapshot>({ items: [], captions: [] });
  const initializedProjectId = useRef("");
  const saveQueue = useRef<Promise<void>>(Promise.resolve());
  const saveVersion = useRef(0);
  const video = useRef<HTMLVideoElement>(null);
  const selected = items.find(item => item.id === selectedId && item.itemType === "clip");
  useEffect(() => { if (selected) { setGainDb(Number(selected.properties?.audioGainDb ?? 0)); setFadeInSeconds(Number(selected.properties?.fadeInTicks ?? 0)/1e6); setFadeOutSeconds(Number(selected.properties?.fadeOutTicks ?? 0)/1e6); setVideoFadeInSeconds(Number(selected.properties?.videoFadeInTicks ?? 0)/1e6); setVideoFadeOutSeconds(Number(selected.properties?.videoFadeOutTicks ?? 0)/1e6); } }, [selectedId]);
  const asset = assets.find(row => String(row.id) === assetId);
  const assetDuration = Number(asset?.duration_ticks ?? 0);
  const audioAssets = assets.filter(hasAudioStream);
  const bedAsset = audioAssets.find(row => String(row.id) === bedAssetId);
  const bedAssetDuration = Number(bedAsset?.duration_ticks ?? 0);
  const videoEndTicks = Math.max(0, ...items.filter(item => item.itemType === "clip").map(item => item.timelineStartTicks + item.timelineDurationTicks));
  const timelineEnd = Math.max(1, ...items.map(item => item.timelineStartTicks + item.timelineDurationTicks));
  const orderedClips = useMemo(() => stable(items.filter(item => item.itemType === "clip")), [items]);
  const unavailableClips = orderedClips.filter(item => {
    const source = assets.find(row => String(row.id) === item.assetId);
    return source?.sourceStatus === "unavailable" || source?.sourceStatus === "changed_since_import";
  });

  useEffect(() => {
    const initial = project?.state?.timelineItems ?? [];
    const snapshot = { items: initial, captions: project?.state?.captions ?? [] };
    const sameProject = initializedProjectId.current === (project?.projectId ?? "");
    const sameTimeline = JSON.stringify(latestSnapshot.current) === JSON.stringify(snapshot);
    if (sameProject && sameTimeline) return;
    initializedProjectId.current = project?.projectId ?? "";
    latestSnapshot.current = snapshot; const version = ++saveVersion.current; setSavePending(false); setSaveFailed(false);
    setItems(snapshot.items); setCaptions(snapshot.captions);
    setAssetId(String(project?.state?.assets[0]?.id ?? "")); setSelectedId(initial.find(item => item.itemType === "clip")?.id ?? "");
    setBedAssetId(String((project?.state?.assets ?? []).find(hasAudioStream)?.id ?? "")); setBedSourceIn(0); setBedSourceOut(0); setBedTimelineStart(0);
    setPast([]); setFuture([]);
    let active = true;
    if (project?.path) void bridge.request("timeline_history", { path: project.path }).then((response) => {
      const snapshots = (response as { snapshots?: Snapshot[] }).snapshots;
      if (active && saveVersion.current === version && Array.isArray(snapshots)) setPast(snapshots);
    }).catch(() => undefined);
    return () => { active = false; };
  }, [project?.projectId, project?.path, project?.state.revision, bridge]);
  useEffect(() => { setBedSourceIn(0); setBedSourceOut(bedAssetDuration); }, [bedAssetId, bedAssetDuration]);
  useEffect(() => {
    setSourceIn(0); setSourceOut(assetDuration); setMessage("");
    if (!project?.path || !assetId) { setVideoUrl(""); setWaveform(null); return; }
    void bridge.request("transcript_list", { path: project.path, assetId }).then(value => {
      const data = value as { playbackUrl?: string; sourceMinusProxySeconds?: number; waveform?: WaveformPreview | null };
      setVideoUrl(data.playbackUrl ?? ""); setSourceOffset(data.sourceMinusProxySeconds ?? 0); setWaveform(data.waveform ?? null);
    }).catch(() => { setVideoUrl(""); setWaveform(null); });
  }, [project?.path, assetId, assetDuration, bridge]);

  function persist(next: Snapshot, remember = true) {
    if (!project) return;
    const previous = latestSnapshot.current;
    latestSnapshot.current = next;
    setItems(next.items); setCaptions(next.captions); setSavePending(true); setSaveFailed(false); setMessage("Saving timeline locally…");
    if (remember) { setPast(history => [...history.slice(-49), previous]); setFuture([]); }
    const version = ++saveVersion.current;
    const operation = saveQueue.current.then(async () => {
      try {
        const state = { ...project.state, timelineItems: next.items, captions: next.captions };
        const saved = await bridge.request("project_save", { path: project.path, state }) as { revision: number };
        if (version === saveVersion.current) { setSavePending(false); setSaveFailed(false); setMessage(`Saved locally · revision ${saved.revision}`); await onProjectRefresh?.(); }
      } catch (error) {
        if (version === saveVersion.current) { setSavePending(false); setSaveFailed(true); setMessage(error instanceof Error ? error.message : "Timeline could not be saved. Retry the save before closing the project."); }
      }
    });
    saveQueue.current = operation;
    return operation;
  }
  function appendClip(from = sourceIn, to = sourceOut, props: Record<string, unknown> = {}) {
    if (!assetId || to <= from || to > assetDuration) return;
    const start = Math.max(0, ...items.filter(item => item.trackId === "video" || item.trackId === "main_video").map(item => item.timelineStartTicks + item.timelineDurationTicks));
    const item: Item = { id: uid(), assetId, itemType: "clip", trackId: "video", timelineStartTicks: start, timelineDurationTicks: to - from, sourceInTicks: from, sourceOutTicks: to, properties: { linkedAudio: true, audioGainDb: 0, fadeInTicks: 0, fadeOutTicks: 0, videoFadeInTicks: 0, videoFadeOutTicks: 0, cropPreset: "full_frame", ...props } };
    setSelectedId(item.id); void persist({ items: [...items, item], captions });
  }
  function addAudioBed() {
    if (!bedAssetId || bedSourceIn < 0 || bedSourceOut <= bedSourceIn || bedSourceOut > bedAssetDuration) return;
    const start = Math.max(0, bedTimelineStart);
    const length = Math.min(bedSourceOut - bedSourceIn, videoEndTicks - start);
    if (length <= 0) { setMessage("Place the audio bed before the end of the video timeline."); return; }
    const fadeIn = Math.round(Math.max(0, Math.min(length / 2 / 1e6, bedFadeInSeconds)) * 1e6);
    const fadeOut = Math.round(Math.max(0, Math.min(length / 2 / 1e6, bedFadeOutSeconds)) * 1e6);
    const item: Item = { id: uid(), assetId: bedAssetId, itemType: "audio", trackId: "music", timelineStartTicks: start, timelineDurationTicks: length, sourceInTicks: bedSourceIn, sourceOutTicks: bedSourceIn + length, properties: { role: "music-bed", audioGainDb: Math.max(-60, Math.min(24, bedGainDb)), fadeInTicks: fadeIn, fadeOutTicks: fadeOut, userSupplied: true } };
    void persist({ items: [...items, item], captions });
  }
  function removeAudioBed(id: string) { void persist({ items: items.filter(item => item.id !== id), captions }); }
  function applyTrim() {
    if (!selected || sourceIn < 0 || sourceOut <= sourceIn || sourceOut > assetDuration) return;
    const removedCueIds = new Set<string>();
    const next = items.flatMap(item => {
      if (item.id === selected.id) return [{ ...item, sourceInTicks: sourceIn, sourceOutTicks: sourceOut, timelineDurationTicks: sourceOut - sourceIn }];
      if (item.properties?.linkedClipId !== selected.id) return [item];
      const cueIn = Math.max(sourceIn, item.sourceInTicks);
      const cueOut = Math.min(sourceOut, item.sourceOutTicks);
      if (cueOut <= cueIn) { if (item.itemType === "caption") removedCueIds.add(item.id); return []; }
      return [{ ...item, sourceInTicks: cueIn, sourceOutTicks: cueOut, timelineStartTicks: selected.timelineStartTicks + cueIn - sourceIn, timelineDurationTicks: cueOut - cueIn }];
    });
    const delta = (sourceOut - sourceIn) - selected.timelineDurationTicks;
    const oldEnd = selected.timelineStartTicks + selected.timelineDurationTicks;
    const adjustedClips = next.map(item => item.id !== selected.id && item.itemType === "clip" && item.trackId === selected.trackId && item.timelineStartTicks >= oldEnd ? { ...item, timelineStartTicks: Math.max(0, item.timelineStartTicks + delta) } : item);
    const adjusted = adjustedClips.map(item => {
      const linkedClipId = String(item.properties?.linkedClipId ?? "");
      const parentAfter = adjustedClips.find(candidate => candidate.id === linkedClipId);
      if (!parentAfter || parentAfter.itemType !== "clip") return item;
      return placeLinkedItem(item, parentAfter);
    });
    const nextCaptions = captions.filter(caption => !removedCueIds.has(caption.timelineItemId));
    void persist({ items: adjusted, captions: nextCaptions });
  }
  function split() {
    if (!selected) return;
    const at = sourceIn;
    if (at <= selected.sourceInTicks || at >= selected.sourceOutTicks) { setMessage("Set the source in field to a point inside the selected clip, then split."); return; }
    const ratio = selected.timelineDurationTicks / (selected.sourceOutTicks - selected.sourceInTicks);
    const leftDuration = Math.round((at - selected.sourceInTicks) * ratio);
    const left = { ...selected, sourceOutTicks: at, timelineDurationTicks: leftDuration };
    const right: Item = { ...selected, id: uid(), sourceInTicks: at, timelineStartTicks: selected.timelineStartTicks + leftDuration, timelineDurationTicks: selected.timelineDurationTicks - leftDuration };
    const newCaptions: Caption[] = [];
    const next = items.flatMap(item => {
      if (item.id === selected.id) return [left, right];
      if (item.properties?.linkedClipId === selected.id) {
        const pieces = [
          { parent: left, sourceInTicks: Math.max(item.sourceInTicks, left.sourceInTicks), sourceOutTicks: Math.min(item.sourceOutTicks, left.sourceOutTicks) },
          { parent: right, sourceInTicks: Math.max(item.sourceInTicks, right.sourceInTicks), sourceOutTicks: Math.min(item.sourceOutTicks, right.sourceOutTicks) },
        ].filter(piece => piece.sourceOutTicks > piece.sourceInTicks);
        const original = captions.find(row => row.timelineItemId === item.id);
        return pieces.map((piece, index) => {
          const itemId = index === 0 ? item.id : uid();
          const linkedItem: Item = {
            ...item,
            id: itemId,
            sourceInTicks: piece.sourceInTicks,
            sourceOutTicks: piece.sourceOutTicks,
            timelineStartTicks: piece.parent.timelineStartTicks + Math.round((piece.sourceInTicks - piece.parent.sourceInTicks) * ratio),
            timelineDurationTicks: Math.max(1, Math.round((piece.sourceOutTicks - piece.sourceInTicks) * ratio)),
            properties: { ...item.properties, linkedClipId: piece.parent.id },
          };
          if (index > 0 && original) newCaptions.push({ ...original, id: uid(), timelineItemId: linkedItem.id });
          return linkedItem;
        });
      }
      return [item];
    });
    setSelectedId(right.id); void persist({ items: next, captions: [...captions, ...newCaptions] });
  }
  function removeSelected(ripple: boolean) {
    if (!selected) return;
    const deletedEnd = selected.timelineStartTicks + selected.timelineDurationTicks;
    let next = items.filter(item => item.id !== selected.id && item.properties?.linkedClipId !== selected.id);
    let nextCaptions = captions.filter(item => next.some(timeline => timeline.id === item.timelineItemId));
    if (ripple) {
      const shift = (item: Item) => item.trackId === selected.trackId && item.timelineStartTicks >= deletedEnd ? { ...item, timelineStartTicks: Math.max(0, item.timelineStartTicks - selected.timelineDurationTicks) } : item;
      next = next.map(shift);
      next = next.map(item => { const linked = String(item.properties?.linkedClipId ?? ""); const clip = next.find(candidate => candidate.id === linked && candidate.itemType === "clip"); return clip ? placeLinkedItem(item, clip) : item; });
    }
    setSelectedId(""); void persist({ items: next, captions: nextCaptions });
  }
  function moveSelected(direction: -1 | 1) {
    if (!selected) return;
    const order = [...orderedClips]; const index = order.findIndex(item => item.id === selected.id); const target = index + direction;
    if (target < 0 || target >= order.length) return;
    [order[index], order[target]] = [order[target], order[index]];
    let cursor = 0; const starts = new Map<string, number>();
    for (const clip of order) { starts.set(clip.id, cursor); cursor += clip.timelineDurationTicks; }
    const next = items.map(item => {
      if (starts.has(item.id)) return { ...item, timelineStartTicks: starts.get(item.id)! };
      const linked = String(item.properties?.linkedClipId ?? ""); const clip = order.find(candidate => candidate.id === linked);
      return clip && starts.has(linked) ? placeLinkedItem(item, { ...clip, timelineStartTicks: starts.get(linked)! }) : item;
    });
    void persist({ items: next, captions });
  }
  function addCaption() {
    if (!selected) return;
    const text = captionText.trim(); if (!text) { setMessage("Enter caption text first."); return; }
    const item: Item = { id: uid(), assetId: selected.assetId, itemType: "caption", trackId: "captions", timelineStartTicks: selected.timelineStartTicks, timelineDurationTicks: selected.timelineDurationTicks, sourceInTicks: selected.sourceInTicks, sourceOutTicks: selected.sourceOutTicks, properties: { text, linkedClipId: selected.id } };
    void persist({ items: [...items, item], captions: [...captions, { id: uid(), timelineItemId: item.id, text, language: "en", style: {} }] }); setCaptionText("");
  }
  async function saveCaption(item: Item, text: string) {
    const nextItems = items.map(row => row.id === item.id ? { ...row, properties: { ...row.properties, text } } : row);
    const nextCaptions = captions.map(row => row.timelineItemId === item.id ? { ...row, text } : row);
    void persist({ items: nextItems, captions: nextCaptions });
  }
  async function undo() {
    const previous = past.at(-1); if (!previous) return;
    setPast(past.slice(0, -1)); setFuture([...future, { items, captions }]); await persist(previous, false);
  }
  async function redo() {
    const next = future.at(-1); if (!next) return;
    setFuture(future.slice(0, -1)); setPast([...past, { items, captions }]); await persist(next, false);
  }
  useEffect(() => {
    function onShortcut(event: KeyboardEvent) {
      if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== "z") return;
      const target = event.target;
      if (target instanceof HTMLElement && (target.isContentEditable || target.closest("input, textarea, select, [contenteditable='true']"))) return;
      if (event.shiftKey) {
        if (!future.length) return;
        event.preventDefault(); void redo();
      } else {
        if (!past.length) return;
        event.preventDefault(); void undo();
      }
    }
    window.addEventListener("keydown", onShortcut);
    return () => window.removeEventListener("keydown", onShortcut);
  }, [past, future, items, captions]);
  function seek(ticks: number) { if (video.current) video.current.currentTime = Math.max(0, ticks / 1e6 - sourceOffset); }

  if (!project) return <section className="timeline-editor page-content"><h2>Timeline</h2><p>Create or reopen a project to edit footage.</p></section>;
  return <section className="timeline-editor page-content" aria-label="Timeline editor">
    <div className="page-heading"><div><div className="eyebrow">NONDESTRUCTIVE EDIT</div><h1>Timeline</h1><p>Manual edits are saved into the project; source footage is never rewritten.</p></div><div className="timeline-history"><button className="button secondary" disabled={!past.length} onClick={() => void undo()}><Undo2 size={14}/>Undo</button><button className="button secondary" disabled={!future.length} onClick={() => void redo()}><Redo2 size={14}/>Redo</button></div></div>
    <section className="capability-panel timeline-controls"><label>Source<select aria-label="Timeline source" value={assetId} onChange={event => setAssetId(event.target.value)}>{assets.map(row => <option key={String(row.id)} value={String(row.id)}>{String(row.original_uri)}</option>)}</select></label>
      <label>Source in (seconds)<input aria-label="Clip source in" type="number" min="0" max={assetDuration / 1e6} step="0.01" value={sourceIn / 1e6} onChange={event => setSourceIn(Math.round(Number(event.target.value) * 1e6))}/></label>
      <label>Source out (seconds)<input aria-label="Clip source out" type="number" min="0" max={assetDuration / 1e6} step="0.01" value={sourceOut / 1e6} onChange={event => setSourceOut(Math.round(Number(event.target.value) * 1e6))}/></label>
      <button className="button primary" disabled={!assetId || !assetDuration} onClick={() => appendClip()}><Plus size={14}/>Add source range</button>
      <span role="status" aria-busy={savePending}>{message || (assetDuration ? `Source duration ${(assetDuration / 1e6).toFixed(2)} s · timebase 1,000,000 ticks/s` : "Import and probe media to enable timeline edits.")}</span>{saveFailed && <button className="button secondary" onClick={() => void persist(latestSnapshot.current, false)}>Retry save</button>}
    </section>
    {unavailableClips.length > 0 && <div className="media-source-warning" role="alert"><b>{unavailableClips.length} timeline clip{unavailableClips.length === 1 ? " uses" : "s use"} missing or changed source media.</b><span>Timeline edits are preserved. Visit Projects to relink each source before preview, analysis, or export.</span></div>}
    {videoUrl && <video ref={video} aria-label="Timeline source preview" src={videoUrl} controls/>}
    {waveform && waveform.peaks.length > 0 && waveform.durationTicks > 0 && <div className="waveform-preview" role="group" aria-label="Source audio waveform">
      <div><b>Source audio waveform</b><span>{(waveform.sourceStartTicks / 1e6).toFixed(2)}–{((waveform.sourceStartTicks + waveform.durationTicks) / 1e6).toFixed(2)}s · {waveform.originalBinCount.toLocaleString()} source bins summarized</span></div>
      <svg role="img" aria-label="Waveform from source audio" viewBox={`0 0 ${waveform.peaks.length} 100`} preserveAspectRatio="none">
        <line x1="0" x2={waveform.peaks.length} y1="50" y2="50" />
        {waveform.peaks.map(([minimum, maximum], index) => <rect key={index} x={index} y={50 - maximum * 48} width="1" height={Math.max(1, (maximum - minimum) * 48)} />)}
      </svg>
    </div>}
    <section className="timeline-board" aria-label="Tracks">
      <div className="timeline-ruler"><b>Video</b><span>0 s</span><span>{(timelineEnd / 1e6).toFixed(1)} s</span></div>
      {orderedClips.length ? orderedClips.map(item => <button className={`timeline-clip ${item.id === selectedId ? "selected" : ""}`} key={item.id} style={{ left: `${item.timelineStartTicks / timelineEnd * 100}%`, width: `${Math.max(1.5, item.timelineDurationTicks / timelineEnd * 100)}%` }} onClick={() => { setSelectedId(item.id); setSourceIn(item.sourceInTicks); setSourceOut(item.sourceOutTicks); seek(item.sourceInTicks); }} title={`${(item.sourceInTicks / 1e6).toFixed(2)}–${(item.sourceOutTicks / 1e6).toFixed(2)} source seconds`}><span>{String(assets.find(row => String(row.id) === item.assetId)?.original_uri ?? "Clip").split(/[\\/]/).at(-1)}</span></button>) : <p className="timeline-empty">Add a source range to begin a manual first cut.</p>}
      <div className="timeline-ruler caption-ruler"><b>Captions</b>{items.filter(item => item.itemType === "caption").map(item => <span key={item.id}>{String(item.properties?.text ?? "Caption")}</span>)}</div>
      <div className="timeline-ruler audio-bed-ruler"><b>Music / audio bed</b>{items.filter(item => item.itemType === "audio" && (item.trackId === "music" || item.trackId === "audio")).map(item => <span key={item.id} style={{ left: `${item.timelineStartTicks / timelineEnd * 100}%`, width: `${Math.max(1.5, item.timelineDurationTicks / timelineEnd * 100)}%` }}>{String(assets.find(row => String(row.id) === item.assetId)?.original_uri ?? "Audio bed").split(/[\\/]/).at(-1)} · {Number(item.properties?.audioGainDb ?? 0)} dB <button aria-label="Remove audio bed" onClick={() => removeAudioBed(item.id)}><Trash2 size={12}/></button></span>)}</div>
    </section>
    <section className="capability-panel audio-bed-editor" aria-label="Add music or audio bed">
      <div className="timeline-edit-head"><div><h3>Music / audio bed</h3><p>Choose an imported MP4 or MOV with an audio stream. The source stays linked and is mixed under the video during export.</p></div></div>
      <div className="timeline-controls">
        <label>Audio source<select aria-label="Audio bed source" value={bedAssetId} onChange={event => setBedAssetId(event.target.value)}>{audioAssets.map(row => <option key={String(row.id)} value={String(row.id)}>{String(row.last_known_path ?? row.original_uri)}</option>)}</select></label>
        <label>Source in (seconds)<input aria-label="Audio bed source in" type="number" min="0" max={bedAssetDuration / 1e6} step="0.01" value={bedSourceIn / 1e6} onChange={event => setBedSourceIn(Math.round(Number(event.target.value) * 1e6))}/></label>
        <label>Source out (seconds)<input aria-label="Audio bed source out" type="number" min="0" max={bedAssetDuration / 1e6} step="0.01" value={bedSourceOut / 1e6} onChange={event => setBedSourceOut(Math.round(Number(event.target.value) * 1e6))}/></label>
        <label>Place at (seconds)<input aria-label="Audio bed timeline start" type="number" min="0" max={videoEndTicks / 1e6} step="0.01" value={bedTimelineStart / 1e6} onChange={event => setBedTimelineStart(Math.round(Number(event.target.value) * 1e6))}/></label>
        <label>Bed level (dB)<input aria-label="Audio bed gain" type="number" min="-60" max="24" step="1" value={bedGainDb} onChange={event => setBedGainDb(Number(event.target.value))}/></label>
        <label>Fade in (seconds)<input aria-label="Audio bed fade in" type="number" min="0" step="0.1" value={bedFadeInSeconds} onChange={event => setBedFadeInSeconds(Number(event.target.value))}/></label>
        <label>Fade out (seconds)<input aria-label="Audio bed fade out" type="number" min="0" step="0.1" value={bedFadeOutSeconds} onChange={event => setBedFadeOutSeconds(Number(event.target.value))}/></label>
        <button className="button secondary" disabled={!bedAssetId || !videoEndTicks || bedSourceOut <= bedSourceIn} onClick={addAudioBed}><Plus size={14}/>Add audio bed</button>
      </div>
    </section>
    {selected && <section className="capability-panel timeline-edit-panel"><div className="timeline-edit-head"><h3>Selected clip</h3><span>{(selected.timelineStartTicks / 1e6).toFixed(2)} s on timeline</span></div>
      <div className="timeline-actions"><button className="button secondary" onClick={applyTrim}><Save size={14}/>Apply trim</button><button className="button secondary" onClick={split}><Scissors size={14}/>Split at source in</button><button className="button secondary" onClick={() => moveSelected(-1)}><ArrowUp size={14}/>Move earlier</button><button className="button secondary" onClick={() => moveSelected(1)}><ArrowDown size={14}/>Move later</button><button className="button secondary" onClick={() => removeSelected(false)}><Trash2 size={14}/>Delete</button><button className="button secondary" onClick={() => removeSelected(true)}><Trash2 size={14}/>Ripple delete</button></div>
      <div className="timeline-controls"><label>Crop preset<select aria-label="Crop preset" value={String(selected.properties?.cropPreset ?? "full_frame")} onChange={event => { const next=items.map(item=>item.id===selected.id?{...item,properties:{...item.properties,cropPreset:event.target.value}}:item); void persist({items:next,captions}); }}><option value="full_frame">Full frame</option><option value="punch_in_110">Punch in 110%</option><option value="punch_in_125">Punch in 125%</option></select></label>
        <label>Audio gain (dB)<input aria-label="Audio gain" type="number" min="-60" max="24" step="1" value={gainDb} onChange={event => setGainDb(Number(event.target.value))}/></label>
        <label>Fade in (seconds)<input aria-label="Fade in seconds" type="number" min="0" max={duration(selected)/2/1e6} step="0.1" value={fadeInSeconds} onChange={event => setFadeInSeconds(Number(event.target.value))}/></label>
        <label>Fade out (seconds)<input aria-label="Fade out seconds" type="number" min="0" max={duration(selected)/2/1e6} step="0.1" value={fadeOutSeconds} onChange={event => setFadeOutSeconds(Number(event.target.value))}/></label>
        <button className="button secondary" onClick={() => { const maxGain=Math.max(-60,Math.min(24,gainDb)); const maxFade=duration(selected)/2/1e6; const next=items.map(item=>item.id===selected.id?{...item,properties:{...item.properties,audioGainDb:maxGain,fadeInTicks:Math.round(Math.max(0,Math.min(maxFade,fadeInSeconds))*1e6),fadeOutTicks:Math.round(Math.max(0,Math.min(maxFade,fadeOutSeconds))*1e6)}}:item); void persist({items:next,captions}); }}>Apply audio settings</button>
        <span>Linked source audio · trim/fade values remain nondestructive properties.</span></div>
      <div className="timeline-controls"><label>Video fade in from black (seconds)<input aria-label="Video fade in seconds" type="number" min="0" max={duration(selected)/2/1e6} step="0.1" value={videoFadeInSeconds} onChange={event => setVideoFadeInSeconds(Number(event.target.value))}/></label>
        <label>Video fade out to black (seconds)<input aria-label="Video fade out seconds" type="number" min="0" max={duration(selected)/2/1e6} step="0.1" value={videoFadeOutSeconds} onChange={event => setVideoFadeOutSeconds(Number(event.target.value))}/></label>
        <button className="button secondary" onClick={() => { const maxFade=duration(selected)/2/1e6; const next=items.map(item=>item.id===selected.id?{...item,properties:{...item.properties,videoFadeInTicks:Math.round(Math.max(0,Math.min(maxFade,videoFadeInSeconds))*1e6),videoFadeOutTicks:Math.round(Math.max(0,Math.min(maxFade,videoFadeOutSeconds))*1e6)}}:item); void persist({items:next,captions}); }}>Apply video fades</button>
        <span>Fade through black between cuts; this does not overlap source clips.</span></div>
      <div className="caption-editor"><label>Add editable caption<input aria-label="New caption" value={captionText} onChange={event => setCaptionText(event.target.value)} placeholder="Type a caption for this clip"/></label><button className="button secondary" onClick={addCaption}>Add caption</button></div>
    </section>}
    {items.filter(item => item.itemType === "caption").map(item => <CaptionRow key={item.id} item={item} onSave={text => void saveCaption(item,text)} onSeek={seek}/>)}
    <ExportPanel bridge={bridge} project={project} hasClips={orderedClips.length > 0} hasCaptions={(project?.state.captions?.length ?? 0) > 0}/>
  </section>;
}

function CaptionRow({ item, onSave, onSeek }: { item: Item; onSave: (text: string) => void; onSeek: (ticks: number) => void }) {
  const [text,setText]=useState(String(item.properties?.text??"")); useEffect(()=>setText(String(item.properties?.text??"")),[item.properties?.text]);
  return <div className="caption-row"><button onClick={()=>onSeek(item.sourceInTicks)}>{(item.sourceInTicks/1e6).toFixed(2)}s</button><textarea aria-label="Edit caption text" rows={2} value={text} onChange={event=>setText(event.target.value)}/><button disabled={text===String(item.properties?.text??"")} onClick={()=>onSave(text)}>Save caption</button></div>;
}
