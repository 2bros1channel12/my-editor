from localcut_service.config import AppConfig
from localcut_service.jobs import JobInput,JobQueue
from localcut_service.persistence import ProjectStore,ProjectCatalog,_connect
from localcut_service.preview import PreviewServer
from localcut_service.server import dispatch


def test_project_delete_removes_queue_history_pointer_and_preview_tickets(tmp_path):
    config=AppConfig(data_dir=tmp_path/'app')
    store=ProjectStore.create(tmp_path/'edit.localcut','Delete test')
    original=tmp_path/'original.mp4';original.write_bytes(b'keep original')
    store.add_asset(original)
    ProjectCatalog(config.data_dir).remember(store.root)
    queue=JobQueue(config.data_dir,autostart=False)
    preview=PreviewServer()
    try:
        job=queue.submit(JobInput(str(store.root),'proxy',{'assetId':'fixture'},'private'))
        queue.cancel(job['id'])
        preview.register(store.database_path)
        review=dispatch('storage_review',{'path':str(store.root),'scope':'project'},config,queue,preview)
        result=dispatch('storage_remove',{'path':str(store.root),'scope':'project','confirmationToken':review['confirmationToken']},config,queue,preview)
        assert result['projectDeleted'] and not store.root.exists()
        assert original.read_bytes()==b'keep original'
        assert not ProjectCatalog(config.data_dir).pointer_path.exists()
        assert preview.files=={}
        with _connect(queue.database_path) as db:
            assert db.execute('SELECT COUNT(*) FROM queue_jobs').fetchone()[0]==0
    finally: preview.close();queue.close()
