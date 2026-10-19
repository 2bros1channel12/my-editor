"""Immutable local transcript search snapshots. Indexed material is data, never instructions."""
from __future__ import annotations
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import uuid
from .jobs import JobInput, JobOutput
from .proxies import source_path
from .persistence import ProjectStore, _connect, _json, _sha256_file, _now
from .analysis import DETECTORS

VERSION = 'footage-index-2'
MAX_CHUNKS = 50_000
MAX_TEXT = 2_000
MAX_VECTOR_MEMORY_BYTES = 256 * 1024 * 1024
FILTERS = {'assetId','sourceInTicks','sourceOutTicks','shotId','silence','qualityFlags'}

def _snapshot(store, check=lambda: None, *, verify_sources=False):
    with _connect(store.database_path) as db:
        source_rows = [dict(r) for r in db.execute('SELECT * FROM assets WHERE project_id=? ORDER BY id',(store.project_id,))]
        assets = [{k:r[k] for k in ('id','sha256','duration_ticks')} for r in source_rows]
        artifacts = {r['id']:dict(r) for r in db.execute("SELECT id,asset_id,kind,status,checksum_sha256,source_fingerprint,generator_version,config_json,created_at,relative_path FROM artifacts WHERE project_id=?",(store.project_id,))}
    for asset in source_rows:
        check()
        try:
            source = source_path(store,asset)
            stat = source.stat()
            if stat.st_size != asset['size_bytes'] or stat.st_mtime_ns != asset['mtime_ns']:
                raise ValueError('Source media changed; reimport the source and rebuild the index.')
            # Status/search inspect filesystem metadata only. Workers perform a
            # fresh cancellable digest, so frequent polling never reads full media.
            if verify_sources and _sha256_file(source,check) != asset['sha256']:
                raise ValueError('Source media changed; reimport the source and rebuild the index.')
        except OSError as exc: raise ValueError('Source media is unavailable; relink it before indexing or searching.') from exc
    by_asset = {a['id']:a for a in assets}
    latest = {}
    for t in store.list_transcripts():
        check()
        if t['status'] != 'ready' or t['assetId'] not in by_asset or t['sourceFingerprint'] != by_asset[t['assetId']]['sha256']: continue
        artifact = artifacts.get(t.get('artifactId'))
        if t.get('artifactId') and (not artifact or artifact['status'] != 'ready'): continue
        latest[t['assetId']] = t
    transcripts = [latest[k] for k in sorted(latest)]
    analyses = []
    detector_artifacts = {}
    for artifact in sorted(artifacts.values(), key=lambda a:(a['created_at'],a['id'])):
        asset = by_asset.get(artifact['asset_id'])
        detector = artifact['kind'].removeprefix('analysis:')
        if asset and artifact['status']=='ready' and artifact['source_fingerprint']==asset['sha256'] and detector in {'shot_boundaries','silence','near_black','blur'} and artifact['generator_version']==DETECTORS[detector]['version']:
            detector_artifacts[(asset['id'],detector)] = artifact
    for row in store.list_analyses():
        check()
        if row['assetId'] not in by_asset or row['sourceFingerprint'] != by_asset[row['assetId']]['sha256']: continue
        if row['_artifactStatus'] != 'ready': continue
        latest_detector = detector_artifacts.get((row['assetId'],row['detectorId']))
        if latest_detector is None or latest_detector['id'] != row['artifactId']: continue
        if row['detectorId'] in {'shot_boundaries','silence','near_black','blur'}:
            analyses.append({k:row[k] for k in ('id','assetId','detectorId','detectorVersion','sourceInTicks','sourceOutTicks','sourceFingerprint','artifactId','config','result')})
    referenced = sorted({t['artifactId'] for t in transcripts if t.get('artifactId')} | {a['artifactId'] for a in analyses if a.get('artifactId')} | {a['id'] for a in detector_artifacts.values()})
    for artifact_id in referenced:
        check()
        artifact = artifacts[artifact_id]
        path = (store.root / artifact['relative_path']).resolve()
        path.relative_to(store.root.resolve())
        try:
            if _sha256_file(path,check) != artifact['checksum_sha256']: raise ValueError('A transcript or detector artifact is corrupt; regenerate it before indexing.')
        except OSError as exc: raise ValueError('A transcript or detector artifact is unavailable; regenerate it before indexing.') from exc
    data = {'projectId':store.project_id,'assets':assets,'transcripts':transcripts,'analyses':analyses,'detectorVersions':{key:DETECTORS[key]['version'] for key in ('shot_boundaries','silence','near_black','blur')},'detectorCoverage':{asset:sorted(detector for (owner,detector) in detector_artifacts if owner==asset) for asset in by_asset},'artifacts':[artifacts[k] for k in referenced]}
    return data, hashlib.sha256(_json(data).encode()).hexdigest()

