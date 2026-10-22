import {useState} from 'react';
import type {CapabilityProfile,HealthCheck} from '../contracts';

export default function FirstRunChecks({health,profile,onCheck,onModels,onSample}:{health:Record<string,HealthCheck>|null;profile:CapabilityProfile|null;onCheck:()=>void;onModels:()=>void;onSample:()=>Promise<void>}) {
 const [dismissed,setDismissed]=useState(()=>{try{return localStorage.getItem('localcut-onboarding-v1')==='reviewed';}catch{return false;}}),[message,setMessage]=useState('');
 if(dismissed)return null;
 const ready=!!health?.service?.available&&!!health?.storage?.available;
 const ram=profile?.memory?.totalBytes;
 return <section className="capability-panel" aria-label="First run checks"><h2>Set up Local Cut</h2>
 <p>First release: Windows 11 x64. Editing runs locally. Download speech weights only when you choose a model; a local Whisper backend and FFmpeg are separately installed dependencies.</p>
 <ul><li>Local service: {health?.service?.available?'available':'checking or unavailable'}</li><li>Writable storage: {health?.storage?.available?'available':'checking or unavailable'}</li><li>FFmpeg: {health?.ffmpeg?.available?'available':'install/configure FFmpeg and FFprobe in Settings'}</li><li>Hardware: {profile?.platform?`${profile.platform.system} ${profile.platform.machine}; ${ram?(ram/1024**3).toFixed(1)+' GiB RAM':'RAM unknown'}`:'checking'}</li><li>FFprobe: {profile?.mediaTools?.ffprobe?.available?'available':'not detected; import requires FFprobe'}</li></ul>
 <p>{ram&&ram<16*1024**3?'This device is below the 16 GiB minimum target. Use tiny models and low-resolution proxies; this configuration is not release validated.':'Start with the recommended speech model; use tiny if memory is limited.'} Models shows download size, integrity, and whether the local speech backend is available.</p>
 <button onClick={onCheck}>Run dependency checks</button><button onClick={onModels}>Choose local models</button><button disabled={!ready||!health?.ffmpeg?.available} onClick={()=>void onSample().catch(e=>setMessage(String(e)))}>Create a local sample project</button><button disabled={!ready} onClick={()=>{try{localStorage.setItem('localcut-onboarding-v1','reviewed');}catch{/* Session dismissal remains available without browser storage. */}setDismissed(true);}}>Checks reviewed</button><p role="status">{message}</p>
 </section>;
}
