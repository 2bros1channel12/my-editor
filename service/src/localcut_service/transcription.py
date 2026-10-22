"""Replaceable local ASR, explicit verified model acquisition, and job integration."""
from pathlib import Path
from typing import Protocol, TypedDict
from fractions import Fraction
import hashlib
import json
import math
import string
import os
import shutil
import urllib.request
import urllib.error
import wave
import threading
import uuid

INFERENCE_LOCK = threading.Lock()
VERIFIED_FILES = {}
VERIFIED_BUNDLES = {}

class SpeechWord(TypedDict):
    text: str
    startSeconds: float
    endSeconds: float
    confidence: float | None

class SpeechSegment(SpeechWord):
    words: list[SpeechWord]

class SpeechResult(TypedDict):
    language: str
    segments: list[SpeechSegment]

class TranscriptionInput(TypedDict):
    assetId: str
    model: str


from .jobs import JobInput, JobOutput, TransientJobError
from .persistence import ProjectStore, _connect, _sha256_file
from .proxies import MediaProcess, PIPELINE_VERSION as PROXY_PIPELINE_VERSION, source_digest, source_path

REVISION = '5359861c739e955e79d9a303bcbc70fb988958b1'
CATALOG = {
 'tiny': (77691713, 'be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21', 2),
 'base': (147951465, '60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe', 3),
 'small': (487601967, '1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b', 5),
}

def choose_model(profile):
    available = profile.get('memory', {}).get('availableBytes')
    # Leave room for the OS, UI, decoded media and native runtime buffers.
    return 'small' if available and available >= 8 * 1024**3 else 'base' if available and available >= 5 * 1024**3 else 'tiny'

def model_file(store, model):
    if model not in CATALOG: raise ValueError('Unknown local model.')
    return store.root / 'models' / model / REVISION / 'model.bin'

def model_status(store, model, *, force=False):
    size, digest, ram = CATALOG[model]
    path = model_file(store, model)
    ready = False
    if path.is_file() and not path.is_symlink() and path.stat().st_size == size:
        stat = path.stat()
        signature = (stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns,stat.st_ino,digest)
        ready = (not force and VERIFIED_FILES.get(str(path)) == signature) or _sha256_file(path) == digest
        if ready: VERIFIED_FILES[str(path)] = signature
    return dict(id=model, version=REVISION, bytes=size, requiredRamBytes=ram*1024**3, ready=ready, integrity='verified' if ready else 'missing_or_invalid', license='MIT', source='Hugging Face / ggerganov/whisper.cpp', sourceUrl=f'https://huggingface.co/ggerganov/whisper.cpp/resolve/{REVISION}/ggml-{model}.bin', sha256=digest)

class ModelDownloadHandler:
    kind = 'model_download'
    retry_safe = True
    def validate_input(self, payload):
        if payload.get('model') not in CATALOG: raise ValueError('Choose a catalog model.')
        return {'model': payload['model']}
    def run(self, context, payload):
        model = payload['model']; size, digest, _ = CATALOG[model]
        store = ProjectStore.open(Path(context.input.project_path))
        if shutil.disk_usage(store.root).free < size + 256*1024**2: raise ValueError('Free additional disk space before downloading this model.')
        target = model_file(store, model)
        if target.exists() and not model_status(store,model)['ready']:
            target.rename(target.with_name('invalid-'+str(uuid.uuid4())+'.bin'))
        stage = context.staging_file('.bin')
        url = f'https://huggingface.co/ggerganov/whisper.cpp/resolve/{REVISION}/ggml-{model}.bin'
        count = 0; checksum = hashlib.sha256()
        context.report(0, 'downloading', 'Downloading model weights only; no media is uploaded.')
        try:
            with urllib.request.urlopen(url, timeout=15) as response, stage.open('wb') as output:
                while True:
                    context.check_cancelled()
                    block = response.read(256*1024)
                    if not block: break
                    count += len(block)
                    if count > size: raise ValueError('Model download exceeds catalog size.')
                    checksum.update(block); output.write(block)
                    context.report(min(.95, count/size*.95), 'downloading', f'{count} / {size} bytes')
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise TransientJobError("Model download interrupted. Check connectivity and retry.") from exc
        if count != size or checksum.hexdigest() != digest: raise ValueError('Model integrity verification failed. Retry the download.')
        return JobOutput(str(stage), model_file(store, model).relative_to(store.root).as_posix(), 'model', {'model':model, 'version':REVISION}, 'MIT')
    def validate_output(self, output):
        size, digest, _ = CATALOG[output.metadata['model']]
        if Path(output.staging_path).stat().st_size != size or _sha256_file(Path(output.staging_path)) != digest: raise ValueError('Invalid model output.')

