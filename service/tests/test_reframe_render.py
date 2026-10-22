"""Real decoder/filter checks, using generated media rather than original footage."""
import json
import shutil
import subprocess
import time
import pytest
from localcut_service.reframe_engine import framing_filter, validate_reframe
from localcut_service.export_profiles import profile_presets
from localcut_service.render import RenderHandler


@pytest.mark.parametrize('source,output,stacked', [((320,180),(180,320),False), ((180,320),(320,180),False), ((320,180),(180,320),True)])
def test_real_reframe_filters(tmp_path, source, output, stacked):
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe: pytest.skip('Local FFmpeg required')
    sw,sh=source;ow,oh=output
    settings=dict(enabled=True,locked=False,mode='crop',keyframes=[dict(sourceTicks=0,x=0,y=0,width=.6,height=1),dict(sourceTicks=800000,x=.4,y=0,width=.6,height=1)])
    if stacked: settings.update(layout='stacked',regions=[dict(x=0,y=0,width=.5,height=1),dict(x=.5,y=0,width=.5,height=1)])
    graph='[0:v]setpts=PTS-STARTPTS'+framing_filter(settings,sw,sh,ow,oh,0,1000000,10)+'[out]'
    path=tmp_path/'output 雪 [crop].mp4'
    started=time.perf_counter()
    result=subprocess.run([ffmpeg,'-v','error','-f','lavfi','-i',f'testsrc2=size={sw}x{sh}:rate=10:duration=1','-filter_complex',graph,'-map','[out]','-c:v','libx264','-pix_fmt','yuv420p',str(path)],capture_output=True,timeout=30)
    assert result.returncode==0,result.stderr.decode(errors='replace')
    probe=json.loads(subprocess.check_output([ffprobe,'-v','error','-show_streams','-of','json',str(path)]))['streams'][0]
    assert (probe['width'],probe['height'])==(ow,oh)
    assert float(probe['duration'])==pytest.approx(1,abs=.1)
    print(f'{source}->{output} stacked={stacked}: {time.perf_counter()-started:.3f}s')


def test_safe_caption_wrapping_uses_asymmetric_profile_margins():
    profile=next(p for p in profile_presets() if p['aspect']=='vertical')
    profile['safeArea']=dict(top=.1,right=.2,bottom=.25,left=.1)
    result=RenderHandler._ass_document([dict(text='A long caption that must wrap inside the portrait safe region',timelineStartTicks=0,timelineDurationTicks=1000000)],180,320,{'outputProfile':profile})
    assert r'\N' in result
    assert ',18,36,80,1' in result


def test_invalid_regions_and_out_of_bounds_keyframes_rejected():
    base=dict(enabled=True,locked=False,mode='crop',keyframes=[dict(sourceTicks=2,x=0,y=0,width=1,height=1)])
    with pytest.raises(ValueError):validate_reframe(base,1)
    base.update(keyframes=[],layout='stacked',regions=[dict(x=0,y=0,width=1,height=1,command='unsafe')]*2)
    with pytest.raises(ValueError):validate_reframe(base,1)


def test_crop_history_split_and_restart_preserve_source(tmp_path):
    from localcut_service.persistence import ProjectStore
    from localcut_service.timeline import timeline_state,timeline_command,timeline_undo,timeline_redo
    store=ProjectStore.create(tmp_path/'project','Crop history')
    original=tmp_path/'original.mp4';original.write_bytes(b'untouched original')
    asset=store.import_asset_reference(original,{'video':{'width':320,'height':180}},probe_tool='fixture',probe_version='1',duration_ticks=1000000)['assetId']
    def command(value):return timeline_command(store,value,timeline_state(store)['revision'])
    doc=command(dict(type='insert',assetId=asset,trackId='video',sourceInTicks=0,sourceOutTicks=1000000))
    cid=doc['clips'][0]['id']
    framing=dict(enabled=True,locked=False,mode='crop',keyframes=[dict(sourceTicks=0,x=.2,y=0,width=.6,height=1)])
    doc=command(dict(type='reframe',clipId=cid,settings=framing))
    restored=timeline_undo(store,doc['revision'])
    assert 'reframe' not in restored['clips'][0]['properties']
    doc=timeline_redo(store,restored['revision'])
    doc=command(dict(type='split',clipId=cid,atTicks=500000))
    reopened=timeline_state(ProjectStore.open(store.root))
    assert all(c['properties']['reframe']==framing for c in reopened['clips'])
    assert original.read_bytes()==b'untouched original'


@pytest.mark.parametrize('fit',['crop','contain'])
def test_profile_render_through_persistent_job(tmp_path,fit):
    from localcut_service.persistence import ProjectStore,_sha256_file
    from localcut_service.media import FFprobe,import_media
    from localcut_service.timeline import timeline_state,timeline_command
    from localcut_service.output_service import save_profile,ReframeHandler,reframe_job
    from localcut_service.render import render_job
    from localcut_service.jobs import JobQueue
    ffmpeg,ffprobe=shutil.which('ffmpeg'),shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:pytest.skip('Local media tools required')
    source=tmp_path/'original landscape 雪.mp4'
    subprocess.run([ffmpeg,'-v','error','-f','lavfi','-i','testsrc2=size=320x180:rate=30:duration=1','-f','lavfi','-i','sine=duration=1','-c:v','libx264','-c:a','aac','-shortest',str(source)],check=True,capture_output=True)
    digest=_sha256_file(source)
    store=ProjectStore.create(tmp_path/'project','Portrait render')
    asset=import_media(store,[source],probe=FFprobe(ffprobe))['imported'][0]['assetId']
    doc=timeline_command(store,dict(type='insert',assetId=asset,trackId='video',sourceInTicks=0,sourceOutTicks=1000000),timeline_state(store)['revision'])
    profile=next(p for p in profile_presets() if p['id']=='tiktok')
    profile.update(width=180,height=320,fit=fit)
    save_profile(store,profile,doc['revision'])
    queue=JobQueue(tmp_path/'jobs',handlers={'rendering':RenderHandler(ffmpeg,ffprobe),'reframing':ReframeHandler()},concurrency=1)
    try:
        job=queue.submit(render_job(store,ffmpeg,ffprobe))
        completed=queue.wait(job['id'],30)
        assert completed['state']=='completed',completed.get('error')
        if fit=='crop':
            tracking=queue.submit(reframe_job(store,doc['clips'][0]['id'],profile,ffmpeg))
            analyzed=queue.wait(tracking['id'],30)
            assert analyzed['state']=='completed',analyzed.get('error')
        assert _sha256_file(source)==digest
    finally:queue.close()
