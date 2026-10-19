"""Written-plan review is transactional and never changes the executable timeline."""
from copy import deepcopy
import pytest
from localcut_service.persistence import ProjectStore, ProjectError
from localcut_service.planning import recompute_plan
from localcut_service import plan_review


def selection(cid):
    return {'candidateId':cid,'reason':'Verified passage','confidence':.9,'confidenceReason':'Clear source','requirementIds':[]}


@pytest.fixture
def review(tmp_path, monkeypatch):
    store=ProjectStore.create(tmp_path/'project','Review')
    candidates=[{'id':str(i),'assetId':'asset','sourceInTicks':i*1000000,'sourceOutTicks':(i+1)*1000000,'uncertain':False} for i in range(5)]
    evidence={'briefId':None,'briefRevision':1,'assetId':'asset','candidates':candidates,'requirements':[],'requiredRanges':[],'excludedRanges':[],'sourceDurationTicks':5000000,'fingerprint':'original','query':'','searchMode':'exact'}
    monkeypatch.setattr(plan_review,'collect_evidence',lambda *a,**k:deepcopy(evidence))
    plan={key:deepcopy(evidence[key]) for key in ('briefId','briefRevision','assetId','candidates','requirements','requiredRanges','excludedRanges')}
    plan.update(query='',searchMode='exact',targetDurationTicks=None,warnings=[],provenance={'provider':'fake','modelName':'tiny','modelVersion':'1','settings':{},'executionMs':1},planType='written-rough-cut',schemaVersion=1,evidenceFingerprint='original',reviewVersion=0,hook={'candidateId':'0','reason':'Opening'},unmetRequirements=[],sections=[{'id':f's{i}','title':'Section','purpose':'Explain','selections':[selection(str(i))],'alternatives':[selection('3'),selection('4')],'alternativeNote':'','status':'pending','locked':False} for i in range(3)])
    recompute_plan(plan)
    def save():
        return store.add_edit_plan(plan,status='pending',rationale=[],generator_id='test',generator_version='1')
    return store,evidence,plan,save


def get(store,pid):
    return next(r['plan'] for r in store.list_edit_plans() if r['id']==pid)


def test_every_review_action_is_persisted_without_timeline_changes(review):
    store,evidence,plan,save=review; pid=save(); before=store.load_state()
    version=0
    def act(action,**kw):
        nonlocal version
        result=plan_review.review_plan(store,pid,action,expected_review_version=version,**kw)
        version+=1
        assert result['plan']['reviewVersion']==version
        return result['plan']
    assert act('approve',section_id='s0')['sections'][0]['status']=='approved'
    assert act('lock',section_id='s0')['sections'][0]['locked']
    assert not act('unlock',section_id='s0')['sections'][0]['locked']
    replaced=act('replace',section_id='s0',candidate_id='3')
    assert replaced['hook']['candidateId']=='3'
    assert replaced['sections'][0]['status']=='pending'
    assert any(s['candidateId']=='0' for s in replaced['sections'][0]['alternatives'])
    reordered=act('reorder',order=['s2','s1','s0'])
    assert reordered['hook']['candidateId']=='2'
    rejected=act('reject',section_id='s2')
    assert rejected['hook']['candidateId']=='1'
    assert get(store,pid)==rejected
    assert store.load_state()==before


def test_reject_all_removes_hook(review):
    store,_,_,save=review; pid=save()
    for i in range(3):
        result=plan_review.review_plan(store,pid,'reject',section_id=f's{i}',expected_review_version=i)
    assert result['plan']['hook'] is None
    assert result['plan']['estimatedDurationTicks']==0
    assert result['status']=='reviewed'
    restored=plan_review.review_plan(store,pid,'approve',section_id='s1',expected_review_version=3)
    assert restored['plan']['hook']['candidateId']=='1'


