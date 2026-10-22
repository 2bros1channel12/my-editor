import hashlib
import json
import shutil
import subprocess
import threading

import pytest

from localcut_service.finishing import render_options, finish_audio
from localcut_service.jobs import JobQueue
from localcut_service.media import FFprobe, import_media
from localcut_service.persistence import ProjectStore
from localcut_service.render import RenderHandler, render_job


@pytest.mark.parametrize('options', [dict(frameRate='30'), dict(speechGainDb=float('nan')), dict(noiseReduction='yes'), dict(filter='movie=secret'), dict(resolution=True), dict(safeArea=.4)])
def test_rejects_unstructured_or_unsafe_settings(options):
    with pytest.raises(ValueError): render_options(options)


def test_preview_and_peak_protection():
    options = render_options(dict(mode='preview', normalize=True, loudnessLufs=-18))
    assert options['resolution'] == 720
    assert 'loudnorm=I=-18:TP=-1.5' in finish_audio('[mix]', options)
    assert 'level=0:latency=1' in finish_audio('[mix]', options)


def test_cancel_render_discards_partial_output(tmp_path):
    from test_render import _project, FixtureRenderHandler
    from localcut_service.render import list_exports
    entered, release = threading.Event(), threading.Event()
    class PartialProcess:
        def run(self,args,context,**kwargs):
            if '-filter_complex' in args:
                from pathlib import Path
                Path(args[-1]).write_bytes(b'partial')
                entered.set()
                assert release.wait(5)
                context.check_cancelled()
            return b''
    store,_,_,ffmpeg,ffprobe = _project(tmp_path)
    queue = JobQueue(tmp_path/'jobs',handlers={'rendering':FixtureRenderHandler(str(ffmpeg),str(ffprobe),process=PartialProcess())},concurrency=1)
    try:
        job=queue.submit(render_job(store,str(ffmpeg),str(ffprobe)))
        assert entered.wait(5)
        queue.cancel(job['id']); release.set()
        assert queue.wait(job['id'],5)['state']=='cancelled'
        assert list_exports(store)==[]
        assert not list((store.root/'exports').glob('*.mp4'))
    finally: release.set(); queue.close()


def test_music_rights_are_persistent_and_undoable(tmp_path):
    from test_render import _project
    from localcut_service.timeline import timeline_state, timeline_command, timeline_undo
    store,*_= _project(tmp_path)
    doc=timeline_state(store)
    saved=timeline_command(store,dict(type='music_rights',clipId='bed-1',rights='Licensed by user',attribution='Music: Example'),doc['revision'])
    reopened=timeline_state(ProjectStore.open(store.root))
    assert next(c for c in reopened['clips'] if c['id']=='bed-1')['properties']['attribution']=='Music: Example'
    undone=timeline_undo(store,saved['revision'])
    assert 'rights' not in next(c for c in undone['clips'] if c['id']=='bed-1')['properties']


def test_disk_exhaustion_fails_without_publishing(tmp_path,monkeypatch):
    from test_render import _project, FixtureRenderHandler, RecordingProcess
    from types import SimpleNamespace
    from localcut_service.render import list_exports
    store,_,_,ffmpeg,ffprobe=_project(tmp_path)
    monkeypatch.setattr('localcut_service.render.shutil.disk_usage',lambda _:SimpleNamespace(free=0))
    queue=JobQueue(tmp_path/'jobs',handlers={'rendering':FixtureRenderHandler(str(ffmpeg),str(ffprobe),process=RecordingProcess())},concurrency=1)
    try:
        result=queue.wait(queue.submit(render_job(store,str(ffmpeg),str(ffprobe)))['id'],5)
        assert result['state']=='failed' and 'disk space' in result['error']['message']
        assert list_exports(store)==[]
    finally: queue.close()


def test_real_multiclip_gap_finishing_and_source_preservation(tmp_path):
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe: pytest.skip('Local FFmpeg tools unavailable')
    source = tmp_path / 'source [雪] spaces.mp4'
    subprocess.run([ffmpeg,'-v','error','-f','lavfi','-i','testsrc2=size=320x180:rate=24:duration=2','-f','lavfi','-i','sine=frequency=440:duration=2','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-shortest',str(source)],check=True,capture_output=True,timeout=30)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    store = ProjectStore.create(tmp_path/'project', 'Finish')
    imported = import_media(store, [str(source)], probe=FFprobe(ffprobe))
    # Import response includes per-file records; use the persisted stable asset ID.
    asset = store.load_state()['assets'][0]['id']
    clips = [dict(id=f'clip-{i}',assetId=asset,itemType='clip',trackId='video',timelineStartTicks=i*1_500_000,timelineDurationTicks=1_000_000,sourceInTicks=i*1_000_000,sourceOutTicks=(i+1)*1_000_000,properties={'linkedAudio': i == 0}) for i in range(2)]
    store.autosave(store.project_id, {'timelineItems':clips})
    queue = JobQueue(tmp_path/'jobs',handlers={'rendering':RenderHandler(ffmpeg,ffprobe)},concurrency=1)
    try:
        request = render_job(store,ffmpeg,ffprobe,options=dict(mode='preview',resolution=480,frameRate=24,normalize=True,noiseReduction=True))
        job = queue.submit(request)
        result = queue.wait(job['id'],120)
        assert result['state'] == 'completed', result.get('error')
        output = store.root/'exports'/f"{request.config['exportId']}.mp4"
        probe = json.loads(subprocess.run([ffprobe,'-v','error','-show_streams','-show_format','-of','json',str(output)],check=True,capture_output=True,timeout=30).stdout)
        assert float(probe['format']['duration']) == pytest.approx(2.5,abs=1/24)
        video = next(s for s in probe['streams'] if s['codec_type']=='video')
        assert video['height']==480 and video['avg_frame_rate']=='24/1'
        assert hashlib.sha256(source.read_bytes()).hexdigest()==digest
        from localcut_service.server import dispatch
        from localcut_service.config import AppConfig
        class Preview:
            def register(self,path): return 'http://127.0.0.1/ticket'
        params={'path':str(store.root),'previewExportId':request.config['exportId']}
        listed=dispatch('export_list',params,AppConfig(data_dir=tmp_path/'data'),preview=Preview())
        assert listed['exports'][0]['playbackUrl']=='http://127.0.0.1/ticket'
        output.write_bytes(b'corrupt')
        with pytest.raises(ValueError,match='corrupt'):
            dispatch('export_list',params,AppConfig(data_dir=tmp_path/'data'),preview=Preview())
    finally: queue.close()