class SpeechBackend(Protocol):
    name: str
    version: str
    def transcribe(self, audio: Path, model: Path, output: Path, context, language: str) -> SpeechResult: ...

class WhisperCppBackend:
    name = 'whisper.cpp'
    def __init__(self, executable=None):
        candidate = executable or os.environ.get('LOCAL_CUT_WHISPER_CLI') or 'whisper-cli'
        self.executable = shutil.which(str(candidate))
        self.version = 'binary-sha256:' + _sha256_file(Path(self.executable)) if self.executable and Path(self.executable).is_file() else 'unavailable'
    def transcribe(self, audio, model, output, context, language='auto'):
        if not self.executable: raise ValueError('Install whisper.cpp whisper-cli and set LOCAL_CUT_WHISPER_CLI, then restart the app.')
        context.report(.1, 'transcribing', 'Local speech inference is running. Cancellation stops the inference process.')
        # CPU is a conservative default: driver/VRAM discovery does not prove backend compatibility.
        try:
            MediaProcess().run([str(self.executable), '-m', str(model), '-f', str(audio), '-ojf', '-of', str(output), '-l', language, '-ng', '-t', str(max(1,min(4,(os.cpu_count() or 2)//2)))], context, stage='transcribing')
        except TransientJobError:
            raise
        except RuntimeError as exc:
            raise ValueError('Local inference failed. Close memory-heavy apps or choose tiny in Models; verify the whisper.cpp executable and model format. Details: '+str(exc)) from exc
        if not output.with_suffix('.json').is_file(): raise ValueError('whisper.cpp did not produce full JSON output. Verify the executable supports -ojf.')
        if output.with_suffix('.json').stat().st_size > 128*1024**2: raise ValueError('Transcript exceeded the output limit. Split the footage into shorter inputs.')
        raw = json.loads(output.with_suffix('.json').read_text(encoding='utf-8'))
        return normalize_whisper(raw)

def normalize_whisper(raw):
    """whisper.cpp offsets are milliseconds; token t0/t1 values are 10ms units."""
    if not isinstance(raw.get("transcription"), list): raise ValueError("Backend JSON is missing transcript segments.")
    segments=[]
    for item in raw.get('transcription', []):
        start=item['offsets']['from']/1000; end=item['offsets']['to']/1000
        words=[]; current=None
        for token in item.get('tokens', []):
            text=token.get('text','')
            if not text.strip() or text.startswith('[_') or token.get('id',0)>=50257: continue
            offsets=token.get('offsets', {})
            a=offsets.get('from', token.get('t0',0)*10)/1000
            b=offsets.get('to', token.get('t1',0)*10)/1000
            uncertain = b <= a
            inferred_start, inferred_end = a, b
            if uncertain:
                if current is not None and (not text[0].isspace() or all(c in string.punctuation for c in text.strip())):
                    current['text'] += text.strip()
                    current['timingUncertain'] = True
                    if current['confidence'] is not None and token.get('p') is not None:
                        current['confidence'] = min(current['confidence'],token['p'])
                    current['inferenceEndSeconds'] = inferred_end
                    continue
                if end <= start: raise ValueError('Backend segment has no usable timing.')
                # A zero-length native estimate is retained, with an explicit
                # uncertain 10ms review interval bounded by its parent segment.
                a = min(max(start, a), max(start, end-.01))
                b = min(end, a+.01)
            if text[0].isspace() or current is None:
                current={'text':text.strip(), 'startSeconds':a,'endSeconds':b,'confidence':token.get('p'), 'timingUncertain':uncertain, 'inferenceStartSeconds':inferred_start, 'inferenceEndSeconds':inferred_end}; words.append(current)
            else:
                current['text']+=text; current['endSeconds']=b
                current['inferenceEndSeconds']=inferred_end
                current['timingUncertain']=current.get('timingUncertain',False) or uncertain
                if current['confidence'] is not None and token.get('p') is not None: current['confidence']=min(current['confidence'],token['p'])
        if str(item.get('text','')).strip() and not words: raise ValueError('Backend returned text without word timestamps. Use a whisper.cpp build supporting full JSON token timestamps.')
        if end<=start and str(item.get('text','')).strip(): raise ValueError('Backend returned a segment without usable timing.')
        if end>start: segments.append(dict(text=item.get('text','').strip(),startSeconds=start,endSeconds=end,words=words))
    return {'language':raw.get('result',{}).get('language') or 'und', 'segments':segments}

def ready_proxy(store, asset_id, *, force=True):
    with _connect(store.database_path) as db:
        rows=db.execute("SELECT artifacts.* FROM artifacts JOIN assets ON assets.id=artifacts.asset_id WHERE artifacts.asset_id=? AND artifacts.source_fingerprint=assets.sha256 AND artifacts.kind='proxy' AND artifacts.status='ready' ORDER BY artifacts.created_at DESC",(asset_id,)).fetchall()
    for row in rows:
        path=(store.root/row['relative_path']).resolve(); path.relative_to(store.root.resolve())
        if path.is_dir():
            files=sorted(path.rglob('*'))
            if any(p.is_symlink() for p in files): continue
            signature=tuple((str(p.relative_to(path)),p.stat().st_size,p.stat().st_mtime_ns,p.stat().st_ctime_ns,p.stat().st_ino) for p in files if p.is_file())
            key=(str(path),row['checksum_sha256'])
            if (not force and VERIFIED_BUNDLES.get(key)==signature) or _sha256_file(path)==row['checksum_sha256']:
                VERIFIED_BUNDLES[key]=signature
                try:
                    manifest=json.loads((path/'manifest.json').read_text(encoding='utf-8'))
                except (OSError,ValueError,json.JSONDecodeError):
                    continue
                if manifest.get('pipelineVersion') == PROXY_PIPELINE_VERSION:
                    return path
    raise ValueError('Generate preview assets first, then transcribe. Missing or invalid analysis media.')

class TranscriptionHandler:
    kind='transcription'; retry_safe=True
    def __init__(self, backend=None): self.backend=backend or WhisperCppBackend()
    def validate_input(self,payload):
        if payload.get('model') not in CATALOG or not isinstance(payload.get('assetId'),str): raise ValueError('Transcription requires assetId and catalog model.')
        return {'model':payload['model'],'assetId':payload['assetId']}
    def validate_job(self, job, context=None):
        if job.asset_id != job.payload.get('assetId') or job.model_id != job.payload.get('model') or job.model_version != REVISION:
            raise ValueError('Transcription provenance does not match its input.')
        store=ProjectStore.open(Path(job.project_path))
        with _connect(store.database_path) as db: asset=db.execute('SELECT * FROM assets WHERE id=? AND project_id=?',(job.asset_id,store.project_id)).fetchone()
        if asset is None or asset['sha256']!=job.source_fingerprint or source_digest(source_path(store,asset),context)!=job.source_fingerprint: raise ValueError('Source changed or unavailable. Relink/import the original file.')
    def run(self,context,payload):
        self.validate_job(context.input,context)
        store=ProjectStore.open(Path(context.input.project_path)); model=payload['model']
        if not model_status(store,model,force=True)['ready']: raise ValueError('Download and verify the selected model in Models first.')
        from .capabilities import collect_capabilities
        available=collect_capabilities(store.root)['memory'].get('availableBytes')
        if available is not None and available < CATALOG[model][2]*1024**3: raise ValueError('Insufficient available RAM. Close other applications or select the tiny model in Models and retry.')
        bundle=ready_proxy(store,payload['assetId']); manifest=json.loads((bundle/'manifest.json').read_text())
        analysis=manifest.get('analysis')
        if not analysis: raise ValueError('This source has no audio track to transcribe.')
        audio=(bundle/analysis['path']).resolve(); audio.relative_to(bundle)
        mapping=json.loads((bundle/str(manifest.get('mapping','mapping.json'))).read_text(encoding='utf-8'))
        source_origin=Fraction(mapping.get('sourceStartSeconds','0'))
        offset=round(float(Fraction(analysis['sourceStartSeconds'])-source_origin)*1e6)
        if offset<0: raise ValueError('Negative source audio origins require timestamp normalization before transcription.')
        with wave.open(str(audio)) as wav: duration=wav.getnframes()/wav.getframerate()
        if shutil.disk_usage(store.root).free < 32*1024**2 + duration*8192:
            raise ValueError('Free additional disk space for transcript artifacts, then retry.')
        stage=context.staging_file('.json'); prefix=stage.with_suffix('')
        context.report(.05,'waiting_for_memory','Waiting for the local inference slot.')
        while not INFERENCE_LOCK.acquire(timeout=.1): context.check_cancelled()
        try:
            context.check_cancelled()
            available=collect_capabilities(store.root)['memory'].get('availableBytes')
            if available is not None and available<CATALOG[model][2]*1024**3:
                raise ValueError('Available memory fell while queued. Close other apps or select tiny and retry.')
            result=self.backend.transcribe(audio,model_file(store,model),prefix,context,'auto')
        finally:
            INFERENCE_LOCK.release()
        segments=[]; words=[]
        def timed(record):
            a=float(record['startSeconds']); b=float(record['endSeconds'])
            if not math.isfinite(a+b) or a<0 or b<=a or b>duration+.5: raise ValueError('Backend returned invalid or out-of-bounds timestamps.')
            confidence=record.get('confidence')
            if confidence is not None and (not math.isfinite(confidence) or not 0<=confidence<=1): raise ValueError('Invalid confidence value.')
            return dict(text=record['text'],sourceInTicks=offset+round(a*1e6),sourceOutTicks=offset+round(b*1e6),confidence=confidence,speakerOrdinal=None,timingUncertain=record.get('timingUncertain',False),inferenceStartSeconds=record.get('inferenceStartSeconds',a),inferenceEndSeconds=record.get('inferenceEndSeconds',b))
        for segment in result['segments']:
            normalized=timed(segment); segments.append(normalized)
            for word in segment['words']:
                w=timed(word)
                if w['sourceInTicks']<normalized['sourceInTicks'] or w['sourceOutTicks']>normalized['sourceOutTicks']: raise ValueError('Word outside segment.')
                words.append(w)
        self.validate_job(context.input,context)
        data=dict(schemaVersion=1,assetId=payload['assetId'],language=result['language'],segments=segments,words=words,analysisStartTicks=offset,modelId=model,modelVersion=REVISION,backend=self.backend.name,backendVersion=self.backend.version)
        stage.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        context.check_cancelled(); context.report(.95,'validating','Validating transcript and source timestamps.')
        return JobOutput(str(stage),f'cache/transcripts/{context.job_id}.json','transcript',{'language':result['language']},'MIT')
    def validate_output(self,output):
        data=json.loads(Path(output.staging_path).read_text(encoding='utf-8'))
        if data.get('schemaVersion')!=1 or not isinstance(data.get('words'),list): raise ValueError('Invalid transcript artifact.')
    def commit_output(self,connection,store,job,artifact_id,target,metadata=None):
        data=json.loads(target.read_text(encoding='utf-8'))
        store.add_transcript(asset_id=job.asset_id,language=data['language'],words=data['words'],segments=data['segments'],generator_id=data['backend'],generator_version=data['backendVersion'],source_fingerprint=job.source_fingerprint,model_id=data['modelId'],model_version=data['modelVersion'],artifact_id=artifact_id,analysis_start_ticks=data['analysisStartTicks'],connection=connection)

def transcription_job(store,asset_id,model,backend):
    with _connect(store.database_path) as db: asset=db.execute('SELECT sha256 FROM assets WHERE id=? AND project_id=?',(asset_id,store.project_id)).fetchone()
    if asset is None: raise ValueError('Source asset is unavailable.')
    ready_proxy(store,asset_id)
    return JobInput(str(store.root),'transcription',{'assetId':asset_id,'model':model},asset['sha256'],pipeline_version='asr-1',asset_id=asset_id,model_id=model,model_version=REVISION,generator_id=backend.name,generator_version=backend.version)
