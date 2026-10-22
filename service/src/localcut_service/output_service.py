"""Output profiles and review-only framing jobs, separate from timeline operations."""
from pathlib import Path
import hashlib
import json
import shutil
import math
from fractions import Fraction
from .export_profiles import profile_presets, validate_profile
from .jobs import JobInput, JobOutput
from .persistence import ProjectStore, ProjectError, _connect, _json, _sha256_file
from .timeline import _base, _persist
from .proxies import source_path, rotation, source_digest
from .reframing import OpenCVSubjectDetector, detect_video_samples, build_reframe_keyframes, VERSION


def output_state(store):
    state=store.load_state()
    profile=state['settings'].get('outputProfile')
    return {'revision':state['revision'],'presets':profile_presets(),'profile':validate_profile(profile) if profile else None}


def save_profile(store,profile,expected_revision):
    profile=validate_profile(profile)
    with _connect(store.database_path) as db:
        db.execute('BEGIN IMMEDIATE');state=store.load_state(db)
        if type(expected_revision) is not int or state['revision']!=expected_revision: raise ProjectError('Project changed. Refresh output settings.')
        state['settings']['outputProfile']=profile
        result=_persist(store,db,state,_base(state));db.commit()
    store._checkpoint_if_due(result['revision'])
    return output_state(store)


def reframe_job(store,clip_id,profile,ffmpeg,subject_id=None,keep_both=False):
    profile=validate_profile(profile)
    if subject_id is not None and (not isinstance(subject_id,str) or len(subject_id)>120):raise ValueError('Invalid subject choice.')
    if type(keep_both) is not bool:raise ValueError('Keep both must be boolean.')
    state=store.load_state();clip=next((c for c in state['timelineItems'] if c['id']==clip_id and c['itemType']=='clip'),None)
    if clip is None:raise ValueError('Select a video clip for framing.')
    if clip.get('properties',{}).get('reframe',{}).get('locked'):raise ValueError('Unlock the selected crop before analyzing.')
    asset=next(a for a in state['assets'] if a['id']==clip['assetId'])
    executable=ffmpeg or shutil.which('ffmpeg')
    if not executable:raise ValueError('Configure local FFmpeg before subject detection.')
    detector=OpenCVSubjectDetector()
    payload={'clipId':clip_id,'assetId':asset['id'],'sourceInTicks':clip['sourceInTicks'],'sourceOutTicks':clip['sourceOutTicks'],'subjectId':subject_id,'keepBoth':keep_both,'profile':profile}
    config={'sha256':asset['sha256'],'ffmpeg':executable,'detector':detector.name,'version':detector.version}
    fingerprint=hashlib.sha256(_json({'payload':payload,'config':config}).encode()).hexdigest()
    return JobInput(str(store.root),'reframing',payload,fingerprint,pipeline_version=VERSION,asset_id=asset['id'],generator_id=detector.name,generator_version=detector.version,config=config)


