import json
import sqlite3
from types import SimpleNamespace

import pytest

from localcut_service.config import AppConfig
from localcut_service.persistence import ProjectStore, ProjectError
from localcut_service.server import dispatch, handlers
from localcut_service.timeline import timeline_command
from localcut_service.export_profiles import profile_presets
from localcut_service.output_service import ReframeHandler, reframe_job, validate_result
from localcut_service.reframing import Detection


def test_output_profiles_variants_persist_via_rpc(tmp_path):
    store=ProjectStore.create(tmp_path/'project','Outputs');config=AppConfig(data_dir=tmp_path/'data')
    def call(method,**params):return dispatch(method,{'path':str(store.root),**params},config)
    state=call('output_profiles');vertical=next(p for p in state['presets'] if p['id']=='tiktok')
    saved=call('output_profile_save',profile=vertical,expectedRevision=state['revision'])
    assert saved['profile']==vertical
    with pytest.raises(ProjectError):call('output_profile_save',profile=vertical,expectedRevision=state['revision'])
    variant=call('output_variant_save',name='Vertical',profile=vertical,expectedRevision=saved['revision'])
    assert len(variant['variants'])==2
    restored=call('output_variant_select',variantId=variant['variants'][0]['id'],expectedRevision=variant['revision'])
    assert restored['activeId']==variant['variants'][0]['id']
    assert ProjectStore.open(store.root).load_state()['settings']['outputProfile']==vertical
    request=call('output_request',text='Turn this into three vertical highlight clips')
    assert len(request['profiles'])==3 and request['requiresReview']
    assert 'reframing' in handlers(config)


@pytest.mark.parametrize('duration',[1,100,1_000_000])
def test_reframing_job_rotation_short_samples_and_provenance(tmp_path,monkeypatch,duration):
    store=ProjectStore.create(tmp_path/'project','Frame');source=tmp_path/'a.mp4';source.write_bytes(b'original');asset=store.add_asset(source)
    with sqlite3.connect(store.database_path) as db:db.execute('UPDATE assets SET duration_ticks=?,metadata_json=? WHERE id=?',(duration,json.dumps({'video':{'width':1920,'height':1080,'rotation':{'degrees':90}}}),asset))
    doc=timeline_command(store,{'type':'insert','assetId':asset,'trackId':'video','sourceInTicks':0,'sourceOutTicks':duration},store.load_state()['revision'])
    class Detector:
        name='fixture';version='1'
    monkeypatch.setattr('localcut_service.output_service.OpenCVSubjectDetector',Detector)
    seen=[]
    def samples(path,timestamps,context,**kwargs):
        seen.extend(timestamps)
        return [(t,[Detection(.3,.2,.2,.2,.9)]) for t in timestamps]
    monkeypatch.setattr('localcut_service.output_service.detect_video_samples',samples)
    job=reframe_job(store,doc['clips'][0]['id'],profile_presets()[1],'ffmpeg')
    context=SimpleNamespace(input=job,job_id='fixture',check_cancelled=lambda:None,staging_file=lambda suffix:tmp_path/('stage'+suffix))
    output=ReframeHandler().run(context,job.payload);ReframeHandler().validate_output(output)
    data=json.loads((tmp_path/'stage.json').read_text())
    assert (data['sourceWidth'],data['sourceHeight'])==(1080,1920)
    assert all(0<=round(t*1e6)<duration for t in seen)
    assert len(set(seen))==len(seen)
    assert data['sourceFingerprint']==job.source_fingerprint
    data['keyframes'][0]['sourceTicks']=duration
    with pytest.raises(ValueError,match='timestamps'):validate_result(data)
    assert source.read_bytes()==b'original'


def test_reframe_generate_rpc_preserves_validated_arguments(tmp_path,monkeypatch):
    store=ProjectStore.create(tmp_path/'project','Frame');sent={}
    def job(project,clip,profile,ffmpeg,subject,keep):
        sent.update(clip=clip,subject=subject,keep=keep);return 'validated'
    monkeypatch.setattr('localcut_service.server.reframe_job',job)
    result=dispatch('reframe_generate',{'path':str(store.root),'clipId':'c','profile':profile_presets()[0],'subjectId':'subject-2','keepBoth':True},AppConfig(data_dir=tmp_path/'data'),SimpleNamespace(submit=lambda job:{'job':job}))
    assert result=={'job':'validated'} and sent=={'clip':'c','subject':'subject-2','keep':True}
