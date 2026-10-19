export interface PlaybackClip { id: string; assetId: string | null; itemType: string; trackId: string; timelineStartTicks: number; timelineDurationTicks: number; sourceInTicks: number; sourceOutTicks: number; properties?: Record<string, unknown> }
export function orderedVideo(clips: PlaybackClip[]) { return clips.filter(c=>c.itemType==='clip').sort((a,b)=>a.timelineStartTicks-b.timelineStartTicks||a.id.localeCompare(b.id)); }
/** Intervals are half open: at a cut the incoming clip owns the frame. */
export function clipAt(clips: PlaybackClip[], ticks: number) { let low=0,high=clips.length; while(low<high){const mid=(low+high)>>>1;if(clips[mid].timelineStartTicks<=ticks)low=mid+1;else high=mid;} const c=clips[low-1];return c&&ticks<c.timelineStartTicks+c.timelineDurationTicks?c:undefined; }
export function timelineToSource(clip: PlaybackClip,ticks:number){return Math.round(clip.sourceInTicks+Math.max(0,Math.min(clip.timelineDurationTicks,ticks-clip.timelineStartTicks))*(clip.sourceOutTicks-clip.sourceInTicks)/clip.timelineDurationTicks);}
export function sourceToTimeline(clip:PlaybackClip,ticks:number){return Math.round(clip.timelineStartTicks+Math.max(0,Math.min(clip.sourceOutTicks-clip.sourceInTicks,ticks-clip.sourceInTicks))*clip.timelineDurationTicks/(clip.sourceOutTicks-clip.sourceInTicks));}
export function proxySeconds(clip:PlaybackClip,ticks:number,offset:number){return Math.max(0,timelineToSource(clip,ticks)/1e6-offset);}
export function visibleClips(clips:PlaybackClip[],start:number,end:number){return clips.filter(c=>c.timelineStartTicks<end&&c.timelineStartTicks+c.timelineDurationTicks>start);}
/** Never step over a cut while advancing a gap clock. */
export function advanceGap(clips:PlaybackClip[],ticks:number,delta:number,duration:number){const next=clips.find(c=>c.timelineStartTicks>ticks);return Math.min(duration,next?.timelineStartTicks??duration,ticks+delta);}