class ReframeHandler:
    kind='reframing';retry_safe=True
    def validate_input(self,payload):
        if not isinstance(payload,dict) or set(payload)!={'clipId','assetId','sourceInTicks','sourceOutTicks','subjectId','keepBoth','profile'}:raise ValueError('Invalid framing request.')
        validate_profile(payload['profile'])
        if any(not isinstance(payload[k],str) or not payload[k] or len(payload[k])>200 for k in ('clipId','assetId')):raise ValueError('Invalid framing asset or clip ID.')
        if payload['subjectId'] is not None and (not isinstance(payload['subjectId'],str) or len(payload['subjectId'])>120):raise ValueError('Invalid framing subject.')
        if type(payload['sourceInTicks']) is not int or type(payload['sourceOutTicks']) is not int or not 0<=payload['sourceInTicks']<payload['sourceOutTicks'] or type(payload['keepBoth']) is not bool:raise ValueError('Invalid framing source range.')
        return payload
    def run(self,context,payload):
        store=ProjectStore.open(Path(context.input.project_path));state=store.load_state()
        asset=next(a for a in state['assets'] if a['id']==payload['assetId'])
        path=source_path(store,asset)
        if payload['sourceOutTicks']>asset['duration_ticks'] or source_digest(path,context)!=context.input.config['sha256']:raise ValueError('Source changed; reimport before reframing.')
        metadata=json.loads(asset['metadata_json']);video=metadata['video']
        sw,sh=int(video['width']),int(video['height'])
        try:
            sar=Fraction(str(video.get('sampleAspectRatio') or '1:1').replace(':','/'))
            if not 0<sar<=100:raise ValueError('Invalid source pixel aspect ratio.')
        except (ValueError,ZeroDivisionError) as exc:raise ValueError('Source pixel aspect ratio is unknown. Reinspect media.') from exc
        sw=max(1,round(sw*float(sar)))
        rotation_value=video.get('rotation',0)
        degrees=rotation_value.get('degrees',0) if isinstance(rotation_value,dict) else rotation_value
        if degrees is None or not isinstance(degrees,(int,float)) or not math.isfinite(degrees):raise ValueError('Source rotation is unknown. Reinspect media before reframing.')
        if round(degrees)%180:sw,sh=sh,sw
        from .export_profiles import resolve_profile
        profile=resolve_profile(payload['profile'],sw,sh)
        start,end=payload['sourceInTicks']/1e6,payload['sourceOutTicks']/1e6
        count=min(128,payload['sourceOutTicks']-payload['sourceInTicks'],max(2,int((end-start)/.5)+1))
        # Integer source ticks keep even sub-millisecond samples distinct and in bounds.
        span=payload['sourceOutTicks']-payload['sourceInTicks']
        timestamps=[(payload['sourceInTicks']+(span*i//count))/1e6 for i in range(count)]
        detector=OpenCVSubjectDetector()
        if detector.name!=context.input.config['detector'] or detector.version!=context.input.config['version']:raise ValueError('Subject detector changed. Submit framing again.')
        samples=detect_video_samples(path,timestamps,context,ffmpeg=context.input.config['ffmpeg'],detector=detector)
        result=build_reframe_keyframes(samples,sw,sh,profile['width'],profile['height'],selected_subject_id=payload['subjectId'],keep_both=payload['keepBoth'],safe_area=profile['safeArea'],check_cancelled=context.check_cancelled)
        result.update(assetId=asset['id'],clipId=payload['clipId'],sourceSha256=asset['sha256'],sourceWidth=sw,sourceHeight=sh,outputWidth=profile['width'],outputHeight=profile['height'],detector=detector.name,detectorVersion=detector.version,sourceInTicks=payload['sourceInTicks'],sourceOutTicks=payload['sourceOutTicks'],profile=payload['profile'],sourceFingerprint=context.input.source_fingerprint)
        if source_digest(path,context)!=asset['sha256']:raise ValueError('Source changed during subject detection.')
        output=context.staging_file('.json');output.write_text(_json(result),encoding='utf-8')
        return JobOutput(str(output),f'cache/reframing/{context.job_id}.json','reframing',{'assetId':asset['id'],'clipId':payload['clipId']})
    def validate_output(self,output):
        data=json.loads(Path(output.staging_path).read_text(encoding='utf-8'))
        validate_result(data)


def validate_result(data):
    if not isinstance(data,dict) or data.get('version')!=VERSION:raise ValueError('Invalid framing result version.')
    for key in ('assetId','clipId','sourceSha256','detector','detectorVersion','sourceFingerprint'):
        if not isinstance(data.get(key),str) or not data[key]:raise ValueError('Framing provenance is incomplete.')
    a,b=data.get('sourceInTicks'),data.get('sourceOutTicks')
    if type(a) is not int or type(b) is not int or not 0<=a<b:raise ValueError('Invalid framing source range.')
    validate_profile(data.get('profile'))
    if not isinstance(data.get('keyframes'),list) or not data['keyframes'] or len(data['keyframes'])>3600:raise ValueError('No valid framing samples were produced.')
    previous=-1
    for frame in data['keyframes']:
        if not isinstance(frame,dict):raise ValueError('Invalid framing keyframe.')
        t=frame.get('sourceTicks')
        if type(t) is not int or not a<=t<b or t<=previous:raise ValueError('Framing timestamps escape the selected source interval.')
        previous=t
        for key in ('x','y','width','height','confidence'):
            value=frame.get(key)
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<=value<=1:raise ValueError('Invalid normalized framing geometry.')
        if min(frame['width'],frame['height'])<=0 or frame['x']+frame['width']>1.000001 or frame['y']+frame['height']>1.000001:raise ValueError('Crop escapes original source frame.')
        if frame.get('layoutMode') not in ('crop','fit'):raise ValueError('Invalid framing layout mode.')


def framing_results(store,asset_id):
    state=store.load_state();asset=next((a for a in state['assets'] if a['id']==asset_id),None)
    if asset is None:raise ValueError('Imported asset was not found.')
    try:
        stat=source_path(store,asset).stat()
        if stat.st_size!=asset['size_bytes'] or stat.st_mtime_ns!=asset['mtime_ns']:return {'results':[]}
    except OSError:return {'results':[]}
    with _connect(store.database_path) as db:
        rows=db.execute("SELECT * FROM artifacts WHERE project_id=? AND asset_id=? AND kind='reframing' AND status='ready' ORDER BY created_at DESC",(store.project_id,asset_id)).fetchall()
    results=[]
    for row in rows:
        try:
            path=(store.root/row['relative_path']).resolve();path.relative_to((store.root/'cache'/'reframing').resolve())
            if not path.is_file() or path.stat().st_size>8*1024*1024 or _sha256_file(path)!=row['checksum_sha256']:continue
            data=json.loads(path.read_text(encoding='utf-8'));validate_result(data)
            if data['sourceOutTicks']>asset['duration_ticks'] or data['sourceSha256']!=asset['sha256'] or data['assetId']!=asset_id or data['sourceFingerprint']!=row['source_fingerprint'] or data['detector']!=row['generator_id'] or data['detectorVersion']!=row['generator_version']:continue
            results.append(data)
        except (OSError,ValueError,KeyError,TypeError):continue
    return {'results':results}
