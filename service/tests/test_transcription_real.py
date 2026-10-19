"""Optional real CPU inference smoke test; test dependencies are supplied explicitly."""
import os
from pathlib import Path
import re
import subprocess
import time
import wave
import pytest
from localcut_service.jobs import JobQueue
from localcut_service.media import FFprobe, import_media
from localcut_service.persistence import ProjectStore, _sha256_file
from localcut_service.proxies import ProxyHandler, proxy_job
from localcut_service.transcription import WhisperCppBackend, TranscriptionHandler, transcription_job, model_file, model_status
from localcut_service.suggestions import SuggestionHandler, proposal_job
from localcut_service.analysis import AnalysisHandler, analysis_job


def test_real_short_speech_end_to_end(tmp_path):
    names=['LOCAL_CUT_TEST_WHISPER','LOCAL_CUT_TEST_WHISPER_MODEL','LOCAL_CUT_TEST_SPEECH','LOCAL_CUT_TEST_FFMPEG','LOCAL_CUT_TEST_FFPROBE']
    if not all(os.environ.get(n) for n in names):pytest.skip('Supply local Whisper, model, speech WAV, and FFmpeg fixtures')
    cli,weights,speech,ffmpeg,ffprobe=[os.environ[n] for n in names]
    source=tmp_path/'lesson space 雪 & punctuation.mp4'
    with wave.open(speech, 'rb') as reader:
        speech_duration = reader.getnframes() / reader.getframerate()
    subprocess.run([ffmpeg,'-nostdin','-v','error','-f','lavfi','-i',f'color=c=blue:s=320x180:r=25:d={speech_duration + 1}','-i',speech,'-shortest','-c:v','libx264','-c:a','aac',str(source)],check=True,capture_output=True,timeout=30)
    digest=_sha256_file(source)
    store=ProjectStore.create(tmp_path/'project','Speech integration');asset=store.add_asset(source)
    target=model_file(store,'tiny');target.parent.mkdir(parents=True)
    import shutil
    shutil.copyfile(weights,target)
    assert model_status(store,'tiny')['ready']
    backend=WhisperCppBackend(cli)
    queue=JobQueue(tmp_path/'app',handlers={'proxy':ProxyHandler(ffmpeg,ffprobe),'transcription':TranscriptionHandler(backend),'analysis':AnalysisHandler(ffmpeg),'suggestion':SuggestionHandler()},concurrency=1)
    try:
        profile={'recommendations':{'proxyPreset':'low'},'mediaTools':{'ffmpeg':{'version':'integration'}}}
        preview=queue.wait(queue.submit(proxy_job(store,asset,profile))['id'],timeout=60)
        assert preview['state']=='completed',preview
        submitted=queue.submit(transcription_job(store,asset,'tiny',backend))
        inference_started = time.monotonic()
        result=queue.wait(submitted['id'],timeout=60)
        inference_seconds = time.monotonic() - inference_started
        assert result['state']=='completed',result
        transcript=store.list_transcripts(asset)[0]
        assert transcript['language']=='en'
        assert transcript['modelId'] == 'tiny'
        assert transcript['generatorId'] == backend.name
        assert transcript['generatorVersion'] == backend.version
        assert len(transcript['words']) >= 20
        assert all(0 <= w['sourceInTicks'] < w['sourceOutTicks'] <= round((speech_duration + 0.5) * 1_000_000) for w in transcript['words'])
        normalized = ' '.join(re.findall(r'[a-z]+', ' '.join(s['text'] for s in transcript['segments']).lower()))
        assert normalized == 'and so my fellow americans ask not what your country can do for you ask what you can do for your country'
        assert _sha256_file(source)==digest
        assert queue.submit(transcription_job(store,asset,'tiny',backend))['deduplicated']
        print('Backend:', backend.name, backend.version, 'Model:', transcript['modelId'], transcript['modelVersion'])
        print('Detected:', transcript['language'], 'Words:',len(transcript['words']), 'Inference job seconds:', round(inference_seconds, 3))
        print('Text:', ' '.join(s['text'] for s in transcript['segments']))
    finally:queue.close()


