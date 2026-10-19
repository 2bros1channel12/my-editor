import io
import json
from pathlib import Path
import threading
import wave
import urllib.request
import pytest
from localcut_service import transcription as asr
from localcut_service.jobs import JobQueue, JobInput, JobCancelled
from localcut_service.persistence import ProjectStore, _connect, _sha256_file
from localcut_service.preview import PreviewServer

@pytest.fixture
def fixture(tmp_path,monkeypatch):
    store=ProjectStore.create(tmp_path/'project','Speech')
    source=tmp_path/'source unusual 雪.mov'; source.write_bytes(b'original')
    asset=store.add_asset(source)
    bundle=store.root/'cache'/'proxy';bundle.mkdir(parents=True)
    with wave.open(str(bundle/'analysis.wav'),'wb') as wav:
        wav.setparams((1,2,16000,0,'NONE','not compressed'));wav.writeframes(b'\0\0'*16000)
    (bundle/'mapping.json').write_text(json.dumps({'sourceStartSeconds':'0','sourceMinusProxySeconds':'0'}))
    (bundle/'manifest.json').write_text(json.dumps({'pipelineVersion':'preview-2','mapping':'mapping.json','analysis':{'path':'analysis.wav','sourceStartSeconds':'2.5'}}))
    store.register_artifact(asset_id=asset,kind='proxy',relative_path='cache/proxy',source_fingerprint=_sha256_file(source),generator_id='fixture',generator_version='1',checksum_sha256=_sha256_file(bundle))
    model=asr.model_file(store,'tiny');model.parent.mkdir(parents=True);model.write_bytes(b'model')
    monkeypatch.setitem(asr.CATALOG,'tiny',(5,_sha256_file(model),0))
    import localcut_service.capabilities as cap
    monkeypatch.setattr(cap,'collect_capabilities',lambda *a,**k:{'memory':{'availableBytes':8*1024**3}})
    return store,asset

class Backend:
    name='fixture-whisper';version='fixture-1'
    def transcribe(self,audio,model,output,context,language):
        assert audio.name=='analysis.wav'
        return {'language':'en','segments':[{'text':'Hello world','startSeconds':0,'endSeconds':1,'words':[{'text':'Hello','startSeconds':0,'endSeconds':.4,'confidence':.9},{'text':'world','startSeconds':.5,'endSeconds':1}]}]}

def test_commit_dedupe_edit_marker_reopen(fixture,tmp_path):
    store,asset=fixture; backend=Backend()
    with_queue=JobQueue(tmp_path/'app',handlers={'transcription':asr.TranscriptionHandler(backend)},concurrency=1)
    try:
        job=asr.transcription_job(store,asset,'tiny',backend)
        submitted=with_queue.submit(job); result=with_queue.wait(submitted['id'])
        assert result['state']=='completed',result
        transcript=store.list_transcripts(asset)[0]
        assert transcript['language']=='en'
        assert transcript['words'][0]['sourceInTicks']==2500000
        assert transcript['words'][1]['confidence'] is None
        assert transcript['words'][0]['speakerOrdinal'] is None
        assert transcript['artifactId']==result['output']['artifactId']
        assert with_queue.submit(job)['deduplicated']
        changed=store.update_transcript_segment(transcript['id'],transcript['segments'][0]['id'],'Corrected')
        assert changed['segments'][0]['originalText']=='Hello world'
        assert changed['segments'][0]['text']=='Corrected'
        store.set_transcript_range_marker(transcript['id'],2500000,3500000,'required')
        reopened=ProjectStore.open(store.root).get_transcript(transcript['id'])
        assert reopened['markers'][0]['marker']=='required'
        assert reopened['words'][0]['originalText']=='Hello'
    finally:with_queue.close()

