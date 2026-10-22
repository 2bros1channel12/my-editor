import {render,screen,fireEvent,waitFor} from '@testing-library/react';
import {it,expect,vi} from 'vitest';
import PrivacyStoragePanel from '../src/ui/PrivacyStoragePanel';
import FirstRunChecks from '../src/ui/FirstRunChecks';
import type {DesktopBridge} from '../src/bridge';
import type {LastProjectResponse} from '../src/contracts';

it('requires a current review token before deleting the project',async()=>{
 const onDeleted=vi.fn(),request=vi.fn(async(method:string)=>method==='storage_review'?{scope:'project',model:null,confirmationToken:'review-token',canRemove:true,files:4,bytes:1024,detail:'External originals preserved.',activeJobs:[]}:method==='storage_remove'?{removed:true,scope:'project',bytes:1024,projectDeleted:true}:{projectId:'p',telemetryEnabled:false,mediaUploadEnabled:false,network:{activeDownloads:[],policy:'explicit',localRuntimeUrl:null},storage:{projectBytes:1024,modelsBytes:0,transcriptsBytes:0,cacheBytes:0},models:[]});
 render(<PrivacyStoragePanel bridge={{request} as unknown as DesktopBridge} project={{path:'C:/project',projectId:'p'} as LastProjectResponse} onDeleted={onDeleted}/>);
 fireEvent.click(screen.getByText('Review complete project deletion'));
 await screen.findByText('External originals preserved.');
 expect(request.mock.calls.some(c=>c[0]==='storage_remove')).toBe(false);
 fireEvent.click(screen.getByText('Confirm removal'));
 await waitFor(()=>expect(onDeleted).toHaveBeenCalledOnce());
 expect(request).toHaveBeenCalledWith('storage_remove',{path:'C:/project',scope:'project',model:undefined,confirmationToken:'review-token'});
});

it('shows first-run dependency failures and does not allow sample creation until tools are ready',()=>{
 localStorage.clear(); const sample=vi.fn(async()=>undefined);
 render(<FirstRunChecks health={{service:{available:true,detail:''},storage:{available:true,detail:''},ffmpeg:{available:false,detail:''}}} profile={null} onCheck={()=>undefined} onModels={()=>undefined} onSample={sample}/>);
 expect(screen.getByText(/install\/configure FFmpeg and FFprobe/)).toBeVisible();
 expect(screen.getByText('Create a local sample project')).toBeDisabled();
 expect(sample).not.toHaveBeenCalled();
});