def _model(runtime,check_cancelled=None):
    if runtime is None: raise ValueError('Configure an available local embedding model before semantic indexing.')
    selected = runtime.select('embedding',check_cancelled=check_cancelled)
    return {k:selected.get(k) for k in ('name','version','provider','settings')}

def indexing_job(store, runtime=None, *, semantic=False, force=False):
    if type(semantic) is not bool or type(force) is not bool: raise ValueError('semantic and force must be boolean.')
    snapshot, fingerprint = _snapshot(store)
    if not snapshot['transcripts']: raise ValueError('Create a transcript before building the footage index.')
    model = _model(runtime) if semantic else None
    config = {'schemaVersion':1,'chunkVersion':2,'embedding':model}
    if force: config['rebuildId'] = str(uuid.uuid4())
    return JobInput(str(store.root),'indexing',{'semantic':semantic},fingerprint,pipeline_version=VERSION,model_id=model['name'] if model else None,model_version=model['version'] if model else None,generator_id='localcut-footage-index',generator_version='1',config=config)

def _vector(vector):
    if not isinstance(vector,list) or not 1 <= len(vector) <= 8192 or any(type(v) not in (int,float) or abs(v)>1e308 or not math.isfinite(v) for v in vector): raise ValueError('Embedding response contains an invalid vector.')
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm <= 0: raise ValueError('Embedding response contains a zero or invalid vector.')
    return [v/norm for v in vector]

def _passages(transcript,check=lambda:None):
    """Prefer explicit segment review; otherwise incorporate word corrections.

    Word-only passages retain their first/last word source bounds. Grouping never
    interpolates timestamps, and a gap over one second starts another passage.
    """
    segments = transcript['segments']
    words = sorted(transcript['words'], key=lambda w:(w['sourceInTicks'],w['ordinal']))
    covered = set()
    passages = []
    for segment in segments:
        check()
        members = [w for w in words if w.get('segmentId') == segment['id'] or
                   (segment['sourceInTicks'] <= w['sourceInTicks'] and w['sourceOutTicks'] <= segment['sourceOutTicks'])]
        covered.update(w['id'] for w in members)
        item = dict(segment)
        if segment.get('editedText') is None and any(w.get('editedText') is not None for w in members):
            item['text'] = ' '.join(w['text'] for w in members)
        passages.append(item)
    group = []
    length = 0
    def flush():
        if group:
            passages.append({'id':group[0]['id'], 'text':' '.join(w['text'] for w in group),
                             'sourceInTicks':group[0]['sourceInTicks'],
                             'sourceOutTicks':max(w['sourceOutTicks'] for w in group)})
    for word in words:
        check()
        if word['id'] in covered:
            flush(); group=[]; length=0
            continue
        if len(word['text']) > MAX_TEXT:
            raise ValueError('A transcript word exceeds 2,000 characters. Shorten its reviewed text before indexing.')
        if group and (word['sourceInTicks'] - group[-1]['sourceOutTicks'] > 1_000_000 or length + 1 + len(word['text']) > MAX_TEXT):
            flush(); group=[]; length=0
        group.append(word); length += len(word['text']) + (1 if len(group)>1 else 0)
    flush()
    return sorted(passages,key=lambda p:(p['sourceInTicks'],p['id']))


