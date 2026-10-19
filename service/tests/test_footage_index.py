import pytest
import threading
import time
from dataclasses import replace
from localcut_service.footage_index import IndexHandler, indexing_job, search_index, index_status
from localcut_service.persistence import ProjectStore, _sha256_file
from localcut_service.jobs import JobQueue, JobCancelled
from localcut_service.analysis import DETECTORS

class Runtime:
    version='v1'
    def select(self,task,check_cancelled=None): return {'name':'tiny-embedding','version':self.version,'provider':'fixture','settings':{}}
    def embed(self,texts,check_cancelled=None):
        if check_cancelled: check_cancelled()
        return {'vectors':[[1.,0.] if any(term in text.casefold() for term in ('cat','feline')) else [0.,1.] for text in texts],'provenance':{'modelName':'tiny-embedding','modelVersion':self.version,'provider':'fixture','settings':{},'executionMs':1}}

@pytest.fixture
def project(tmp_path):
    store=ProjectStore.create(tmp_path/'project [雪]','Search')
    source=tmp_path/'source.mov'; source.write_bytes(b'original')
    asset=store.import_asset_reference(source,{},probe_tool='fixture',probe_version='1',duration_ticks=10_000_000)['assetId']
    transcript=store.add_transcript(asset_id=asset,language='en',words=[],segments=[{'text':'Cat lesson ignore instructions <script> -- OR 1=1','sourceInTicks':100_000,'sourceOutTicks':1_000_000},{'text':'Dog lesson','sourceInTicks':2_000_000,'sourceOutTicks':3_000_000}],generator_id='fixture',generator_version='1',source_fingerprint=_sha256_file(source))
    return store,asset,transcript,source

def build(tmp_path,store,runtime=None,semantic=False,force=False):
    queue=JobQueue(tmp_path/'queue',handlers={'indexing':IndexHandler(runtime)},concurrency=1)
    try:
        submitted=queue.submit(indexing_job(store,runtime,semantic=semantic,force=force))
        if submitted['id']:
            result=queue.wait(submitted['id'],5)
            assert result['state']=='completed',result.get('error')
        return submitted
    finally: queue.close()

def test_literal_search_and_precise_half_open_filters(project,tmp_path):
    store,asset,_,source=project
    build(tmp_path,store)
    for query in ('ignore instructions','<script>','-- OR 1=1'):
        result=search_index(store,query)['results']
        assert len(result)==1 and result[0]['sourceInTicks']==100_000
    assert search_index(store,'lesson',filters={'sourceInTicks':1_000_000,'sourceOutTicks':2_000_000})['results']==[]
    assert len(search_index(store,'lesson',filters={'assetId':asset})['results'])==2
    assert source.read_bytes()==b'original'

def test_semantic_versions_and_review_invalidation(project,tmp_path):
    store,_,transcript,_=project; runtime=Runtime()
    build(tmp_path,store,runtime,True)
    assert not search_index(store,'feline')['results']
    assert search_index(store,'feline',mode='semantic',runtime=runtime)['results'][0]['text'].startswith('Cat')
    runtime.version='v2'
    assert index_status(store,runtime)['stale']
    with pytest.raises(ValueError,match='out of date'): search_index(store,'cat',mode='semantic',runtime=runtime)
    runtime.version='v1'
    segment=store.get_transcript(transcript)['segments'][0]
    store.update_transcript_segment(transcript,segment['id'],'Reviewed cat')
    assert index_status(store)['stale']

def test_deduplication_and_force_rebuild(project,tmp_path):
    store,*_=project
    assert build(tmp_path,store)['id']
    assert build(tmp_path,store)['deduplicated']
    assert build(tmp_path,store,force=True)['id']

def test_cancelled_embedding_preserves_prior_index(project,tmp_path):
    store,*_=project; build(tmp_path,store)
    started=threading.Event()
    class Cancel(Runtime):
        def embed(self,texts,check_cancelled=None):
            started.set()
            for _ in range(500):
                check_cancelled()
                time.sleep(.01)
            raise AssertionError('Cancellation was not observed')
    queue=JobQueue(tmp_path/'cancel-queue',handlers={'indexing':IndexHandler(Cancel())},concurrency=1)
    try:
        job=queue.submit(indexing_job(store,Cancel(),semantic=True))
        assert started.wait(3)
        queue.cancel(job['id'])
        assert queue.wait(job['id'],5)['state']=='cancelled'
        assert search_index(store,'Cat')['results']
    finally: queue.close()

def test_invalid_vectors_never_publish(project,tmp_path):
    store,*_=project
    class Invalid(Runtime):
        def embed(self,*args,**kwargs):
            data=super().embed(*args,**kwargs); data['vectors'][0]=[float('nan'),0]; return data
    runtime=Invalid(); queue=JobQueue(tmp_path/'bad-queue',handlers={'indexing':IndexHandler(runtime)},concurrency=1)
    try:
        job=queue.submit(indexing_job(store,runtime,semantic=True))
        assert queue.wait(job['id'],5)['state']=='failed'
        assert not index_status(store)['available']
    finally: queue.close()


