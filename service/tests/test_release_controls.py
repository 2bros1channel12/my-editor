from pathlib import Path
import sqlite3

import pytest

from localcut_service.persistence import ProjectStore, ProjectCatalog, ProjectError, _connect
from localcut_service.release_controls import privacy_status, storage_review, storage_remove
from localcut_service.transcription import model_status, REVISION


def project(tmp_path):
    return ProjectStore.create(tmp_path/'project','Privacy')


def test_status_has_no_telemetry_and_discloses_network_and_model_integrity(tmp_path):
    store=project(tmp_path)
    status=privacy_status(store)
    assert status['telemetryEnabled'] is False and status['mediaUploadEnabled'] is False
    assert status['network']['activeDownloads']==[]
    assert 'Explicit model downloads' in status['network']['policy']
    model=status['models'][0]
    assert model['sourceUrl'].startswith('https://huggingface.co/') and len(model['sha256'])==64
    assert model['integrity']=='missing_or_invalid'


def test_model_removal_requires_review_and_preserves_other_data(tmp_path):
    store=project(tmp_path)
    model=store.root/'models'/'tiny'/REVISION/'model.bin'
    model.parent.mkdir(parents=True); model.write_bytes(b'invalid weights')
    keep=store.root/'exports'/'keep.mp4'; keep.write_bytes(b'keep')
    review=storage_review(store,'model','tiny')
    assert review['bytes']==len(b'invalid weights')
    with pytest.raises(ProjectError,match='changed'):
        storage_remove(store,'model','wrong','tiny')
    result=storage_remove(store,'model',review['confirmationToken'],'tiny')
    assert result['removed'] and not model.exists() and keep.read_bytes()==b'keep'
    assert not model_status(store,'tiny')['ready']


def test_stale_review_and_active_jobs_block_removal(tmp_path):
    store=project(tmp_path)
    review=storage_review(store,'project')
    (store.root/'changed.txt').write_text('new')
    with pytest.raises(ProjectError,match='changed'): storage_remove(store,'project',review['confirmationToken'])
    # Use normal persistent queue storage shape with a queued job.
    from localcut_service.jobs import JobQueue, JobInput
    queue=JobQueue(tmp_path/'jobs',concurrency=1,autostart=False)
    try:
        queue.submit(JobInput(str(store.root),'proxy',{'assetId':'fixture'},'fixture'))
        assert not storage_review(store,'project')['canRemove']
    finally: queue.close()


def test_complete_project_delete_preserves_external_original_and_forgets_last(tmp_path):
    store=project(tmp_path)
    original=tmp_path/'original.mp4'; original.write_bytes(b'original')
    store.add_asset(original)
    catalog=ProjectCatalog(tmp_path/'app'); catalog.remember(store.root)
    managed=store.root/'media'/'managed.mp4'; managed.write_bytes(b'copy')
    review=storage_review(store,'project')
    result=storage_remove(store,'project',review['confirmationToken'],catalog=catalog)
    assert result['projectDeleted'] and not store.root.exists()
    assert original.read_bytes()==b'original' and catalog.reopen_last() is None


def test_project_delete_refuses_original_linked_inside_project(tmp_path):
    store=project(tmp_path)
    original=store.root/'my-original.mp4'; original.write_bytes(b'original')
    store.add_asset(original)
    with pytest.raises(ProjectError,match='linked original'): storage_review(store,'project')
    assert original.read_bytes()==b'original'


def test_managed_copy_ownership_is_durable_and_cleared_on_relink(tmp_path):
    import json
    store=project(tmp_path)
    original=tmp_path/'original.mp4'; original.write_bytes(b'original')
    result=store.import_asset_managed(original,{},probe_tool='fixture',probe_version='1',duration_ticks=1000000)
    with _connect(store.database_path) as db:
        row=db.execute('SELECT * FROM assets WHERE id=?',(result['assetId'],)).fetchone()
        metadata=json.loads(row['metadata_json'])
    assert metadata['localcutManagedCopy'] is True and metadata['localcutManagedOriginalUri']==original.as_uri()
    assert storage_review(store,'project')['canRemove']
    store.relink_asset(result['assetId'],original,metadata={'localcutManagedCopy':True},duration_ticks=1000000)
    with _connect(store.database_path) as db:
        metadata=json.loads(db.execute('SELECT metadata_json FROM assets WHERE id=?',(result['assetId'],)).fetchone()[0])
    assert 'localcutManagedCopy' not in metadata
    # Untrusted probe metadata cannot claim ownership of an original.
    linked=store.root/'linked.mp4'; linked.write_bytes(b'unique')
    store.add_asset(linked,{'localcutManagedCopy':True})
    with pytest.raises(ProjectError,match='linked original'): storage_review(store,'project')


def test_owned_managed_project_can_be_completely_deleted(tmp_path):
    store=project(tmp_path)
    original=tmp_path/'original.mp4'; original.write_bytes(b'original')
    store.import_asset_managed(original,{},probe_tool='fixture',probe_version='1',duration_ticks=1000000)
    review=storage_review(store,'project')
    assert storage_remove(store,'project',review['confirmationToken'])['projectDeleted']
    assert original.read_bytes()==b'original'