def _chunks(data,check=lambda:None):
    result=[]
    assets={a['id']:a for a in data['assets']}
    for transcript in data['transcripts']:
        check()
        duration=assets[transcript['assetId']]['duration_ticks']
        for item in _passages(transcript,check):
            check()
            text=item['text']
            if len(text)>MAX_TEXT: raise ValueError('A transcript passage exceeds 2,000 characters. Split or shorten that passage before indexing; its timing cannot be invented.')
            if not text.strip(): continue
            start,end=item['sourceInTicks'],item['sourceOutTicks']
            if type(start) is not int or type(end) is not int or not 0<=start<end: raise ValueError('Transcript has invalid source timestamps.')
            if type(duration) is int and duration>0 and end>duration: raise ValueError('Transcript passage extends beyond the known source duration. Correct the timestamps before indexing.')
            related=[r for r in data['analyses'] if r['assetId']==transcript['assetId'] and r['sourceInTicks']<end and r['sourceOutTicks']>start]
            boundaries=sorted((r for r in data['analyses'] if r['assetId']==transcript['assetId'] and r['detectorId']=='shot_boundaries' and r['sourceInTicks']<=start),key=lambda r:r['sourceInTicks'])
            coverage=data['detectorCoverage'].get(transcript['assetId'],[])
            # This label describes the shot containing the passage start, even
            # when the passage itself spans a later candidate boundary.
            shot=boundaries[-1]['id'] if boundaries else (transcript['assetId']+':shot:initial' if 'shot_boundaries' in coverage else None)
            flags=sorted({r['detectorId'] for r in related if r['detectorId']!='shot_boundaries'})
            result.append({'id':item['id'],'assetId':transcript['assetId'],'transcriptId':transcript['id'],'sourceInTicks':start,'sourceOutTicks':end,'text':text,'shotId':shot,'flags':flags,'availableDetectors':coverage,'vector':None})
            if len(result)>MAX_CHUNKS: raise ValueError('Index exceeds 50,000 passages. Use a smaller project.')
    return result