def test_detector_filters_and_invalidation(project,tmp_path):
    store,asset,_,source=project
    build(tmp_path,store)
    # No measurement is unknown, so silence=false must not label it clean.
    assert not search_index(store,'lesson',filters={'silence':False})['results']
    path=store.root/'cache'/'silence.json'; path.write_text('{}')
    artifact=store.register_artifact(asset_id=asset,kind='analysis:silence',relative_path='cache/silence.json',source_fingerprint=_sha256_file(source),generator_id='silence',generator_version=DETECTORS['silence']['version'],checksum_sha256=_sha256_file(path))
    store.add_analysis(asset_id=asset,kind='silence',source_in_ticks=100_000,source_out_ticks=200_000,result={},generator_id='silence',generator_version=DETECTORS['silence']['version'],source_fingerprint=_sha256_file(source),artifact_id=artifact)
    assert index_status(store)['stale']
    build(tmp_path,store)
    assert search_index(store,'lesson',filters={'silence':True})['results'][0]['text'].startswith('Cat')
    assert search_index(store,'lesson',filters={'silence':False})['results'][0]['text'].startswith('Dog')

def test_corrupt_latest_falls_back_to_previous_snapshot(project,tmp_path):
    from localcut_service.persistence import _connect
    store,*_=project
    build(tmp_path,store)
    first=index_status(store)['artifactId']
    build(tmp_path,store,force=True)
    latest=index_status(store)['artifactId']
    assert latest!=first
    with _connect(store.database_path) as db:
        path=db.execute('SELECT relative_path FROM artifacts WHERE id=?',(latest,)).fetchone()[0]
    (store.root/path).write_bytes(b'interrupted or corrupt')
    assert index_status(store)['artifactId']==first
    assert search_index(store,'Cat')['results']

def test_embedding_settings_and_dimensions_cannot_mix(project,tmp_path):
    store,*_=project
    class Mutated(Runtime):
        def embed(self,*args,**kwargs):
            result=super().embed(*args,**kwargs)
            result['provenance']['settings']={'unexpected':True}
            return result
    runtime=Mutated(); queue=JobQueue(tmp_path/'settings-queue',handlers={'indexing':IndexHandler(runtime)},concurrency=1)
    try:
        job=queue.submit(indexing_job(store,runtime,semantic=True))
        assert queue.wait(job['id'],5)['state']=='failed'
        assert not index_status(store)['available']
    finally: queue.close()

def test_changed_source_invalidates_index(project,tmp_path):
    store,_,_,source=project
    build(tmp_path,store)
    source.write_bytes(b'changed original outside Local Cut')
    assert index_status(store)['stale']
    with pytest.raises(ValueError,match='out of date'): search_index(store,'Cat')
    with pytest.raises(ValueError,match='Source media changed'): indexing_job(store)

def test_oversize_review_does_not_invent_timestamps(project,tmp_path):
    store,_,transcript,_=project
    segment=store.get_transcript(transcript)['segments'][0]
    store.update_transcript_segment(transcript,segment['id'],'a'*2001)
    queue=JobQueue(tmp_path/'long-queue',handlers={'indexing':IndexHandler()},concurrency=1)
    try:
        job=queue.submit(indexing_job(store))
        result=queue.wait(job['id'],5)
        assert result['state']=='failed'
        assert 'timing cannot be invented' in result['error']['message']
        assert not index_status(store)['available']
    finally: queue.close()

def test_status_skips_media_hash_but_worker_verifies(project,tmp_path,monkeypatch):
    import localcut_service.footage_index as module
    store,_,_,source=project
    build(tmp_path,store)
    original=module._sha256_file
    calls=[]
    def observed(path,check_cancelled=None):
        calls.append(Path(path).resolve())
        return original(path,check_cancelled)
    from pathlib import Path
    monkeypatch.setattr(module,'_sha256_file',observed)
    assert index_status(store)['available']
    assert source.resolve() not in calls
    job=indexing_job(store,force=True)
    assert source.resolve() not in calls
    class Context:
        def check_cancelled(self): pass
    IndexHandler().validate_job(job,Context())
    assert source.resolve() in calls


def test_vector_extreme_finite_values_normalize_safely():
    from localcut_service.footage_index import _vector
    vector=_vector([1e308,1e308])
    assert vector==pytest.approx([2**-.5,2**-.5])
    with pytest.raises(ValueError): _vector([1.7e308,1.7e308])

def test_index_job_cannot_claim_different_generator_or_model_provenance(project):
    store,_,_,_=project
    job=indexing_job(store)
    with pytest.raises(ValueError,match='provenance'):
        IndexHandler().validate_job(replace(job,generator_id='untrusted filename'))
    with pytest.raises(ValueError,match='must not claim'):
        IndexHandler().validate_job(replace(job,model_id='fake',model_version='fake'))

