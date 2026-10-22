import {fireEvent,render,screen,waitFor} from '@testing-library/react';
import {describe,it,expect,vi} from 'vitest';
import ReframingPanel from '../src/ui/ReframingPanel';
import type {DesktopBridge} from '../src/bridge';
import type {OutputProfile,ReframeAnalysis,TimelineClip} from '../src/contracts';
const profile:OutputProfile={schemaVersion:1,id:'v',name:'Vertical',platform:'editable',aspect:'vertical',width:1080,height:1920,frameRate:30,targetDurationSeconds:45,maxDurationSeconds:180,safeArea:{top:.08,right:.15,bottom:.2,left:.05},fit:'crop'};
const clip={id:'c',assetId:'a',itemType:'clip',sourceInTicks:0,sourceOutTicks:1000000,properties:{}} as TimelineClip;
const result:ReframeAnalysis={assetId:'a',clipId:'c',sourceInTicks:0,sourceOutTicks:1000000,profile,sourceSha256:'abc',sourceWidth:1920,sourceHeight:1080,detector:'local-detector',detectorVersion:'1',version:'spatial-reframe-3',selectedSubjectId:'s1',subjects:[{id:'s1',kind:'face',label:'Subject 1',firstSourceTime:0}],keyframes:[{sourceTicks:0,x:.3,y:0,width:.31640625,height:1,confidence:.8,explanation:'Subject inside safe frame',subjectId:'s1',layoutMode:'crop'}]};
function bridge(results:ReframeAnalysis[]){return {request:vi.fn(async(method:string)=>method==='output_profiles'?{profile,presets:[profile],revision:1}:method==='transcript_list'?{transcripts:[],playbackUrl:'proxy.mp4'}:{results})} as unknown as DesktopBridge;}
describe('reviewable framing proposals',()=>{
 it('keeps proposals separate until accepted and preserves detector evidence',async()=>{
  const onCommand=vi.fn();render(<ReframingPanel bridge={bridge([result])} path="p" projectId="p1" clip={clip} sourceTicks={0} onCommand={onCommand}/>);
  fireEvent.click(await screen.findByText('Preview detected crop proposal'));
  expect(onCommand).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText('Apply reviewed framing'));
  expect(onCommand).toHaveBeenCalledWith({type:'reframe',clipId:'c',settings:expect.objectContaining({detector:'local-detector@1',keyframes:[expect.objectContaining({confidence:.8,explanation:'Subject inside safe frame'})]})});
 });
 it('does not offer stale proposals from another safe area or source trim',async()=>{
  const stale={...result,profile:{...profile,safeArea:{...profile.safeArea,bottom:.3}}};
  render(<ReframingPanel bridge={bridge([stale,{...result,sourceOutTicks:2000000}])} path="p" projectId="p1" clip={clip} sourceTicks={0} onCommand={()=>undefined}/>);
  await waitFor(()=>expect(screen.getByText('Analyze subject framing')).not.toBeDisabled());
  expect(screen.queryByText('Preview detected crop proposal')).toBeNull();
 });
});