@pytest.mark.parametrize('action,kwargs',[
    ('reject',{'section_id':'s0'}),('replace',{'section_id':'s0','candidate_id':'3'}),
    ('reorder',{'order':['s1','s0','s2']})])
def test_locked_section_cannot_be_changed_or_displaced(review,action,kwargs):
    store,_,plan,save=review; plan['sections'][0]['locked']=True; pid=save(); before=get(store,pid)
    with pytest.raises(ProjectError,match='Unlock'):
        plan_review.review_plan(store,pid,action,expected_review_version=0,**kwargs)
    assert get(store,pid)==before
    # Other sections may move around an unchanged locked index.
    result=plan_review.review_plan(store,pid,'reorder',order=['s0','s2','s1'],expected_review_version=0)
    assert result['plan']['sections'][0]['id']=='s0'


@pytest.mark.parametrize('action,kwargs',[
    ('reorder',{'order':['s0','s0','s2']}),('reorder',{'order':['s0','s1']}),
    ('replace',{'section_id':'s0','candidate_id':'1'}),
    ('replace',{'section_id':'s0','candidate_id':"3; delete all footage"}),
    ('approve',{'section_id':"s0' OR 1=1 --"}),('execute',{})])
def test_invalid_or_adversarial_actions_do_not_partially_mutate(review,action,kwargs):
    store,_,_,save=review; pid=save(); before=get(store,pid); timeline=store.load_state()
    with pytest.raises(ValueError):
        plan_review.review_plan(store,pid,action,expected_review_version=0,**kwargs)
    assert get(store,pid)==before
    assert store.load_state()==timeline


def test_stale_review_version_and_evidence_are_rejected(review):
    store,evidence,_,save=review; pid=save()
    plan_review.review_plan(store,pid,'approve',section_id='s0',expected_review_version=0)
    with pytest.raises(ProjectError,match='changed since'):
        plan_review.review_plan(store,pid,'approve',section_id='s1',expected_review_version=0)
    evidence['fingerprint']='changed'
    assert plan_review.list_written_plans(store)[0]['stale']
    before=get(store,pid)
    with pytest.raises(ProjectError,match='evidence changed'):
        plan_review.review_plan(store,pid,'approve',section_id='s1',expected_review_version=1)
    assert get(store,pid)==before


@pytest.mark.parametrize('action,kwargs',[('reject',{}),('replace',{'candidate_id':'3'})])
def test_required_range_cannot_be_omitted(review,action,kwargs):
    store,evidence,plan,save=review
    required={'id':'keep','sourceInTicks':0,'sourceOutTicks':1000000}
    evidence['requiredRanges']=[required]; plan['requiredRanges']=[required]
    evidence['requirements']=[{'id':'range:keep','kind':'range','description':'Required'}]
    plan['requirements']=deepcopy(evidence['requirements']); recompute_plan(plan)
    pid=save(); before=get(store,pid)
    with pytest.raises(ValueError,match='required'):
        plan_review.review_plan(store,pid,action,section_id='s0',expected_review_version=0,**kwargs)
    assert get(store,pid)==before


def test_replacement_cannot_duplicate_other_section(review):
    store,_,plan,save=review
    plan['sections'][0]['alternatives'].append(selection('1')); pid=save(); before=get(store,pid)
    with pytest.raises(ValueError,match='duplicate|overlap'):
        plan_review.review_plan(store,pid,'replace',section_id='s0',candidate_id='1',expected_review_version=0)
    assert get(store,pid)==before


def test_excluded_source_and_malformed_plan_are_marked_stale(review):
    store,evidence,plan,save=review
    excluded={'id':'avoid','sourceInTicks':0,'sourceOutTicks':1000000}
    evidence['excludedRanges']=[excluded]; plan['excludedRanges']=[excluded]; pid=save()
    assert plan_review.list_written_plans(store)[0]['stale']
    with pytest.raises(ValueError,match='excluded'):
        plan_review.review_plan(store,pid,'approve',section_id='s0',expected_review_version=0)