class IndexHandler:
    kind='indexing'
    retry_safe=True
    def __init__(self,runtime=None): self.runtime=runtime
    def validate_input(self,payload):
        if not isinstance(payload,dict) or set(payload)!={'semantic'} or type(payload['semantic']) is not bool: raise ValueError('Index input requires a semantic boolean.')
        return payload
    def validate_job(self,job,context=None):
        self.validate_input(job.payload)
        expected_model = job.config.get('embedding')
        if job.pipeline_version != VERSION or job.generator_id != 'localcut-footage-index' or job.generator_version != '1' or job.config.get('schemaVersion') != 1 or job.config.get('chunkVersion') != 2:
            raise ValueError('Footage index job has incompatible generator or schema provenance.')
        if job.payload['semantic']:
            if not isinstance(expected_model,dict) or job.model_id != expected_model.get('name') or job.model_version != expected_model.get('version'):
                raise ValueError('Footage index embedding provenance does not match the job.')
        elif expected_model is not None or job.model_id is not None or job.model_version is not None:
            raise ValueError('An exact index must not claim an embedding model.')
        store=ProjectStore.open(Path(job.project_path))
        _,fingerprint=_snapshot(store,context.check_cancelled if context else lambda:None,verify_sources=context is not None)
        if fingerprint!=job.source_fingerprint: raise ValueError('Project transcripts or detector results changed; rebuild the footage index.')
        if job.payload['semantic'] and _model(self.runtime,context.check_cancelled if context else None)!=job.config['embedding']: raise ValueError('Embedding model changed; rebuild the footage index.')
    def run(self,context,payload):
        context.report(.02,'verifying sources','Checking source and transcript integrity locally.')
        self.validate_job(context.input,context)
        store=ProjectStore.open(Path(context.input.project_path))
        data,fingerprint=_snapshot(store,context.check_cancelled)
        context.report(.08,'preparing passages','Grouping reviewed text at existing source timestamps.')
        chunks=_chunks(data,context.check_cancelled)
        if not chunks: raise ValueError('No reviewed transcript text is available to index. Add or correct the transcript first.')
        provenance=[]; dimension=None
        for offset in range(0,len(chunks),16):
            context.check_cancelled(); batch=chunks[offset:offset+16]
            context.report(.1+.7*offset/max(1,len(chunks)),'embedding' if payload['semantic'] else 'indexing','Building local transcript search passages.')
            if payload['semantic']:
                embedded=self.runtime.embed([c['text'] for c in batch],check_cancelled=context.check_cancelled)
                vectors=embedded.get('vectors')
                if not isinstance(vectors,list) or len(vectors)!=len(batch): raise ValueError('Embedding response count does not match passages.')
                p=embedded['provenance']; expected=context.input.config['embedding']
                if p.get('modelName')!=expected['name'] or p.get('modelVersion')!=expected['version'] or p.get('provider')!=expected['provider'] or p.get('settings')!=expected['settings']: raise ValueError('Embedding model revision changed during indexing.')
                provenance.append(p)
                for chunk,v in zip(batch,vectors):
                    vector=_vector(v)
                    if dimension is not None and len(vector)!=dimension: raise ValueError('Embedding vector dimensions changed.')
                    dimension=len(vector)
                    if len(chunks)*dimension*64>MAX_VECTOR_MEMORY_BYTES:
                        raise ValueError('Semantic index exceeds the local vector memory budget. Use a smaller project or a lower-dimensional embedding model.')
                    chunk['vector']=vector
        context.check_cancelled(); self.validate_job(context.input,context)
        path=context.staging_file('.sqlite3')
        metadata={'schemaVersion':1,'chunkVersion':2,'pipelineVersion':VERSION,'projectId':store.project_id,'fingerprint':fingerprint,'createdAt':_now(),'semantic':payload['semantic'],'embedding':context.input.config['embedding'],'dimensions':dimension,'chunkCount':len(chunks),'provenance':provenance}
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE metadata(value TEXT NOT NULL)'); db.execute('INSERT INTO metadata VALUES (?)',(_json(metadata),))
            db.execute('CREATE TABLE chunks(id TEXT PRIMARY KEY,asset_id TEXT NOT NULL,source_in INTEGER NOT NULL,source_out INTEGER NOT NULL,folded TEXT NOT NULL,data TEXT NOT NULL)')
            db.execute('CREATE INDEX chunk_time ON chunks(asset_id,source_in,source_out)')
            for chunk in chunks:
                context.check_cancelled()
                db.execute('INSERT INTO chunks VALUES (?,?,?,?,?,?)',(chunk['id'],chunk['assetId'],chunk['sourceInTicks'],chunk['sourceOutTicks'],chunk['text'].casefold(),_json(chunk)))
            db.commit()
        context.report(.95,'validating','Validating the local footage index.')
        return JobOutput(str(path),f'cache/footage-index/{context.job_id}.sqlite3','footage-index',metadata)
    def validate_output(self,output):
        with closing(sqlite3.connect(output.staging_path)) as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise ValueError('Footage index is corrupt.')
            metadata=json.loads(db.execute('SELECT value FROM metadata').fetchone()[0])
            if metadata.get('schemaVersion')!=1 or db.execute('SELECT count(*) FROM chunks').fetchone()[0]!=metadata['chunkCount']: raise ValueError('Footage index version or passage count is invalid.')
            for (raw,) in db.execute('SELECT data FROM chunks'):
                row=json.loads(raw)
                if not 0<=row['sourceInTicks']<row['sourceOutTicks'] or len(row['text'])>MAX_TEXT: raise ValueError('Footage index passage is invalid.')
                if metadata['semantic'] and len(_vector(row['vector']))!=metadata['dimensions']: raise ValueError('Footage index vectors are invalid.')
    def commit_output(self,connection,store,job,artifact_id,target,metadata=None):
        _, fingerprint = _snapshot(store)
        if fingerprint != job.source_fingerprint: raise ValueError('Project changed before index commit; rebuild it.')

def _open_latest(store):
    with _connect(store.database_path) as db:
        rows=db.execute("SELECT * FROM artifacts WHERE project_id=? AND kind='footage-index' AND status='ready' ORDER BY created_at DESC",(store.project_id,)).fetchall()
    for row in rows:
        db = None
        try:
            path=(store.root/row['relative_path']).resolve(); path.relative_to(store.root.resolve())
            if not path.is_file() or _sha256_file(path)!=row['checksum_sha256']: continue
            # immutable prevents journal creation; path URI quoting is handled by Path.as_uri.
            db=sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True)
            metadata=json.loads(db.execute('SELECT value FROM metadata').fetchone()[0])
            if metadata['schemaVersion']!=1 or metadata['projectId']!=store.project_id: db.close(); continue
            return db,metadata,row['id']
        except (OSError,ValueError,KeyError,sqlite3.Error):
            if db is not None: db.close()
            continue
    return None,None,None