def test_real_20_minute_transcription_performance(tmp_path):
    """Opt-in release-gate benchmark; uses only explicitly supplied local binaries and media."""
    if os.environ.get('LOCAL_CUT_RUN_20MIN_ASR') != '1':
        pytest.skip('Set LOCAL_CUT_RUN_20MIN_ASR=1 to run the 20-minute local ASR benchmark')
    names=['LOCAL_CUT_TEST_WHISPER','LOCAL_CUT_TEST_WHISPER_MODEL','LOCAL_CUT_TEST_SPEECH','LOCAL_CUT_TEST_FFMPEG','LOCAL_CUT_TEST_FFPROBE']
    if not all(os.environ.get(n) for n in names):pytest.skip('Supply local Whisper, model, speech WAV, and FFmpeg fixtures')
    cli,weights,speech,ffmpeg,ffprobe=[os.environ[n] for n in names]
    source=tmp_path/'20 minute 1080p tutorial.mp4'
    duration=20*60
    generation_started=time.monotonic()
    subprocess.run([ffmpeg,'-nostdin','-v','error','-f','lavfi','-i',f'color=c=blue:s=1920x1080:r=30:d={duration}','-stream_loop','-1','-i',speech,'-t',str(duration),'-c:v','libx264','-preset','ultrafast','-crf','32','-pix_fmt','yuv420p','-c:a','aac','-b:a','48k','-movflags','+faststart',str(source)],check=True,capture_output=True,timeout=300)
    source_generation_seconds=time.monotonic()-generation_started
    digest=_sha256_file(source)
    store=ProjectStore.create(tmp_path/'project','20 minute ASR performance')
    imported=import_media(store,[source],probe=FFprobe(ffprobe))
    assert imported['imported'] and not imported['errors']
    asset=imported['imported'][0]['assetId']
    target=model_file(store,'tiny');target.parent.mkdir(parents=True)
    import shutil
    shutil.copyfile(weights,target)
    backend=WhisperCppBackend(cli)
    queue=JobQueue(tmp_path/'app',handlers={'proxy':ProxyHandler(ffmpeg,ffprobe),'transcription':TranscriptionHandler(backend),'analysis':AnalysisHandler(ffmpeg),'suggestion':SuggestionHandler()},concurrency=1)
    try:
        profile={'recommendations':{'proxyPreset':'low'},'mediaTools':{'ffmpeg':{'version':'integration'}}}
        proxy_submitted=time.monotonic();proxy_job_record=queue.submit(proxy_job(store,asset,profile));proxy_job_id=proxy_job_record['id']
        while True:
            record=queue._get(proxy_job_id)
            if record and record['state']=='running':break
            if record and record['state'] in {'failed','cancelled'}:pytest.fail(f"Proxy job failed before starting: {record}")
            if time.monotonic()-proxy_submitted>10:pytest.fail('Proxy generation did not start within the 10-second MVP target.')
            time.sleep(.05)
        proxy_start_latency=time.monotonic()-proxy_submitted
        preview=queue.wait(proxy_job_id,timeout=900)
        assert preview['state']=='completed',preview
        submitted=queue.submit(transcription_job(store,asset,'tiny',backend))
        inference_started=time.monotonic();result=queue.wait(submitted['id'],timeout=900);inference_seconds=time.monotonic()-inference_started
        assert result['state']=='completed',result
        transcript=store.list_transcripts(asset)[0]
        assert transcript['language']=='en'
        assert transcript['modelId']=='tiny'
        assert transcript['words']
        assert all(0<=word['sourceInTicks']<word['sourceOutTicks']<=duration*1_000_000 for word in transcript['words'])
        assert inference_seconds<300, f'20-minute transcription took {inference_seconds:.1f}s, exceeding the 300-second MVP gate.'
        analysis_started=time.monotonic()
        for detector,settings in (('silence',{}),('filler_words',{}),('shot_boundaries',{'sampleFps':1})):
            detector_input=analysis_job(store,asset,detector,settings)
            detector_job=queue.submit(detector_input)
            detector_result=queue.wait(detector_job['id'],timeout=900)
            assert detector_result['state']=='completed',f'{detector} analysis failed: {detector_result}'
        analysis_seconds=time.monotonic()-analysis_started
        brief_id=store.add_brief('Audience: developers\nGoal: explain a technical concept\nTone: clear\nTarget duration: 12 minutes',constraints={'audience':'developers','objective':'explain a technical concept','tone':'clear','targetDurationMinutes':12,'aspectRatio':'source','mustKeep':[],'mustRemove':[],'preferredOrder':[]})
        proposal_input=proposal_job(store,asset,brief_id,transcript['id'])
        proposal_started=time.monotonic();proposal_submit=queue.submit(proposal_input);proposal=queue.wait(proposal_submit['id'],timeout=480);proposal_seconds=time.monotonic()-proposal_started
        assert proposal['state']=='completed',proposal
        assert proposal_seconds<480, f'20-minute first-cut proposal took {proposal_seconds:.1f}s, exceeding the 8-minute MVP gate.'
        edit_plans=store.list_edit_plans(brief_id)
        assert edit_plans and edit_plans[0]['status']=='pending'
        assert edit_plans[0]['plan']['operations']
        assert _sha256_file(source)==digest
        print('Hardware: Intel i9-12900K (16 cores/24 logical processors; CPU inference)')
        print('Source: 1920x1080, 30 fps, 1200 seconds; synthetic blue video with repeated local speech sample')
        print('Source generation seconds:',round(source_generation_seconds,2),'Proxy start latency seconds:',round(proxy_start_latency,3),'Transcription seconds:',round(inference_seconds,2),'Core analysis seconds:',round(analysis_seconds,2),'First-cut proposal seconds:',round(proposal_seconds,2))
        print('Reviewable proposal operations:',len(edit_plans[0]['plan']['operations']))
        print('Backend:',backend.name,backend.version,'Model: tiny',transcript['modelVersion'],'Language:',transcript['language'],'Words:',len(transcript['words']))
        print('Original source bytes unchanged:',_sha256_file(source)==digest)
    finally:queue.close()