def test_transcript_timestamps_subtract_nonzero_video_pts_origin(fixture,tmp_path):
    store,asset=fixture; backend=Backend()
    bundle=store.root/'cache'/'proxy'
    (bundle/'mapping.json').write_text(json.dumps({'sourceStartSeconds':'9','sourceMinusProxySeconds':'37/4'}))
    manifest=json.loads((bundle/'manifest.json').read_text())
    manifest['analysis']['sourceStartSeconds']='19/2'
    (bundle/'manifest.json').write_text(json.dumps(manifest))
    # The registered fixture artifact must reflect the modified preview bundle.
    with _connect(store.database_path) as db:
        db.execute("UPDATE artifacts SET checksum_sha256=? WHERE kind='proxy'",(_sha256_file(bundle),));db.commit()
    queue=JobQueue(tmp_path/'app-origin',handlers={'transcription':asr.TranscriptionHandler(backend)},concurrency=1)
    try:
        job=queue.submit(asr.transcription_job(store,asset,'tiny',backend));done=queue.wait(job['id'])
        assert done['state']=='completed',done
        transcript=store.list_transcripts(asset)[0]
        assert transcript['words'][0]['sourceInTicks']==500000
        assert transcript['words'][0]['sourceOutTicks']==900000
    finally:queue.close()

def test_transcription_rejects_proxy_with_obsolete_timestamp_map(fixture):
    store,asset=fixture
    bundle=store.root/'cache'/'proxy'
    manifest=json.loads((bundle/'manifest.json').read_text())
    manifest['pipelineVersion']='preview-1'
    (bundle/'manifest.json').write_text(json.dumps(manifest))
    with _connect(store.database_path) as db:
        db.execute("UPDATE artifacts SET checksum_sha256=? WHERE kind='proxy'",(_sha256_file(bundle),));db.commit()
    with pytest.raises(ValueError,match='Generate preview assets first'):
        asr.ready_proxy(store,asset)

def test_bad_timestamp_never_publishes(fixture,tmp_path):
    store,asset=fixture
    class Bad(Backend):
        def transcribe(self,*args):
            result=super().transcribe(*args);result['segments'][0]['words'][0]['endSeconds']=9;return result
    backend=Bad();queue=JobQueue(tmp_path/'app',handlers={'transcription':asr.TranscriptionHandler(backend)})
    try:
        result=queue.wait(queue.submit(asr.transcription_job(store,asset,'tiny',backend))['id'])
        assert result['state']=='failed';assert store.list_transcripts()==[]
        assert not list((store.root/'cache'/'transcripts').glob('*'))
    finally:queue.close()

def test_cancel_no_inference_artifact(fixture,tmp_path):
    store,asset=fixture;entered=threading.Event()
    class Slow(Backend):
        def transcribe(self,*args):
            entered.set();context=args[3]
            while True:context.check_cancelled()
    backend=Slow();queue=JobQueue(tmp_path/'app',handlers={'transcription':asr.TranscriptionHandler(backend)})
    try:
        submitted=queue.submit(asr.transcription_job(store,asset,'tiny',backend));assert entered.wait(4)
        queue.cancel(submitted['id']);assert queue.wait(submitted['id'])['state']=='cancelled'
        assert store.list_transcripts()==[]
    finally:queue.close()

def test_full_json_token_fixture():
    raw={'result':{'language':'en'},'transcription':[{'text':' Hello world','offsets':{'from':0,'to':1000},'tokens':[{'text':' Hello','id':123,'p':.9,'offsets':{'from':0,'to':400}},{'text':' world','id':234,'p':.8,'offsets':{'from':500,'to':1000}}]}]}
    result=asr.normalize_whisper(raw)
    assert [w['text'] for w in result['segments'][0]['words']]==['Hello','world']
    assert result['segments'][0]['words'][1]['endSeconds']==1
    raw['transcription'][0]['tokens']=[]
    with pytest.raises(ValueError,match='without word'):asr.normalize_whisper(raw)

def test_hardware_selection():
    assert asr.choose_model({})=='tiny'
    assert asr.choose_model({'memory':{'availableBytes':6*1024**3}})=='base'
    assert asr.choose_model({'memory':{'availableBytes':9*1024**3}})=='small'

