from copy import deepcopy
import sqlite3
import pytest
from localcut_service.export_profiles import profile_presets, validate_profile, resolve_profile, caption_safe_rect, parse_output_request
from localcut_service.output_variants import save_variant, select_variant, list_variants
from localcut_service.persistence import ProjectStore, ProjectError
from localcut_service.timeline import timeline_state, timeline_command

def preset(key): return next(p for p in profile_presets() if p['id']==key)

def test_profiles_resolve_original_and_caption_safe_areas():
    for p in profile_presets(): assert validate_profile(p)==p
    assert resolve_profile(preset('original'),1080,1920)['aspectRatio']=='9:16'
    vertical=preset('tiktok'); rect=caption_safe_rect(vertical)
    assert rect=={'x':54,'y':154,'width':864,'height':1382}
    vertical['safeArea']['bottom']=.3
    assert caption_safe_rect(vertical)['height']<rect['height']

@pytest.mark.parametrize('patch',[{'width':1079},{'frameRate':float('nan')},{'frameRate':True},{'aspect':'execute'},{'targetDurationSeconds':999999},{'safeArea':{'top':0,'left':0,'right':0,'bottom':-1}},{'ffmpeg':'bad'}])
def test_profiles_reject_unvalidated_values(patch):
    with pytest.raises(ValueError): validate_profile({**preset('youtube'),**patch})

def test_sample_requests_produce_reviewable_constraints():
    p=parse_output_request('Make a vertical 45-second TikTok.')
    assert p['profiles'][0]['height']==1920 and p['profiles'][0]['targetDurationSeconds']==45
    p=parse_output_request('Create a horizontal eight-minute YouTube video.')
    assert p['profiles'][0]['targetDurationSeconds']==480 and p['profiles'][0]['aspect']=='horizontal'
    p=parse_output_request('Turn this into three vertical highlight clips. Keep both speakers visible.')
    assert len(p['profiles'])==3 and len({x['id'] for x in p['profiles']})==3
    assert p['subjectMode']=='keep_all' and p['requiresReview']
    assert parse_output_request('Put the gameplay above the face camera.')['layout']=='stacked'

def test_isolated_variants_reopen_without_copying_sources(tmp_path):
    source=tmp_path/'source.mp4'; source.write_bytes(b'original unchanged')
    store=ProjectStore.create(tmp_path/'project','Variants'); asset=store.add_asset(source)
    with sqlite3.connect(store.database_path) as db: db.execute('UPDATE assets SET duration_ticks=10000000 WHERE id=?',(asset,))
    doc=timeline_command(store,{'type':'insert','assetId':asset,'trackId':'video','sourceInTicks':0,'sourceOutTicks':5_000_000},timeline_state(store)['revision'])
    first=save_variant(store,'Horizontal',preset('youtube'),doc['revision'])
    horizontal=first['activeId']
    second=save_variant(store,'Vertical',preset('tiktok'),first['revision'])
    vertical=second['activeId']; clip=second['timeline']['clips'][0]
    trimmed=timeline_command(store,{'type':'trim','clipId':clip['id'],'sourceInTicks':1_000_000,'sourceOutTicks':4_000_000},second['revision'])
    switched=select_variant(store,horizontal,trimmed['revision'])
    assert switched['timeline']['durationTicks']==5_000_000
    reopened=ProjectStore.open(store.root)
    restored=select_variant(reopened,vertical,switched['revision'])
    assert restored['timeline']['durationTicks']==3_000_000 and restored['timeline']['canUndo']
    state=reopened.load_state()
    assert len(state['assets'])==1 and state['assets'][0]['id']==asset
    assert state['settings']['outputProfile']['aspect']=='vertical'
    assert source.read_bytes()==b'original unchanged'
    assert not list(store.root.rglob('*.mp4'))
    with pytest.raises(ProjectError): select_variant(store,horizontal,0)
    assert len(list_variants(store)['variants'])==3
