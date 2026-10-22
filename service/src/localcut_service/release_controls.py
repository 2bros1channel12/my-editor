"""Explicit, reviewed local storage controls. Never follow linked source paths."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import stat
import uuid
from urllib.parse import urlsplit
from urllib.request import url2pathname

from .persistence import ProjectError, _connect, _json
from .transcription import CATALOG, REVISION, model_status, VERIFIED_FILES
from .storage_lock import STORAGE_LOCK


def _check_path(path):
    if path.is_symlink() or (path.exists() and getattr(path.stat(follow_symlinks=False), 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024)):
        raise ProjectError('Storage contains a symbolic link or junction. Remove it manually before using storage controls.')


def _inventory(path):
    _check_path(path)
    if not path.exists(): return []
    if path.is_file(): return [(path, path.stat().st_size, path.stat().st_mtime_ns)]
    rows=[]
    for child in sorted(path.iterdir()): rows.extend(_inventory(child))
    return rows


def _active(store):
    with _connect(store.database_path) as db:
        return [dict(r) for r in db.execute("SELECT id,kind,state,stage,progress FROM jobs WHERE project_id=? AND state IN ('queued','running','retrying')", (store.project_id,))]


def _targets(store, scope, model):
    root=store.root.resolve()
    if root.parent == root or root == Path.home().resolve(): raise ProjectError('Refusing storage cleanup of a system or home directory.')
    if scope == 'model':
        if model not in CATALOG: raise ValueError('Choose a catalog model.')
        paths=[root/'models'/model]
    elif scope == 'transcripts': paths=[root/'cache'/'transcripts',root/'cache'/'footage-index',root/'recovery']
    elif scope == 'project':
        # A source linked from within the project folder is still an original,
        # not a managed copy, and must never be deleted by this control.
        with _connect(store.database_path) as db:
            for row in db.execute('SELECT path_mode,original_uri,last_known_path,metadata_json FROM assets WHERE project_id=?',(store.project_id,)):
                source=(root/row['original_uri'] if row['path_mode']=='project-relative' else Path(row['last_known_path'])).resolve()
                metadata=json.loads(row['metadata_json'])
                managed=metadata.get('localcutManagedCopy') is True and (root/'media').resolve() in source.parents and row['path_mode']=='project-relative'
                original_uri=metadata.get('localcutManagedOriginalUri')
                if managed and isinstance(original_uri,str):
                    parsed=urlsplit(original_uri)
                    if parsed.scheme=='file' and not parsed.netloc:
                        original=Path(url2pathname(parsed.path)).resolve()
                        if original.exists() and (original==root or root in original.parents):
                            raise ProjectError('The original used to create a managed copy remains inside this project folder. Move it outside the project before deletion.')
                if (source == root or root in source.parents) and not managed:
                    raise ProjectError('A linked original or legacy source with unverified managed storage is inside this project folder. Move and relink it outside the project before deleting the project.')
        paths=[root]
    else: raise ValueError('Choose model, transcripts, or project storage.')
    for path in paths:
        path.resolve().relative_to(root)
        current=path
        while current != root:
            _check_path(current); current=current.parent
        _check_path(root)
    return paths


def storage_review(store, scope, model=None):
    paths=_targets(store,scope,model)
    active=_active(store)
    rows=[row for path in paths for row in _inventory(path)]
    with _connect(store.database_path) as db:
        revision=db.execute('SELECT state_revision FROM projects WHERE id=?',(store.project_id,)).fetchone()[0]
        transcript_count=db.execute('SELECT COUNT(*) FROM transcripts WHERE project_id=?',(store.project_id,)).fetchone()[0]
    # Opening a project updates SQLite last_opened_at. Bind to logical edit revision
    # rather than database mtime so a fresh dispatch can honor a reviewed token.
    signature=[(str(p.relative_to(store.root)),size,mtime) for p,size,mtime in rows if p not in {store.database_path,Path(str(store.database_path)+'-wal'),Path(str(store.database_path)+'-shm')}]
    payload=dict(projectId=store.project_id,projectPath=str(store.root.resolve()),revision=revision,scope=scope,model=model,files=signature,transcriptCount=transcript_count)
    token=hashlib.sha256(_json(payload).encode()).hexdigest()
    detail={'model':'Remove downloaded weights and invalid copies for this catalog model. Existing transcripts remain.',
            'transcripts':'Remove inferred transcripts, search index files and recovery snapshots. Editable captions, briefs, plans, exports and timeline history remain; this is not secure erasure.',
            'project':'Delete this entire project folder, managed copies, models, caches, transcripts, exports and recovery data. External linked original media is preserved.'}[scope]
    return dict(scope=scope,model=model,confirmationToken=token,activeJobs=active,canRemove=not active,bytes=sum(s for _,s,_ in rows),files=len(rows),detail=detail)


def storage_remove(store, scope, confirmation_token, model=None, catalog=None):
    with STORAGE_LOCK:
        return _storage_remove(store,scope,confirmation_token,model,catalog)


def _storage_remove(store, scope, confirmation_token, model=None, catalog=None):
    reviewed=storage_review(store,scope,model)
    if reviewed['activeJobs']: raise ProjectError('Wait for queued/running jobs to finish or cancel them before removing storage.')
    if not isinstance(confirmation_token,str) or confirmation_token != reviewed['confirmationToken']:
        raise ProjectError('Storage changed after review. Refresh the storage review before confirming removal.')
    paths=_targets(store,scope,model)
    if scope == 'project':
        project_id=store.project_id
        root=store.root.resolve()
        # Rename before removing. An interrupted deletion cannot leave a project
        # that still appears openable with only some of its data present.
        quarantine=root.with_name(root.name+'.deleting-'+uuid.uuid4().hex)
        with _connect(store.database_path) as db:
            db.execute('BEGIN IMMEDIATE')
            fresh=storage_review(store,scope,model)
            if fresh['activeJobs'] or fresh['confirmationToken']!=confirmation_token:
                raise ProjectError('Storage or active jobs changed after review. Refresh before deleting.')
            db.rollback()
        # Windows does not allow renaming a directory containing an open SQLite
        # connection. STORAGE_LOCK also guards JobQueue.submit; release the DB
        # handle only after active-job and token checks, then rename atomically.
        root.rename(quarantine)
        if catalog is not None and catalog.pointer_path.exists():
            try: pointer=json.loads(catalog.pointer_path.read_text(encoding='utf-8'))
            except (ValueError,OSError): pointer={}
            if pointer.get('projectId') == project_id: catalog.pointer_path.unlink(missing_ok=True)
        try: shutil.rmtree(quarantine)
        except OSError as exc:
            raise ProjectError(f'Project closed, but deletion was interrupted. Finish removing this exact folder: {quarantine}') from exc
        return dict(removed=True,scope=scope,bytes=reviewed['bytes'],projectDeleted=True)
    with _connect(store.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        fresh=storage_review(store,scope,model)
        if fresh['activeJobs'] or fresh['confirmationToken']!=confirmation_token:
            raise ProjectError('Storage or active jobs changed after review. Refresh before removing storage.')
        if scope == 'model':
            db.execute("UPDATE artifacts SET status='stale' WHERE project_id=? AND kind='model' AND model_id=?",(store.project_id,model))
        else:
            db.execute('PRAGMA secure_delete=ON')
            db.execute('DELETE FROM transcripts WHERE project_id=?',(store.project_id,))
            db.execute("UPDATE artifacts SET status='stale' WHERE project_id=? AND kind IN ('transcript','footage-index')",(store.project_id,))
        for path in paths:
            _inventory(path) # Recheck all symlink/junction boundaries before recursion.
            if path.is_dir(): shutil.rmtree(path)
            elif path.exists(): path.unlink()
        if scope=='transcripts': store._record_recovery_state(db)
        db.commit()
    if scope == 'model':
        for key in list(VERIFIED_FILES):
            if str(paths[0]) in key: VERIFIED_FILES.pop(key,None)
    else:
        with _connect(store.database_path) as db:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            db.execute('VACUUM')
        store.checkpoint()
    return dict(removed=True,scope=scope,bytes=reviewed['bytes'],projectDeleted=False)


def privacy_status(store,config=None):
    root=store.root
    active=_active(store)
    def size(path): return sum(s for _,s,_ in _inventory(path))
    return dict(projectId=store.project_id,telemetryEnabled=False,mediaUploadEnabled=False,
                network=dict(policy='Local processing by default. Explicit model downloads contact Hugging Face; local inference uses loopback.',activeDownloads=[r for r in active if r['kind']=='model_download'],localRuntimeUrl=getattr(config,'runtime_base_url','http://127.0.0.1:11434')),
                storage=dict(projectBytes=size(root),modelsBytes=size(root/'models'),transcriptsBytes=size(root/'cache'/'transcripts'),cacheBytes=size(root/'cache')),
                models=[model_status(store,k) for k in CATALOG])