def test_download_integrity_and_cancellation(tmp_path,monkeypatch):
    store=ProjectStore.create(tmp_path/'p','P');payload=b'model';digest=__import__('hashlib').sha256(payload).hexdigest()
    monkeypatch.setitem(asr.CATALOG,'tiny',(len(payload),digest,0))
    monkeypatch.setattr(asr.urllib.request,'urlopen',lambda *a,**k:io.BytesIO(payload))
    queue=JobQueue(tmp_path/'app',handlers={'model_download':asr.ModelDownloadHandler()})
    try:
        job=JobInput(str(store.root),'model_download',{'model':'tiny'},digest)
        assert queue.wait(queue.submit(job)['id'])['state']=='completed'
        assert asr.model_status(store,'tiny')['ready']
        asr.model_file(store,'tiny').write_bytes(b'WRONG')
        assert not asr.model_status(store,'tiny')['ready']
    finally:queue.close()

def test_ticketed_preview_ranges(tmp_path):
    path=tmp_path/'preview.mp4';path.write_bytes(b'0123456789');server=PreviewServer()
    try:
        url=server.register(path)
        request=urllib.request.Request(url,headers={'Range':'bytes=2-5'})
        with urllib.request.urlopen(request) as response:
            assert response.status==206;assert response.read()==b'2345'
        with pytest.raises(__import__('urllib.error',fromlist=['HTTPError']).HTTPError):urllib.request.urlopen(url+'/../preview.mp4')
    finally:server.close()

def test_atomic_registration_rollback(fixture,tmp_path):
    store,asset=fixture
    class FailingCommit(asr.TranscriptionHandler):
        def commit_output(self,*args):
            super().commit_output(*args)
            raise ValueError('Simulated registration failure')
    backend=Backend();queue=JobQueue(tmp_path/'app',handlers={'transcription':FailingCommit(backend)})
    try:
        result=queue.wait(queue.submit(asr.transcription_job(store,asset,'tiny',backend))['id'])
        assert result['state']=='failed'
        assert store.list_transcripts()==[]
        with _connect(store.database_path) as db:assert db.execute("SELECT count(*) FROM artifacts WHERE kind='transcript'").fetchone()[0]==0
        assert not list((store.root/'cache'/'transcripts').glob('*'))
    finally:queue.close()

def test_low_memory_is_actionable(fixture,tmp_path,monkeypatch):
    store,asset=fixture
    import localcut_service.capabilities as cap
    monkeypatch.setitem(asr.CATALOG,'tiny',(5,_sha256_file(asr.model_file(store,'tiny')),2))
    monkeypatch.setattr(cap,'collect_capabilities',lambda *a,**k:{'memory':{'availableBytes':128*1024**2}})
    queue=JobQueue(tmp_path/'app',handlers={'transcription':asr.TranscriptionHandler(Backend())})
    try:
        result=queue.wait(queue.submit(asr.transcription_job(store,asset,'tiny',Backend()))['id'])
        assert result['state']=='failed'
        assert 'RAM' in result['error']['message']
        assert 'Close' in result['error']['message']
    finally:queue.close()

def test_bad_model_download_never_commits(tmp_path,monkeypatch):
    store=ProjectStore.create(tmp_path/'p','P')
    monkeypatch.setitem(asr.CATALOG,'tiny',(5,'0'*64,0))
    monkeypatch.setattr(asr.urllib.request,'urlopen',lambda *a,**k:io.BytesIO(b'WRONG'))
    queue=JobQueue(tmp_path/'app',handlers={'model_download':asr.ModelDownloadHandler()})
    try:
        result=queue.wait(queue.submit(JobInput(str(store.root),'model_download',{'model':'tiny'},'0'*64))['id'])
        assert result['state']=='failed';assert not asr.model_file(store,'tiny').exists()
        assert 'integrity' in result['error']['message']
    finally:queue.close()

