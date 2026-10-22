import {fireEvent,render,screen,waitFor} from '@testing-library/react';
import {describe,it,expect,vi} from 'vitest';
import OutputProfilesPanel from '../src/ui/OutputProfilesPanel';
import type {DesktopBridge} from '../src/bridge';
import type {OutputProfile} from '../src/contracts';
const profile:OutputProfile={schemaVersion:1,id:'vertical',name:'Vertical',platform:'editable',aspect:'vertical',width:1080,height:1920,frameRate:30,targetDurationSeconds:45,maxDurationSeconds:180,safeArea:{top:.08,right:.15,bottom:.2,left:.05},fit:'crop'};
describe('editable output profiles',()=>{
 it('saves reviewed safe areas and delivery constraints under revision control',async()=>{
  const saved=vi.fn(),request=vi.fn(async(method:string)=>method==='output_profiles'?{revision:4,presets:[profile],profile}:method==='output_variants'?{revision:4,activeId:null,variants:[]}:{briefs:[]});
  render(<OutputProfilesPanel bridge={{request} as unknown as DesktopBridge} path="p" revision={4} onSaved={saved}/>);
  await screen.findByDisplayValue('Vertical');
  fireEvent.change(screen.getByLabelText('Safe area bottom percent'),{target:{value:'25'}});
  fireEvent.change(screen.getByLabelText('Output targetDurationSeconds'),{target:{value:'40'}});
  fireEvent.click(screen.getByText('Save output profile'));
  await waitFor(()=>expect(request).toHaveBeenCalledWith('output_profile_save',{path:'p',expectedRevision:4,profile:expect.objectContaining({targetDurationSeconds:40,safeArea:expect.objectContaining({bottom:.25})})}));
  expect(saved).toHaveBeenCalledOnce();
 });
 it('requires proposal review before creating separate variants and chains revisions',async()=>{
  let revision=4;const request=vi.fn(async(method:string)=>method==='output_profiles'?{revision:4,presets:[profile],profile}:method==='output_variants'?{revision:4,activeId:null,variants:[]}:method==='output_request'?{profiles:[{...profile,id:'one'},{...profile,id:'two'}],subjectMode:'keep_all',layout:'single',requiresReview:true}:method==='output_variant_save'?{revision:++revision,activeId:'v',variants:[],timeline:{}}:{briefs:[]});
  render(<OutputProfilesPanel bridge={{request} as unknown as DesktopBridge} path="p" revision={4} onSaved={()=>undefined}/>);
  fireEvent.change(screen.getByLabelText('Output request'),{target:{value:'Two vertical highlight clips; keep both speakers visible.'}});
  fireEvent.click(screen.getByText('Propose output constraints'));
  await screen.findByLabelText('Review proposed outputs');
  expect(request.mock.calls.some(c=>c[0]==='output_variant_save')).toBe(false);
  fireEvent.click(screen.getByText('Create reviewed output versions'));
  await waitFor(()=>expect(request).toHaveBeenCalledWith('output_variant_save',expect.objectContaining({expectedRevision:5,profile:expect.objectContaining({id:'two'})})));
 });
});
