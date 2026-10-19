import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import TranscriptReview from '../src/ui/TranscriptReview';
import type { DesktopBridge } from '../src/bridge';
import type { LastProjectResponse } from '../src/contracts';
const project={projectId:'p',path:'C:/project',state:{projectId:'p',revision:1,settings:{},timelineItems:[],transcripts:[],analyses:[],assets:[{id:'a',original_uri:'lesson.mov'}]}} as LastProjectResponse;
const initial={id:'t',assetId:'a',language:'en',modelId:'tiny',modelVersion:'pinned',generatorId:'whisper.cpp',generatorVersion:'test',segments:[{id:'s',text:'Hello world',originalText:'Hello world',editedText:null,sourceInTicks:2500000,sourceOutTicks:3500000,confidence:null,speakerOrdinal:null}],words:[{id:'w1',segmentId:'s',ordinal:0,text:'Hello',originalText:'Hello',sourceInTicks:2500000,sourceOutTicks:2900000,confidence:.9},{id:'w2',segmentId:'s',ordinal:1,text:'world',originalText:'world',sourceInTicks:3000000,sourceOutTicks:3500000,confidence:null}],markers:[]};
function setup(words=structuredClone(initial.words)){
 let transcript={...structuredClone(initial),words};
 const request=vi.fn(async(method,params)=>{
  if(method==='models_status')return {recommended:'tiny',backendAvailable:true,models:[{id:'tiny',ready:true,bytes:77e6,requiredRamBytes:2*1024**3}]};
  if(method==='transcript_list')return {transcripts:[transcript],playbackUrl:'http://127.0.0.1:1/ticket',sourceMinusProxySeconds:2};
  if(method==='job_list')return {jobs:[]};
  if(method==='transcript_edit'){transcript={...transcript,segments:[{...transcript.segments[0],text:params.text??'Hello world',editedText:params.text}]};return transcript;}
  if(method==='transcript_marker')return transcript;
  return {state:'queued'};
 });
 const bridge={request,shellHealth:vi.fn()} as DesktopBridge;
 render(<TranscriptReview bridge={bridge} project={project}/>);
 return request;
}
describe('transcript review',()=>{
 it('seeks using source offset and highlights playback word',async()=>{
  setup();fireEvent.click(await screen.findByRole('button',{name:'Hello'}));
  const video=screen.getByLabelText('Source preview') as HTMLVideoElement;
  expect(video.currentTime).toBe(.5);
  video.currentTime=1.2;fireEvent.timeUpdate(video);
  expect(screen.getByRole('button',{name:'world'}).parentElement).toHaveClass('active-word');
 });
 it('searches corrected text and saves without replacing original inference',async()=>{
  const request=setup();const editor=await screen.findByLabelText('Edit segment at 2500000');
  fireEvent.change(editor,{target:{value:'A correction'}});fireEvent.click(screen.getByRole('button',{name:'Save correction'}));
  await waitFor(()=>expect(request).toHaveBeenCalledWith('transcript_edit',expect.objectContaining({text:'A correction'})));
  expect(screen.getByText('Hello world')).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('Search transcript'),{target:{value:'missing phrase'}});
  expect(screen.queryByLabelText('Edit segment at 2500000')).not.toBeInTheDocument();
 });
 it('marks a selected source range and submits transcription',async()=>{
  const request=setup();fireEvent.click(await screen.findByLabelText('Select Hello, word 1, at 2.50 seconds'));fireEvent.click(screen.getByLabelText('Select world, word 2, at 3.00 seconds'));
  fireEvent.click(screen.getByRole('button',{name:'Mark required'}));
  await waitFor(()=>expect(request).toHaveBeenCalledWith('transcript_marker',expect.objectContaining({sourceInTicks:2500000,sourceOutTicks:3500000,marker:'required'})));
  fireEvent.click(screen.getByRole('button',{name:'Transcribe locally'}));
  await waitFor(()=>expect(request).toHaveBeenCalledWith('transcription_generate',{path:'C:/project',assetId:'a',model:'tiny'}));
 });
 it('gives identical transcript words distinct accessible selection names',async()=>{
  setup([{...initial.words[0],text:'go',originalText:'go'},{...initial.words[1],text:'go',originalText:'go'}]);
  expect(await screen.findByLabelText('Select go, word 1, at 2.50 seconds')).toBeInTheDocument();
  expect(screen.getByLabelText('Select go, word 2, at 3.00 seconds')).toBeInTheDocument();
 });
 it('keeps replaced-source transcripts visible as stale history without enabling edits',async()=>{
  const request=vi.fn(async(method)=>method==='models_status'?{recommended:'tiny',backendAvailable:true,models:[{id:'tiny',ready:true,bytes:77e6,requiredRamBytes:2*1024**3}]}:method==='transcript_list'?{transcripts:[{...initial,status:'stale'}]}:method==='job_list'?{jobs:[]}:{});
  render(<TranscriptReview bridge={{request,shellHealth:vi.fn()} as unknown as DesktopBridge} project={project}/>);
  expect(await screen.findByRole('alert')).toHaveTextContent('created from replaced source media');
  expect(screen.queryByLabelText('Edit segment at 2500000')).not.toBeInTheDocument();
 });
 it('opens the transcript panel on the asset selected in analysis',async()=>{
  const multiAssetProject={...project,state:{...project.state,assets:[...project.state.assets,{id:'b',original_uri:'second-lesson.mov'}]}} as LastProjectResponse;
  const request=vi.fn(async(method:string)=>method==='models_status'?{recommended:'tiny',backendAvailable:true,models:[{id:'tiny',ready:true,bytes:77e6,requiredRamBytes:2*1024**3}]}:method==='transcript_list'?{transcripts:[],playbackUrl:undefined}:method==='job_list'?{jobs:[]}:{});
  render(<TranscriptReview bridge={{request,shellHealth:vi.fn()} as unknown as DesktopBridge} project={multiAssetProject} initialAssetId="b"/>);
  await waitFor(()=>expect(request).toHaveBeenCalledWith('transcript_list',{path:'C:/project',assetId:'b'}));
  expect(screen.getByRole('combobox',{name:'Source footage'})).toHaveValue('b');
 });
});
