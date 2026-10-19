from copy import deepcopy
import sqlite3
import time
import pytest
from localcut_service.persistence import ProjectStore, ProjectError
from localcut_service.timeline import timeline_state, timeline_command, timeline_undo, timeline_redo, compile_plan

@pytest.fixture
def project(tmp_path):
    source=tmp_path/'source.mp4'; source.write_bytes(b'original media')
    store=ProjectStore.create(tmp_path/'project','Timeline'); asset=store.add_asset(source)
    with sqlite3.connect(store.database_path) as db: db.execute('UPDATE assets SET duration_ticks=? WHERE id=?',(100_000_000,asset))
    return store,asset,source

def command(store,**value): return timeline_command(store,value,timeline_state(store)['revision'])
def insert(store,asset,a=0,b=1_000_000,**extra): return command(store,type='insert',assetId=asset,trackId='video',sourceInTicks=a,sourceOutTicks=b,**extra)

def test_split_trim_delete_undo_redo_survive_restart(project):
    store,asset,source=project; before=source.read_bytes()
    doc=insert(store,asset,b=4_000_000); cid=doc['clips'][0]['id']
    doc=command(store,type='split',clipId=cid,atTicks=2_000_000)
    ids=[c['id'] for c in doc['clips']]; assert len(set(ids))==2
    doc=command(store,type='trim',clipId=ids[1],sourceInTicks=2_500_000,sourceOutTicks=4_000_000)
    doc=command(store,type='delete',clipId=ids[0],ripple=True)
    assert doc['clips'][0]['timelineStartTicks']==0
    reopened=ProjectStore.open(store.root)
    restored=timeline_undo(reopened,doc['revision']); assert len(restored['clips'])==2
    redone=timeline_redo(reopened,restored['revision']); assert len(redone['clips'])==1
    assert redone['clips'][0]['id']==ids[1]
    assert source.read_bytes()==before

def test_tracks_gaps_fades_gain_markers_caption_linkage(project):
    store,asset,_=project
    doc=insert(store,asset,b=4_000_000); cid=doc['clips'][0]['id']
    command(store,type='caption',clipId=cid,text='Reviewed caption',sourceInTicks=1_000_000,sourceOutTicks=3_000_000)
    command(store,type='gain',clipId=cid,gainDb=-8)
    command(store,type='fades',clipId=cid,fadeInTicks=100_000,fadeOutTicks=200_000)
    doc=command(store,type='split',clipId=cid,atTicks=2_000_000)
    assert len(doc['captions'])==2
    captions=[c for c in doc['clips'] if c['itemType']=='caption']; assert all(c['timelineDurationTicks']==1_000_000 for c in captions)
    command(store,type='gap',trackId='video',atTicks=0,durationTicks=500_000)
    doc=command(store,type='marker',atTicks=500_000,label='Opening')
    assert min(c['timelineStartTicks'] for c in doc['clips'])==500_000
    assert doc['markers'][0]['label']=='Opening'
    doc=command(store,type='insert',assetId=asset,trackId='audio',sourceInTicks=0,sourceOutTicks=1_000_000,atTicks=0)
    assert any(c['itemType']=='audio' for c in doc['clips'])

@pytest.mark.parametrize('mutation',[
 {'type':'insert','assetId':'unknown','trackId':'video','sourceInTicks':0,'sourceOutTicks':100},
 {'type':'insert','trackId':'video','sourceInTicks':True,'sourceOutTicks':100},
 {'type':'insert','trackId':'video','sourceInTicks':0,'sourceOutTicks':100_000_001},
 {'type':'insert','trackId':'video','sourceInTicks':0,'sourceOutTicks':100,'shell':'rm'},
 {'type':'ripple','trackId':'video','atTicks':0,'deltaTicks':-1},
])
def test_invalid_commands_are_atomic(project,mutation):
    store,asset,_=project; insert(store,asset); before=store.load_state()
    if mutation['type']=='insert' and 'assetId' not in mutation: mutation={**mutation,'assetId':asset}
    with pytest.raises((ValueError,ProjectError)): timeline_command(store,mutation,before['revision'])
    assert store.load_state()==before

def test_overlap_revision_redo_branch_and_source_bounds(project):
    store,asset,_=project; doc=insert(store,asset); cid=doc['clips'][0]['id']
    with pytest.raises(ValueError,match='overlap'): insert(store,asset,atTicks=0)
    with pytest.raises(ProjectError,match='changed'): timeline_command(store,{'type':'delete','clipId':cid},0)
    for gain in (float('nan'),float('inf'),True,25):
        with pytest.raises(ValueError): command(store,type='gain',clipId=cid,gainDb=gain)
    with pytest.raises(ValueError): command(store,type='trim',clipId=cid,sourceInTicks=10,sourceOutTicks=100_000_001)
    doc=command(store,type='move',clipId=cid,atTicks=50)
    doc=timeline_undo(store,doc['revision']); assert doc['canRedo']
    doc=command(store,type='marker',atTicks=0,label='New branch'); assert not doc['canRedo']

def test_reorder_ripple_and_serialization(project):
    store,asset,_=project; insert(store,asset); doc=insert(store,asset,1_000_000,2_000_000)
    order=[c['id'] for c in reversed(doc['clips'])]
    doc=command(store,type='reorder',trackId='video',clipIds=order)
    assert sorted(doc['clips'],key=lambda c:c['timelineStartTicks'])[0]['id']==order[0]
    doc=command(store,type='ripple',trackId='video',atTicks=1_000_000,deltaTicks=100)
    assert timeline_state(ProjectStore.open(store.root))==doc

