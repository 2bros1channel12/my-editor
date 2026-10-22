import { useEffect, useRef, useState } from 'react';
import type { DesktopBridge } from '../bridge';
import type { LastProjectResponse, ModelsResponse, Transcript, TranscriptResponse, TranscriptSegment, JobRecord } from '../contracts';

export default function TranscriptReview({bridge, project, modelsOnly=false, initialAssetId}: {bridge: DesktopBridge; project: LastProjectResponse | null; modelsOnly?: boolean; initialAssetId?: string}) {
 const [assetId,setAssetId]=useState('');
 const [models,setModels]=useState<ModelsResponse|null>(null);
 const [model,setModel]=useState('');
 const [data,setData]=useState<TranscriptResponse>({transcripts:[]});
 const [query,setQuery]=useState('');
 const [message,setMessage]=useState('');
 const [ticks,setTicks]=useState(0);
 const [selected,setSelected]=useState<number[]>([]);
 const [jobs,setJobs]=useState<JobRecord[]>([]);
 const video=useRef<HTMLVideoElement>(null);
 const requestGeneration=useRef(0);
 const currentSource=useRef('');
 const transcript=data.transcripts.filter(item=>!item.status||item.status==='ready').at(-1);
 const staleTranscriptCount=data.transcripts.filter(item=>item.status==='stale').length;
 const path=project?.path;
 currentSource.current=`${path}|${assetId}`;
 async function refresh() {
  if(!path||currentSource.current!==`${path}|${assetId}`)return;
  const generation=++requestGeneration.current;
  try {
   const [m,t,j]=await Promise.all([
    bridge.request('models_status',{path}),
    assetId?bridge.request('transcript_list',{path,assetId}):Promise.resolve({transcripts:[]}),
    bridge.request('job_list',{projectId:project.projectId,limit:20})
   ]);
   if(generation!==requestGeneration.current||currentSource.current!==`${path}|${assetId}`)return;
   setModels(m as ModelsResponse); setModel(previous=>previous||(m as ModelsResponse).recommended);
   setData(t as TranscriptResponse); setJobs((j as {jobs:JobRecord[]}).jobs);
  } catch(error){setMessage(String(error));}
 }
 useEffect(()=>{setAssetId(String(initialAssetId??project?.state.assets[0]?.id??''));},[project?.projectId,initialAssetId]);
 useEffect(()=>{setSelected([]);setData({transcripts:[]}); void refresh();},[path,assetId,bridge]);
 useEffect(()=>{if(!path)return;const timer=window.setInterval(()=>void refresh(),5000);return()=>window.clearInterval(timer);},[path,assetId,bridge]);
 async function action(method:'model_download'|'transcription_generate') {
  if(!path)return;
  try{if(method==='model_download')await bridge.request(method,{path,model});else if(assetId)await bridge.request(method,{path,model,assetId});setMessage('Queued. Progress and cancellation are shown below.');await refresh();}
  catch(error){setMessage(String(error));}
 }
 function seek(sourceTicks:number){if(video.current)video.current.currentTime=Math.max(0,sourceTicks/1e6-(data.sourceMinusProxySeconds??0));setTicks(sourceTicks);}
 function replace(t:Transcript){setData(previous=>({...previous,transcripts:previous.transcripts.map(old=>old.id===t.id?t:old)}));}
 async function edit(segment:TranscriptSegment,text:string|null){
  if(!path||!transcript)return;
  try{replace(await bridge.request('transcript_edit',{path,transcriptId:transcript.id,segmentId:segment.id,text}));setMessage('Correction saved. Original inference retained.');}
  catch(error){setMessage(String(error));}
 }
 const selectedWords=transcript?.words.filter(word=>selected.includes(word.ordinal))??[];
 const range=selectedWords.length?{sourceInTicks:Math.min(...selectedWords.map(w=>w.sourceInTicks)),sourceOutTicks:Math.max(...selectedWords.map(w=>w.sourceOutTicks))}:null;
 async function mark(marker:'required'|'excluded'|'uncertain'){if(!path||!range||!transcript)return;try{replace(await bridge.request('transcript_marker',{path,transcriptId:transcript.id,...range,marker}));}catch(error){setMessage(String(error));}}
 const relevant=jobs.filter(j=>j.kind==='transcription'||j.kind==='model_download');
 if(!project)return <div className="page-content"><h1>{modelsOnly?'Local models':'Transcript review'}</h1><p>Open a project and import footage to begin.</p></div>;
 return <div className="page-content transcript-review"><h1>{modelsOnly?'Local models':'Transcript review'}</h1><p>Speech inference and playback stay on this device. Model downloads require an explicit action.</p>
 <section className="capability-panel"><label>Speech model <select aria-label="Speech model" value={model} onChange={e=>setModel(e.target.value)}>{models?.models.map(m=><option key={m.id} value={m.id}>{m.id}{m.id===models.recommended?' (recommended)':''} · {(m.bytes/1024**2).toFixed(0)} MB · {m.ready?'verified':'download needed'}</option>)}</select></label>
 <p>{models?.models.find(m=>m.id===model)&&`Allow ${(models.models.find(m=>m.id===model)!.requiredRamBytes/1024**3).toFixed(0)} GB available RAM plus ${Math.ceil((models.models.find(m=>m.id===model)!.bytes+256*1024**2)/1024**2)} MB free disk space for download.`}</p>
 <button className="button secondary" disabled={!model||models?.models.find(m=>m.id===model)?.ready} onClick={()=>void action('model_download')}>Download and verify model</button>
 {!models?.backendAvailable&&<p role="status">{models?.detail??'Checking local backend…'}</p>}
 <p>If memory is low, close other applications or choose tiny. GPU acceleration is not assumed from device detection.</p></section>
 {models&&<section aria-label="Guided model selection"><h3>Choose speech quality for this device</h3><p>Recommended: {models.recommended}, based on available RAM. Tiny is the lowest-memory starting point; base and small need more memory and may improve recognition. Downloading weights does not install the Whisper executable. Install/configure the local backend before transcription. Generation and semantic search models are managed by your separately configured local provider.</p>{models.models.filter(m=>m.id===model).map(m=><p key={m.id}>Source: {m.source??'whisper.cpp catalog'} · {m.sourceUrl??'See Privacy and storage'}<br/>License: {m.license??'unknown'} · version {m.version}<br/>Size: {m.bytes} bytes · Integrity: {m.integrity}<br/>Expected SHA-256: {m.sha256??'see pinned local catalog'}</p>)}</section>}
 <p role="status">{message}</p>
 {staleTranscriptCount>0&&<p role="alert">{staleTranscriptCount} transcript{staleTranscriptCount===1?' was':'s were'} created from replaced source media and is retained for reference. Rerun transcription before using it for edits.</p>}
 {relevant.map(job=><div className="job-row" key={job.id}><b>{job.kind.replaceAll('_',' ')} · {job.state}</b><span> {job.stage} · {Math.round(job.progress*100)}%</span><progress aria-label={`${job.kind} progress`} value={job.progress} max={1}/>{['queued','running','retrying'].includes(job.state)&&<button aria-label={`Cancel ${job.kind.replaceAll('_',' ')} job ${job.id.slice(0,8)}`} onClick={async()=>{await bridge.request('job_cancel',{jobId:job.id});await refresh();}}>Cancel</button>}{job.error&&<p role="alert">{String(job.error.message??'Job failed')}</p>}</div>)}
 {!modelsOnly&&<><label>Source footage <select aria-label="Source footage" value={assetId} onChange={e=>setAssetId(e.target.value)}>{project.state.assets.map(a=><option key={String(a.id)} value={String(a.id)}>{String(a.original_uri)}</option>)}</select></label>
 <button className="button primary" disabled={!assetId||!models?.backendAvailable||!models.models.find(m=>m.id===model)?.ready} onClick={()=>void action('transcription_generate')}>Transcribe locally</button><p>Generate preview assets in Projects before starting transcription.</p>
 {data.playbackUrl?<video aria-label="Source preview" ref={video} src={data.playbackUrl} controls onTimeUpdate={e=>setTicks(Math.round((e.currentTarget.currentTime+(data.sourceMinusProxySeconds??0))*1e6))}/>:<p>Preview playback becomes available after preview generation.</p>}
 {transcript&&<><p>{transcript.language} · {transcript.modelId} · model {transcript.modelVersion.slice(0,12)} · {transcript.generatorId} {transcript.generatorVersion.slice(0,24)}</p><p>Speaker unassigned. Word timing remains tied to the original inference after text corrections.</p>
 {transcript.words.some(w=>w.timingUncertain)&&<p role="status">Some word timings were missing from inference and have uncertain bounded estimates. Review these against playback before marking ranges.</p>}
 <label>Search transcript <input aria-label="Search transcript" value={query} onChange={e=>setQuery(e.target.value)}/></label>
 <div className="range-tools"><span>{range?`Selected ${(range.sourceInTicks/1e6).toFixed(2)}–${(range.sourceOutTicks/1e6).toFixed(2)}s`:'Select word checkboxes to mark a range.'}</span>{(['required','excluded','uncertain'] as const).map(marker=><button key={marker} disabled={!range} onClick={()=>void mark(marker)}>Mark {marker}</button>)}<button onClick={()=>setSelected([])}>Clear selection</button></div>
 {transcript.markers.map(marker=><div className="range-marker" key={marker.id}><button onClick={()=>seek(marker.sourceInTicks)}>{marker.marker} · {(marker.sourceInTicks/1e6).toFixed(2)}–{(marker.sourceOutTicks/1e6).toFixed(2)}s</button><button aria-label={`Remove ${marker.marker} marker`} onClick={async()=>{if(!path)return;try{replace(await bridge.request('transcript_marker',{path,transcriptId:transcript.id,markerId:marker.id,delete:true}));}catch(error){setMessage(String(error));}}}>Remove</button></div>)}
 {transcript.segments.filter(s=>s.text.toLocaleLowerCase().includes(query.toLocaleLowerCase())).map(segment=><article className={`transcript-segment ${ticks>=segment.sourceInTicks&&ticks<segment.sourceOutTicks?'active':''}`} key={segment.id}><button aria-label={`Seek to ${segment.text}`} onClick={()=>seek(segment.sourceInTicks)}>{(segment.sourceInTicks/1e6).toFixed(2)}s</button><SegmentText segment={segment} save={text=>void edit(segment,text)}/><div className="transcript-words">{transcript.words.filter(w=>w.segmentId===segment.id).map(word=><span key={word.id} className={ticks>=word.sourceInTicks&&ticks<word.sourceOutTicks?'active-word':''}><input type="checkbox" aria-label={`Select ${word.originalText}, word ${word.ordinal+1}, at ${(word.sourceInTicks/1e6).toFixed(2)} seconds`} checked={selected.includes(word.ordinal)} onChange={e=>setSelected(old=>e.target.checked?[...old,word.ordinal]:old.filter(i=>i!==word.ordinal))}/><button title={word.timingUncertain?'Uncertain word timing: bounded estimate; review against playback':word.confidence===null?'Confidence unavailable':`Confidence ${Math.round(word.confidence*100)}%`} onClick={()=>seek(word.sourceInTicks)}>{word.originalText}</button></span>)}</div></article>)}
 {!transcript.segments.length&&<p>No speech was detected. Try clearer audio or another model.</p>}</>}
 </>}
 </div>;
}
function SegmentText({segment,save}:{segment:TranscriptSegment;save:(text:string|null)=>void}){
 const [text,setText]=useState(segment.text);useEffect(()=>setText(segment.text),[segment.text]);
 return <><textarea aria-label={`Edit segment at ${segment.sourceInTicks}`} value={text} onChange={e=>setText(e.target.value)}/><button disabled={text===segment.text} onClick={()=>save(text)}>Save correction</button><button disabled={segment.editedText===null} onClick={()=>save(null)}>Restore original</button><details><summary>Original inference</summary><p>{segment.originalText}</p></details></>;
}