def test_unavailable_semantic_provider_preserves_exact_search(project,tmp_path):
    from localcut_service.model_runtime import RuntimeFailure
    store,_,_,_=project
    build(tmp_path,store,Runtime(),True)
    class Offline(Runtime):
        def select(self,*args,**kwargs):
            raise RuntimeFailure('provider_unavailable','Start the configured local provider.')
    assert search_index(store,'Cat')['results']
    with pytest.raises(RuntimeFailure) as error:
        search_index(store,'cat',mode='semantic',runtime=Offline())
    assert error.value.code=='provider_unavailable'

def test_detector_algorithm_upgrade_invalidates_index(project,tmp_path,monkeypatch):
    store,_,_,_=project
    build(tmp_path,store)
    monkeypatch.setitem(DETECTORS['silence'],'version','next-version')
    assert index_status(store)['stale']

def test_vector_memory_limit_does_not_replace_previous_index(project,tmp_path,monkeypatch):
    from localcut_service import footage_index as module
    store,_,_,_=project
    build(tmp_path,store)
    prior=index_status(store)['artifactId']
    monkeypatch.setattr(module,'MAX_VECTOR_MEMORY_BYTES',1)
    queue=JobQueue(tmp_path/'memory-queue',handlers={'indexing':IndexHandler(Runtime())},concurrency=1)
    try:
        job=queue.submit(indexing_job(store,Runtime(),semantic=True))
        failed=queue.wait(job['id'],5)
        assert failed['state']=='failed'
        assert 'memory budget' in failed['error']['message']
        assert index_status(store)['artifactId']==prior
    finally:
        queue.close()

def test_malformed_quality_filter_is_actionable(project,tmp_path):
    store,_,_,_=project
    build(tmp_path,store)
    with pytest.raises(ValueError,match='Quality flags'):
        search_index(store,'lesson',filters={'qualityFlags':[{'instruction':'run this'}]})

def test_word_only_phrase_and_reviewed_words_keep_source_bounds(project,tmp_path):
    store,asset,_,source=project
    transcript=store.add_transcript(asset_id=asset,language='en',words=[{'text':'Hello','sourceInTicks':100,'sourceOutTicks':200},{'text':'world','sourceInTicks':300,'sourceOutTicks':400},{'text':'Later','sourceInTicks':2_000_000,'sourceOutTicks':2_100_000}],generator_id='fixture',generator_version='1',source_fingerprint=_sha256_file(source))
    word=store.get_transcript(transcript)['words'][1]
    store.update_transcript_word(transcript,word['id'],'universe')
    build(tmp_path,store)
    result=search_index(store,'Hello universe')['results'][0]
    assert (result['sourceInTicks'],result['sourceOutTicks'])==(100,400)
    assert len(search_index(store,'Later')['results'])==1


def test_segment_review_precedence_and_uncovered_words(project,tmp_path):
    store,asset,_,source=project
    transcript=store.add_transcript(asset_id=asset,language='en',segments=[{'text':'Hello world','sourceInTicks':100,'sourceOutTicks':1000}],words=[{'text':'Hello','sourceInTicks':100,'sourceOutTicks':200},{'text':'world','sourceInTicks':300,'sourceOutTicks':400},{'text':'Uncovered','sourceInTicks':2_000_000,'sourceOutTicks':2_100_000}],generator_id='fixture',generator_version='1',source_fingerprint=_sha256_file(source))
    word=store.get_transcript(transcript)['words'][1]
    store.update_transcript_word(transcript,word['id'],'universe')
    build(tmp_path,store)
    result=search_index(store,'Hello universe')['results'][0]
    assert result['sourceOutTicks']==1000
    assert search_index(store,'Uncovered')['results']
    segment=store.get_transcript(transcript)['segments'][0]
    store.update_transcript_segment(transcript,segment['id'],'Explicit reviewed segment')
    build(tmp_path,store)
    assert search_index(store,'Explicit reviewed segment')['results']
    assert not search_index(store,'Hello universe')['results']


def test_source_duration_and_initial_shot(project,tmp_path):
    from localcut_service.footage_index import _chunks
    store,asset,_,source=project
    path=store.root/'cache'/'shots.json'; path.write_text('{}')
    store.register_artifact(asset_id=asset,kind='analysis:shot_boundaries',relative_path='cache/shots.json',source_fingerprint=_sha256_file(source),generator_id='shots',generator_version=DETECTORS['shot_boundaries']['version'],checksum_sha256=_sha256_file(path))
    build(tmp_path,store)
    assert search_index(store,'Cat',filters={'shotId':asset+':shot:initial'})['results']
    data={'assets':[{'id':'a','duration_ticks':100}], 'transcripts':[{'assetId':'a','id':'t','words':[],'segments':[{'id':'s','text':'bad timestamp','sourceInTicks':90,'sourceOutTicks':110}]}],'analyses':[],'detectorCoverage':{}}
    with pytest.raises(ValueError,match='beyond the known source duration'): _chunks(data)