def index_status(store,runtime=None):
    db,metadata,artifact_id=_open_latest(store)
    if db is None: return {'available':False,'stale':False}
    db.close()
    try:
        _,fingerprint=_snapshot(store)
        stale=metadata['fingerprint']!=fingerprint or metadata.get('chunkVersion')!=2 or metadata.get('pipelineVersion')!=VERSION
    except (OSError,ValueError): stale=True
    if runtime is not None and metadata['semantic']:
        try: stale=stale or _model(runtime)!=metadata['embedding']
        except (ValueError,RuntimeError): stale=True
    return {'available':True,'stale':stale,'artifactId':artifact_id,**metadata}

def search_index(store,query,*,mode='exact',runtime=None,filters=None,limit=30):
    if not isinstance(query,str) or not query.strip() or len(query)>3000: raise ValueError('Enter a search query from 1 to 3,000 characters.')
    if mode not in {'exact','semantic'} or type(limit) is not int or not 1<=limit<=100: raise ValueError('Choose exact or semantic search and limit from 1 to 100.')
    filters={} if filters is None else filters
    if not isinstance(filters,dict) or set(filters)-FILTERS: raise ValueError('Unknown footage search filter.')
    for key in ('sourceInTicks','sourceOutTicks'):
        if key in filters and (type(filters[key]) is not int or filters[key]<0): raise ValueError('Search times must be nonnegative integer source ticks.')
    if 'sourceInTicks' in filters and 'sourceOutTicks' in filters and filters['sourceInTicks']>=filters['sourceOutTicks']: raise ValueError('Search source range must be increasing.')
    for key in ('assetId','shotId'):
        if key in filters and (not isinstance(filters[key],str) or len(filters[key])>200): raise ValueError('Search identifiers must be short strings.')
    if 'silence' in filters and type(filters['silence']) is not bool: raise ValueError('Silence filter must be boolean.')
    if 'qualityFlags' in filters and (not isinstance(filters['qualityFlags'],list) or any(not isinstance(v,str) or v not in {'near_black','blur'} for v in filters['qualityFlags'])): raise ValueError('Quality flags must be near_black or blur.')
    # A stopped embedding provider does not make the exact index corrupt/stale.
    # Check model compatibility below so semantic errors retain actionable codes.
    status=index_status(store)
    if not status['available']: raise ValueError('Build a footage index before searching.')
    if status['stale']: raise ValueError('Footage index is out of date. Rebuild it before searching.')
    db,metadata,_=_open_latest(store)
    if db is None: raise ValueError('Footage index is unavailable; rebuild it.')
    try:
        vector=None
        if mode=='semantic':
            if not metadata['semantic']: raise ValueError('Rebuild with local embeddings to enable semantic search.')
            if _model(runtime)!=metadata['embedding']: raise ValueError('Footage index embedding model is out of date; rebuild it.')
            response=runtime.embed([query]); vector=_vector(response['vectors'][0])
            if len(vector)!=metadata['dimensions']: raise ValueError('Query embedding dimensions differ from index.')
            p=response['provenance']; model=metadata['embedding']
            if p.get('modelName')!=model['name'] or p.get('modelVersion')!=model['version'] or p.get('provider')!=model['provider'] or p.get('settings')!=model['settings']: raise ValueError('Query embedding model changed; rebuild the index.')
        sql='SELECT data FROM chunks WHERE 1=1'; args=[]
        if mode=='exact': sql+=' AND instr(folded,?)>0'; args.append(query.casefold())
        for key,column,op in (('assetId','asset_id','='),('sourceInTicks','source_out','>'),('sourceOutTicks','source_in','<')):
            if key in filters: sql+=f' AND {column}{op}?'; args.append(filters[key])
        results=[]
        for (raw,) in db.execute(sql,args):
            row=json.loads(raw)
            if 'shotId' in filters and row['shotId']!=filters['shotId']: continue
            if 'silence' in filters and ('silence' not in row['availableDetectors'] or ('silence' in row['flags'])!=filters['silence']): continue
            if any(flag not in row['flags'] for flag in filters.get('qualityFlags',[])): continue
            stored=row.pop('vector')
            row['score']=sum(a*b for a,b in zip(vector,stored)) if vector is not None else 1.0
            row['score']=max(-1.0,min(1.0,row['score']))
            results.append(row)
            results.sort(key=lambda r:(-r['score'],r['assetId'],r['sourceInTicks'],r['id']))
            if len(results)>limit: results.pop()
        return {'results':results,'index':status,'stale':False}
    finally: db.close()