def test_legacy_projection_change_invalidates_history(project):
    store,asset,_=project; insert(store,asset); state=store.load_state(); state['timelineItems'][0]['timelineStartTicks']=100
    store.save_state(store.project_id,state)
    doc=timeline_state(store); assert not doc['canUndo']; assert doc['clips'][0]['timelineStartTicks']==100

def test_large_timeline_validation_and_bounded_history(project):
    store,asset,_=project; insert(store,asset); state=store.load_state(); template=state['timelineItems'][0]
    state['timelineItems']=[{**deepcopy(template),'id':f'clip-{i}','timelineStartTicks':i*1_000_000} for i in range(2000)]
    state['settings'].pop('timelineEngine'); store.save_state(store.project_id,state)
    start=time.monotonic(); doc=timeline_state(store)
    assert len(doc['clips'])==2000; assert time.monotonic()-start<3

def test_compile_requires_explicit_approval_and_is_undoable(project,monkeypatch):
    from localcut_service import plan_review, planning
    store,asset,_=project
    evidence={'fingerprint':'fixed','candidates':[{'id':'candidate','assetId':asset,'sourceInTicks':0,'sourceOutTicks':1_000_000}]}
    monkeypatch.setattr(plan_review,'_evidence',lambda *a,**k:deepcopy(evidence))
    monkeypatch.setattr(planning,'validate_written_plan',lambda *a:None)
    plan={'reviewVersion':1,'evidenceFingerprint':'fixed','provenance':{'modelName':'local'},'sections':[{'id':'section','status':'pending','title':'Opening','selections':[{'candidateId':'candidate'}]}]}
    pid=store.add_edit_plan(plan,status='pending',rationale=[],generator_id='test',generator_version='1')
    revision=timeline_state(store)['revision']
    with pytest.raises(ValueError,match='approved'): compile_plan(store,pid,['section'],1,revision)
    with sqlite3.connect(store.database_path) as db:
        import json
        plan['sections'][0]['status']='approved'; db.execute('UPDATE edit_plans SET plan_json=? WHERE id=?',(json.dumps(plan),pid))
    doc=compile_plan(store,pid,['section'],1,revision)
    assert doc['clips'][0]['properties']['sectionId']=='section'
    clip_id=doc['clips'][0]['id']
    doc=command(store,type='move',clipId=clip_id,atTicks=100)
    assert doc['markers'][0]['atTicks']==100
    doc=command(store,type='delete',clipId=clip_id)
    assert doc['markers']==[]
    doc=timeline_undo(store,doc['revision']); assert doc['markers'][0]['atTicks']==100
    doc=timeline_undo(store,doc['revision']); assert doc['markers'][0]['atTicks']==0
    revision=doc['revision']; doc=compile_plan(store,pid,['section'],1,revision); assert len(doc['clips'])==1; assert doc['revision']==revision
    doc=timeline_undo(store,doc['revision']); assert doc['clips']==[]

def test_caption_track_move_compatibility_fade_trim_and_gap_preservation(project):
    store,asset,_=project; doc=insert(store,asset,b=4_000_000); cid=doc['clips'][0]['id']
    command(store,type='fades',clipId=cid,fadeInTicks=1_000_000,fadeOutTicks=1_000_000)
    doc=command(store,type='trim',clipId=cid,sourceInTicks=0,sourceOutTicks=100_000)
    assert doc['clips'][0]['properties']['videoFadeInTicks']==50_000
    with pytest.raises(ValueError,match='video track'): command(store,type='move',clipId=cid,atTicks=0,trackId='audio')
    doc=command(store,type='caption',clipId=cid,text='Hello')
    caption=next(c for c in doc['clips'] if c['itemType']=='caption')
    assert next(t for t in doc['tracks'] if t['id']==caption['trackId'])['kind']=='caption'
    doc=insert(store,asset,1_000_000,2_000_000,atTicks=500_000)
    clips=sorted((c for c in doc['clips'] if c['itemType']=='clip'),key=lambda c:c['timelineStartTicks'])
    doc=command(store,type='reorder',trackId='video',clipIds=[c['id'] for c in reversed(clips)])
    clips=sorted((c for c in doc['clips'] if c['itemType']=='clip'),key=lambda c:c['timelineStartTicks'])
    assert clips[1]['timelineStartTicks']-(clips[0]['timelineStartTicks']+clips[0]['timelineDurationTicks'])==400_000

def test_malformed_document_and_unknown_schema_fail_actionably(project):
    import json
    store,asset,_=project; insert(store,asset)
    state=store.load_state(); state['settings']['timelineEngine']['tracks']=[None]
    with sqlite3.connect(store.database_path) as db: db.execute('UPDATE projects SET settings_json=?',(json.dumps(state['settings']),))
    with pytest.raises(ValueError,match='Malformed'): timeline_state(store)

def test_caption_edit_delete_and_undo_are_nondestructive(project):
    store,asset,source=project; original=source.read_bytes()
    doc=insert(store,asset); parent=doc['clips'][0]['id']
    doc=command(store,type='caption',clipId=parent,text='Original')
    caption=doc['captions'][0]; item_id=caption['timelineItemId']
    doc=command(store,type='edit_caption',captionId=caption['id'],text='Reviewed')
    assert doc['captions'][0]['text']=='Reviewed'
    assert next(c for c in doc['clips'] if c['id']==item_id)['properties']['text']=='Reviewed'
    with pytest.raises(ValueError,match='cannot ripple'): command(store,type='delete',clipId=item_id,ripple=True)
    doc=command(store,type='delete',clipId=item_id)
    assert doc['captions']==[]; assert [c['id'] for c in doc['clips']]==[parent]
    doc=timeline_undo(store,doc['revision']); assert doc['captions'][0]['text']=='Reviewed'
    doc=timeline_undo(store,doc['revision']); assert doc['captions'][0]['text']=='Original'
    assert source.read_bytes()==original
