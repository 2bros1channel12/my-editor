"""Format constraints stay evidence-bound and independent across output jobs."""
from copy import deepcopy
import pytest
from localcut_service.briefs import DeterministicBriefParser
from localcut_service.export_profiles import profile_presets
from localcut_service.planning import planning_job, planning_output_jobs, PlanningHandler, collect_evidence, validate_written_plan
from localcut_service.plan_review import list_written_plans
from localcut_service.jobs import JobQueue
from test_planning import project, Runtime

def profile(key): return next(p for p in profile_presets() if p['id']==key)

def test_brief_optional_output_constraints_are_closed():
    parser=DeterministicBriefParser()
    assert 'outputProfiles' not in parser.parse('Explain camera operation')
    brief=parser.parse('Turn this into three vertical highlight clips. Keep both speakers visible.')
    assert len(brief['outputProfiles'])==3 and brief['subjectMode']=='keep_all'
    assert parser.parse('Put the gameplay above the face camera')['layout']=='stacked'
    with pytest.raises(ValueError): parser.parse('Brief',{'subjectMode':'run shell'})
    with pytest.raises(ValueError): parser.parse('Brief',{'outputProfiles':[{'command':'ffmpeg'}]})

def test_separate_format_plans_share_original_and_review_remains_current(project,tmp_path):
    store,asset,_,_,source=project
    horizontal,vertical=profile('youtube'),profile('tiktok')
    brief=store.add_brief('Two versions',constraints={'mustKeep':['first topic'],'mustRemove':[],'outputProfiles':[horizontal,vertical],'subjectMode':'keep_all','layout':'single'})
    runtime=Runtime(); jobs=planning_output_jobs(store,brief,asset,runtime)
    assert len(jobs)==2 and jobs[0].asset_id==jobs[1].asset_id==asset
    assert jobs[0].source_fingerprint!=jobs[1].source_fingerprint
    queue=JobQueue(tmp_path/'jobs',handlers={'planning':PlanningHandler(runtime)},concurrency=1)
    try:
        for job in jobs:
            submitted=queue.submit(job); result=queue.wait(submitted['id'],5)
            assert result['state']=='completed',result.get('error')
    finally: queue.close()
    records=store.list_edit_plans(brief)
    assert len(records)==2 and {r['plan']['outputProfile']['aspect'] for r in records}=={'horizontal','vertical'}
    assert len(store.load_state()['assets'])==1 and source.read_bytes()==b'unchanged original'
    for record in records:
        plan=record['plan']; evidence=collect_evidence(store,brief,asset,output_profile=plan['outputProfile'],output_index=plan['outputIndex'])
        validate_written_plan(plan,evidence)
        changed=deepcopy(plan); changed['outputProfile']['safeArea']['bottom']=.4
        with pytest.raises(ValueError,match='fabricated'): validate_written_plan(changed,evidence)
    assert all(not r['stale'] for r in list_written_plans(store,runtime=runtime))

def test_max_duration_is_rejected_without_truncating_required_footage(project,tmp_path):
    store,asset,_,brief,_=project; runtime=Runtime()
    short=profile('tiktok'); short.update(targetDurationSeconds=3,maxDurationSeconds=3)
    job=planning_job(store,brief,asset,runtime,output_profile=short)
    queue=JobQueue(tmp_path/'jobs',handlers={'planning':PlanningHandler(runtime)},concurrency=1)
    try:
        submitted=queue.submit(job); result=queue.wait(submitted['id'],5)
        assert result['state']=='failed' and 'maximum duration' in str(result.get('error'))
        assert store.list_edit_plans()==[]
    finally: queue.close()