def test_download_cancel_removes_partial_file(tmp_path,monkeypatch):
    store=ProjectStore.create(tmp_path/'p','P');entered=threading.Event();release=threading.Event()
    class Slow(io.BytesIO):
        def read(self,*args):entered.set();release.wait(3);return super().read(*args)
    monkeypatch.setattr(asr.urllib.request,'urlopen',lambda *a,**k:Slow(b'abcde'))
    monkeypatch.setitem(asr.CATALOG,'tiny',(5,'0'*64,0))
    queue=JobQueue(tmp_path/'app',handlers={'model_download':asr.ModelDownloadHandler()})
    try:
        job=queue.submit(JobInput(str(store.root),'model_download',{'model':'tiny'},'0'*64))
        assert entered.wait(3);queue.cancel(job['id']);release.set()
        assert queue.wait(job['id'])['state']=='cancelled'
        assert not asr.model_file(store,'tiny').exists()
        assert not list((store.root/'.localcut-staging').rglob('*.bin'))
    finally:release.set();queue.close()

def test_whisper_adapter_structured_invocation(tmp_path,monkeypatch):
    import sys
    audio=tmp_path/'audio with 雪 & spaces.wav';audio.write_bytes(b'fixture')
    model=tmp_path/'model.bin';model.write_bytes(b'fixture');prefix=tmp_path/'result'
    raw={'result':{'language':'en'},'transcription':[{'text':' Hello','offsets':{'from':0,'to':400},'tokens':[{'text':' Hello','id':123,'p':.9,'offsets':{'from':0,'to':400}}]}]}
    def run(self,args,context,**kwargs):
        assert args[args.index('-f')+1]==str(audio)
        assert '-ojf' in args and '-ng' in args
        Path(args[args.index('-of')+1]+'.json').write_text(json.dumps(raw))
    monkeypatch.setattr(asr.MediaProcess,'run',run)
    class Context:
        def report(self,*args):pass
    backend=asr.WhisperCppBackend(sys.executable)
    result=backend.transcribe(audio,model,prefix,Context(),'auto')
    assert result['segments'][0]['words'][0]['text']=='Hello'
    assert backend.version.startswith('binary-sha256:')

def test_review_markers_reject_invalid_ranges(fixture):
    store,asset=fixture
    tid=store.add_transcript(asset_id=asset,language='en',words=[],segments=[],generator_id='fixture',generator_version='1',source_fingerprint=store.load_state()['assets'][0]['sha256'])
    with pytest.raises(ValueError):store.set_transcript_range_marker(tid,10,9,'required')
    with pytest.raises(ValueError):store.set_transcript_range_marker(tid,0,10,'invented')


def test_v2_migration_backfills_review_segments(tmp_path):
    import sqlite3
    import localcut_service.persistence as persistence
    db=sqlite3.connect(tmp_path/'v2.sqlite');db.row_factory=sqlite3.Row
    try:
        persistence._migration_1(db);persistence._migration_2(db)
        db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES ('p','P','now','now')")
        db.execute("INSERT INTO assets(id,project_id,original_uri,path_mode,last_known_path,size_bytes,mtime_ns,sha256,metadata_json,created_at) VALUES ('a','p','x','file-uri','x',1,1,'sha','{}','now')")
        db.execute("INSERT INTO transcripts(id,project_id,asset_id,language,status,generator_id,generator_version,source_fingerprint,created_at) VALUES ('t','p','a','en','ready','fixture','1','sha','now')")
        db.execute("INSERT INTO transcript_words(id,transcript_id,ordinal,source_in_ticks,source_out_ticks,text) VALUES ('w','t',0,0,1000000,'Hello')")
        db.execute('PRAGMA user_version=2');db.commit();persistence.migrate_database(db)
        assert db.execute('SELECT original_text FROM transcript_segments').fetchone()[0]=='Hello'
        assert db.execute('SELECT segment_id FROM transcript_words').fetchone()[0] is not None
    finally:db.close()

def test_zero_length_native_word_is_explicitly_uncertain():
    result=asr.normalize_whisper({'result':{'language':'en'},'transcription':[{'text':' end','offsets':{'from':0,'to':1000},'tokens':[{'text':' end','id':123,'p':.8,'offsets':{'from':1000,'to':1000}}]}]})
    word=result['segments'][0]['words'][0]
    assert word['timingUncertain']
    assert word['confidence'] == .8
    assert word['inferenceStartSeconds']==word['inferenceEndSeconds']==1
    assert 0<=word['startSeconds']<word['endSeconds']<=1