def test_forget_project_removes_terminal_central_job_snapshots(tmp_path):
    from localcut_service.jobs import JobQueue,JobInput
    store=project(tmp_path)
    queue=JobQueue(tmp_path/'jobs',autostart=False)
    try:
        job=queue.submit(JobInput(str(store.root),'proxy',{'assetId':'fixture'},'private-snapshot'))
        with pytest.raises(ValueError,match='active'): queue.forget_project(store.project_id)
        queue.cancel(job['id'])
        assert queue.forget_project(store.project_id)=={'forgottenJobs':1}
        with _connect(queue.database_path) as db:
            assert db.execute('SELECT COUNT(*) FROM queue_jobs').fetchone()[0]==0
            assert db.execute('SELECT COUNT(*) FROM queue_dependencies').fetchone()[0]==0
    finally: queue.close()


def test_storage_refuses_reparse_or_symlink_before_any_deletion(tmp_path,monkeypatch):
    store=project(tmp_path)
    original=store.root/'unexpected-link'; original.write_bytes(b'keep')
    real=Path.is_symlink
    monkeypatch.setattr(Path,'is_symlink',lambda self:self==original or real(self))
    with pytest.raises(ProjectError,match='symbolic link'): storage_review(store,'project')
    assert original.read_bytes()==b'keep' and store.root.exists()


@pytest.mark.parametrize('upgrade',[False,True])
def test_failed_ownership_mark_keeps_registered_media_copy(tmp_path,monkeypatch,upgrade):
    store=project(tmp_path)
    source=tmp_path/'source.mp4'; source.write_bytes(b'original')
    if upgrade: store.add_asset(source)
    def fail(*args): raise OSError('simulated metadata write failure')
    monkeypatch.setattr(ProjectStore,'_mark_managed_copy',fail)
    with pytest.raises(OSError,match='simulated'):
        store.import_asset_managed(source,{},probe_tool='fixture',probe_version='1',duration_ticks=1000000)
    state=store.load_state()
    assert len(state['assets'])==1
    copy=store.root/state['assets'][0]['original_uri']
    assert copy.is_file() and copy.read_bytes()==source.read_bytes()==b'original'


def test_deletion_dispatch_purges_queue_and_revokes_preview(tmp_path):
    from localcut_service.jobs import JobQueue,JobInput
    from localcut_service.server import dispatch
    from localcut_service.config import AppConfig
    from localcut_service.preview import PreviewServer
    store=project(tmp_path)
    config=AppConfig(data_dir=tmp_path/'app')
    ProjectCatalog(config.data_dir).remember(store.root)
    queue=JobQueue(config.data_dir,autostart=False)
    preview=PreviewServer()
    project_id=store.project_id
    try:
        job=queue.submit(JobInput(str(store.root),'proxy',{'assetId':'fixture'},'private-snapshot'))
        queue.cancel(job['id'])
        file=store.root/'exports'/'render.mp4'; file.write_bytes(b'render')
        preview.register(file)
        reviewed=dispatch('storage_review',{'path':str(store.root),'scope':'project'},config,queue,preview)
        result=dispatch('storage_remove',{'path':str(store.root),'scope':'project','confirmationToken':reviewed['confirmationToken']},config,queue,preview)
        assert result['projectDeleted'] and not store.root.exists()
        assert queue.list_jobs(project_id)==[] and preview.files=={}
        assert ProjectCatalog(config.data_dir).reopen_last() is None
    finally: preview.close(); queue.close()


def test_transcript_purge_removes_inference_index_and_recovery_but_keeps_edits(tmp_path):
    store=project(tmp_path)
    original=tmp_path/'original.mp4'; original.write_bytes(b'original')
    asset=store.add_asset(original)
    with _connect(store.database_path) as db: digest=db.execute('SELECT sha256 FROM assets WHERE id=?',(asset,)).fetchone()[0]
    store.add_transcript(asset_id=asset,language='en',words=[{'text':'private text','sourceInTicks':0,'sourceOutTicks':1000000}],generator_id='fixture',generator_version='1',source_fingerprint=digest)
    for folder in ('cache/transcripts','cache/footage-index'):
        path=store.root/folder; path.mkdir(parents=True,exist_ok=True); (path/'data').write_bytes(b'private')
    keep=store.root/'exports'/'keep.srt'; keep.write_text('user accepted text')
    review=storage_review(store,'transcripts')
    storage_remove(store,'transcripts',review['confirmationToken'])
    with _connect(store.database_path) as db:
        assert db.execute('SELECT COUNT(*) FROM transcripts').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM transcript_words').fetchone()[0]==0
    assert not (store.root/'cache/transcripts').exists()
    for checkpoint in (store.root/'recovery').glob('*.sqlite3'):
        with sqlite3.connect(checkpoint) as db:
            assert db.execute('SELECT COUNT(*) FROM transcripts').fetchone()[0]==0
    assert keep.read_text()=='user accepted text' and original.read_bytes()==b'original'
