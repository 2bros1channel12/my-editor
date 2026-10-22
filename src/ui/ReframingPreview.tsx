import { useEffect, useMemo, useRef, useState } from 'react';
import { aspectCrop, centerCrop, interpolateCrop, moveCrop, resizeCrop, upsertCropKeyframe, type CropKeyframe, type SafeArea } from '../reframingPreview';

export interface ReframeSettings {
  enabled: boolean;
  locked: boolean;
  mode: 'crop' | 'fit';
  subjectId: string | null;
  keyframes: CropKeyframe[];
  layout?: 'single'|'stacked';
  regions?: Array<{x:number;y:number;width:number;height:number}>;
  detector?:string;
  keepBoth?:boolean;
  subjects?:Array<{id:string;kind:string;label:string;firstSourceTime:number}>;
}
export interface ReframingPreviewProps {
  playbackUrl: string;
  sourceTicks: number;
  sourceMinusProxySeconds?: number;
  sourceWidth: number;
  sourceHeight: number;
  outputAspect: number;
  reframe: ReframeSettings;
  safeArea?: SafeArea;
  sectionStartTicks?: number;
  sectionEndTicks?: number;
  subjects?: { id: string; label: string }[];
  onChange: (settings: ReframeSettings) => void;
}

/** A paused source inspection surface. The main timeline owns the playback clock. */
export default function ReframingPreview({ playbackUrl, sourceTicks, sourceMinusProxySeconds = 0, sourceWidth, sourceHeight, outputAspect, reframe, safeArea, sectionStartTicks = 0, sectionEndTicks = Number.MAX_SAFE_INTEGER, subjects = [], onChange }: ReframingPreviewProps) {
  const original = useRef<HTMLVideoElement>(null), output = useRef<HTMLVideoElement>(null);
  const sourceAspect = sourceWidth > 0 && sourceHeight > 0 ? sourceWidth / sourceHeight : 16 / 9;
  const aspect = outputAspect > 0 && Number.isFinite(outputAspect) ? outputAspect : sourceAspect;
  const frameTicks = Math.max(sectionStartTicks, Math.min(sectionEndTicks, Math.round(sourceTicks)));
  const fallback = useMemo(() => centerCrop(sourceAspect, aspect, frameTicks), [sourceAspect, aspect, frameTicks]);
  const evaluated = useMemo(() => { try { return interpolateCrop(reframe.keyframes.map(k=>aspectCrop(k,sourceAspect,aspect)), frameTicks, fallback); } catch { return fallback; } }, [reframe.keyframes, frameTicks, fallback,sourceAspect,aspect]);
  const [draft, setDraft] = useState(evaluated);
  useEffect(() => setDraft(evaluated), [evaluated]);
  const sync = () => {
    const seconds = Math.max(0, frameTicks / 1e6 - sourceMinusProxySeconds);
    for (const video of [original.current, output.current]) if (video && Number.isFinite(seconds) && Math.abs(video.currentTime - seconds) > .03) video.currentTime = seconds;
  };
  useEffect(sync, [frameTicks, sourceMinusProxySeconds, playbackUrl]);
  const forceFit=reframe.mode==='fit'||reframe.keyframes.some(k=>k.layoutMode==='fit');
  const stacked=reframe.enabled&&reframe.layout==='stacked'&&!forceFit;
  const activeCrop = reframe.enabled && !forceFit&&!stacked;
  const [regions,setRegions]=useState(reframe.regions??[{x:0,y:0,width:1,height:1},{x:.65,y:.6,width:.35,height:.4}]);
  useEffect(()=>setRegions(reframe.regions??[{x:0,y:0,width:1,height:1},{x:.65,y:.6,width:.35,height:.4}]),[reframe.regions]);
  const safe = safeArea ?? { top: .05, right: .05, bottom: .1, left: .05 };
  const bounds = { top: `${safe.top * 100}%`, right: `${safe.right * 100}%`, bottom: `${safe.bottom * 100}%`, left: `${safe.left * 100}%` };
  const cropStyle = { position: 'absolute' as const, width: `${100 / draft.width}%`, height: `${100 / draft.height}%`, left: `${-draft.x / draft.width * 100}%`, top: `${-draft.y / draft.height * 100}%`, maxWidth: 'none', objectFit: 'contain' as const };
  return <section aria-label="Reframing preview">
    <h2>Output framing</h2><p>Inspect the current source frame and output crop. Changes apply to the selected clip. Save a keyframe to keep this framing; timeline undo restores prior framing.</p>
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(min(100%,240px),1fr))', gap: 16, alignItems: 'start' }}>
      <figure style={{ margin: 0 }}><figcaption>Original frame</figcaption><div style={{ position: 'relative', aspectRatio: sourceAspect, background: '#000' }}>
        <video ref={original} src={playbackUrl} muted playsInline preload="metadata" onLoadedMetadata={sync} aria-label="Original framing preview" style={{ width: '100%', height: '100%', objectFit: 'contain', display: 'block' }} />
        {activeCrop && <div aria-label="Output crop overlay" style={{ position: 'absolute', pointerEvents: 'none', boxSizing: 'border-box', border: '2px solid #ffcf60', left: `${draft.x * 100}%`, top: `${draft.y * 100}%`, width: `${draft.width * 100}%`, height: `${draft.height * 100}%`, boxShadow: '0 0 0 1px #000' }} />}
      </div></figure>
      <figure style={{ margin: 0 }}><figcaption>Output frame · {aspect.toFixed(3)} ratio</figcaption><div style={{ position: 'relative', aspectRatio: aspect, overflow: 'hidden', background: '#000' }}>
        {stacked?<div style={{position:'absolute',...bounds,display:'flex',alignItems:'center',justifyContent:'center'}}><div aria-label="Stacked output preview" style={{aspectRatio:aspect,width:aspect>aspect*(1-safe.left-safe.right)/(1-safe.top-safe.bottom)?'100%':undefined,height:aspect<=aspect*(1-safe.left-safe.right)/(1-safe.top-safe.bottom)?'100%':undefined,display:'grid',gridTemplateRows:'1fr 1fr',overflow:'hidden'}}>{regions.map((region,i)=><RegionPreview key={i} label={i===0?'Top gameplay region':'Bottom face camera region'} playbackUrl={playbackUrl} seconds={Math.max(0,frameTicks/1e6-sourceMinusProxySeconds)} region={region} sourceAspect={sourceAspect} paneAspect={aspect*2}/>)}</div></div>:<video ref={output} src={playbackUrl} muted playsInline preload="metadata" onLoadedMetadata={sync} aria-label="Reframed output preview" style={activeCrop ? cropStyle : {position:'absolute',left:bounds.left,top:bounds.top,width:`${(1-safe.left-safe.right)*100}%`,height:`${(1-safe.top-safe.bottom)*100}%`,objectFit: 'contain', display: 'block' }} />}
        <div aria-label="Caption and visual safe area" style={{ position: 'absolute', ...bounds, pointerEvents: 'none', border: '1px dashed #a9e6da', boxSizing: 'border-box' }} />
      </div></figure>
    </div>
    <p>Dashed lines mark the editable caption and important-visual safe area. This inspection preview is muted; use timeline playback for sound.</p>
    <label><input type="checkbox" aria-label="Enable reframing" checked={reframe.enabled} onChange={e => onChange({ ...reframe, enabled: e.target.checked })} />Enable crop for this clip</label>
    <label><input type="checkbox" aria-label="Lock crop" checked={reframe.locked} onChange={e => onChange({ ...reframe, locked: e.target.checked })} />Lock crop against automatic changes</label>
    <label>Frame treatment<select aria-label="Frame treatment" value={reframe.mode} onChange={e => onChange({ ...reframe, mode: e.target.value as 'crop' | 'fit' })}><option value="crop">Crop to fill</option><option value="fit">Fit whole source with padding</option></select></label>
    <label>Output composition<select aria-label="Output composition" disabled={reframe.locked} value={reframe.layout??'single'} onChange={e=>onChange({...reframe,layout:e.target.value as 'single'|'stacked',regions})}><option value="single">Single source crop</option><option value="stacked">Gameplay above face camera (two regions of this source)</option></select></label>
    {stacked&&<fieldset disabled={reframe.locked}><legend>Same-source split-screen regions</legend><p>Choose gameplay in the top region and the face camera in the bottom region. Each region fits with padding and never stretches.</p>{regions.map((r,i)=><fieldset key={i}><legend>{i===0?'Top region':'Bottom region'}</legend>{(['x','y','width','height'] as const).map(k=><label key={k}>{k}<input aria-label={`${i===0?'Top':'Bottom'} region ${k}`} type="number" min={k==='width'||k==='height'?.05:0} max="1" step=".01" value={r[k]} onChange={e=>{const n=Number(e.target.value);if(!Number.isFinite(n))return;const next={...r,[k]:Math.max(k==='width'||k==='height'?.05:0,Math.min(1,n))};next.x=Math.min(next.x,1-next.width);next.y=Math.min(next.y,1-next.height);setRegions(regions.map((old,index)=>index===i?next:old));}}/></label>)}</fieldset>)}<button onClick={()=>onChange({...reframe,regions})}>Save stacked regions</button></fieldset>}
    <label>Follow subject<select aria-label="Follow subject" value={reframe.subjectId ?? ''} disabled={reframe.locked} onChange={e => onChange({ ...reframe, subjectId: e.target.value || null })}><option value="">Automatic primary subject</option><option value="all">Keep all subjects visible</option>{subjects.map(s => <option key={s.id} value={s.id}>{s.label}</option>)}</select></label>
    <fieldset disabled={!activeCrop || reframe.locked}><legend>Crop at {(frameTicks / 1e6).toFixed(3)} source seconds</legend>
      <label>Horizontal position<input aria-label="Crop horizontal position" type="range" min="0" max={Math.max(0, 1 - draft.width)} step="0.001" value={draft.x} onChange={e => setDraft(moveCrop(draft, Number(e.target.value), draft.y))} /></label>
      <label>Vertical position<input aria-label="Crop vertical position" type="range" min="0" max={Math.max(0, 1 - draft.height)} step="0.001" value={draft.y} onChange={e => setDraft(moveCrop(draft, draft.x, Number(e.target.value)))} /></label>
      <label>Crop width (%)<input aria-label="Crop width percent" type="number" min={Math.max(.05,.05*aspect/sourceAspect)*100} max={Math.min(1, aspect / sourceAspect) * 100} step="0.1" value={Number((draft.width * 100).toFixed(2))} onChange={e => { const n = Number(e.target.value); if (Number.isFinite(n)) setDraft(resizeCrop(draft, n / 100, sourceAspect, aspect)); }} /></label>
      <button onClick={() => onChange({ ...reframe, keyframes: upsertCropKeyframe(reframe.keyframes, { ...draft, sourceTicks: frameTicks }) })}>Save crop keyframe</button>
      <button onClick={() => setDraft(fallback)}>Center crop</button>
      <button disabled={!reframe.keyframes.some(k => k.sourceTicks === frameTicks)} onClick={() => onChange({ ...reframe, keyframes: reframe.keyframes.filter(k => k.sourceTicks !== frameTicks) })}>Remove keyframe at playhead</button>
    </fieldset>
    <p>{reframe.keyframes.length} saved crop keyframes. Crop positions interpolate continuously between source times.</p>
  </section>;
}

function RegionPreview({playbackUrl,seconds,region,sourceAspect,paneAspect,label}:{playbackUrl:string;seconds:number;region:{x:number;y:number;width:number;height:number};sourceAspect:number;paneAspect:number;label:string}){
 const video=useRef<HTMLVideoElement>(null);const sync=()=>{if(video.current&&Math.abs(video.current.currentTime-seconds)>.03)video.current.currentTime=seconds;};useEffect(sync,[seconds,playbackUrl]);
 const ratio=region.width*sourceAspect/region.height;
 return <div style={{display:'flex',alignItems:'center',justifyContent:'center',minHeight:0,overflow:'hidden',background:'#000'}}><div style={{position:'relative',overflow:'hidden',aspectRatio:ratio,width:ratio>paneAspect?'100%':undefined,height:ratio<=paneAspect?'100%':undefined}}><video ref={video} aria-label={label} src={playbackUrl} muted playsInline preload="metadata" onLoadedMetadata={sync} style={{position:'absolute',width:`${100/region.width}%`,height:`${100/region.height}%`,left:`${-region.x/region.width*100}%`,top:`${-region.y/region.height*100}%`,maxWidth:'none',objectFit:'contain'}}/></div></div>;
}
