import { describe,it,expect } from 'vitest';
import { advanceGap, clipAt,orderedVideo,proxySeconds,sourceToTimeline,timelineToSource,visibleClips,type PlaybackClip } from '../src/timelinePlayback';
const clip=(id:string,start:number):PlaybackClip=>({id,assetId:'a',itemType:'clip',trackId:'video',timelineStartTicks:start,timelineDurationTicks:1000000,sourceInTicks:2000000,sourceOutTicks:3000000});
describe('timeline playback mappings',()=>{
 it('uses incoming clip at exact cut and leaves gaps empty',()=>{const clips=[clip('a',0),clip('b',1000000),clip('c',3000000)];expect(clipAt(clips,999999)?.id).toBe('a');expect(clipAt(clips,1000000)?.id).toBe('b');expect(clipAt(clips,2000000)).toBeUndefined();});
 it('maps trimmed sources and nonzero proxy offsets round trip',()=>{const c=clip('a',5000000);expect(timelineToSource(c,5500000)).toBe(2500000);expect(sourceToTimeline(c,2500000)).toBe(5500000);expect(proxySeconds(c,5500000,.5)).toBe(2);});
 it('clamps out of bounds and selects the correct repeated occurrence',()=>{const c=clip('repeat',5000000);expect(sourceToTimeline(c,0)).toBe(5000000);expect(timelineToSource(c,9000000)).toBe(3000000);});
 it('bounds rendered clips and quickly looks up large timelines',()=>{const clips=orderedVideo(Array.from({length:10000},(_,i)=>clip(String(i),i*1000000)));expect(visibleClips(clips,500000000,510000000)).toHaveLength(10);const start=performance.now();for(let i=0;i<20000;i++)expect(clipAt(clips,(i%10000)*1000000)?.id).toBe(String(i%10000));expect(performance.now()-start).toBeLessThan(3000);});
});

it('clamps gap wall clock at the exact incoming source boundary',()=>{expect(advanceGap([clip('next',1000000)],990000,50000,2000000)).toBe(1000000);});
