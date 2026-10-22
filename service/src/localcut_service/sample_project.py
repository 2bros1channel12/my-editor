"""A generated, redistributable tutorial test card; no download or private media."""
from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

from .briefs import DeterministicBriefParser
from .media import FFprobe, import_media
from .persistence import ProjectStore
from .timeline import timeline_command, timeline_state


def create_sample_project(config):
    ffmpeg = config.ffmpeg_path or shutil.which('ffmpeg')
    ffprobe = config.ffprobe_path or shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        raise ValueError('Install or configure FFmpeg and FFprobe before creating the local sample.')
    root = config.data_dir / 'samples' / f'sample-{uuid.uuid4()}'
    store = ProjectStore.create(root, 'Local Cut sample', settings={'sampleProject': True, 'sampleLicense': 'CC0-1.0', 'sampleNotice': 'Generated test pattern and sine wave; demo captions are authored text, not inferred speech.'})
    try:
        with tempfile.TemporaryDirectory(prefix='localcut-sample-') as scratch:
            source = Path(scratch) / 'Synthetic tutorial.mp4'
            subprocess.run([str(ffmpeg), '-hide_banner', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=30:duration=6', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=6', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', '-n', str(source)], shell=False, check=True, capture_output=True, timeout=60, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            imported = import_media(store, [source], probe=FFprobe(str(ffprobe)), storage_mode='managed')
            if imported['errors'] or not imported['imported']:
                raise ValueError('Could not inspect the generated sample media. Check the configured media tools.')
            asset = imported['imported'][0]['assetId']
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
        words = [dict(text=text, sourceInTicks=start, sourceOutTicks=end) for text, start, end in [('Synthetic', 200000, 1000000), ('sample.', 1000000, 1800000), ('Review', 2200000, 3000000), ('each', 3000000, 3400000), ('edit.', 3400000, 4200000)]]
        store.add_transcript(asset_id=asset, language='en', words=words, generator_id='localcut-authored-demo', generator_version='1', source_fingerprint=digest)
        text = 'Purpose: Demonstrate a private editable sample\nAudience: First-time editors\nRequired topics: Synthetic sample\nTone: calm\nAspect ratio: 16:9'
        store.add_brief(text, constraints=DeterministicBriefParser().parse(text), generator_id='localcut-authored-demo', generator_version='1')
        doc = timeline_state(store)
        doc = timeline_command(store, dict(type='insert', assetId=asset, trackId='video', sourceInTicks=0, sourceOutTicks=6000000), doc['revision'])
        clip = next(c for c in doc['clips'] if c['itemType']=='clip')
        doc = timeline_command(store, dict(type='caption', clipId=clip['id'], text='Synthetic sample — review each edit', sourceInTicks=200000, sourceOutTicks=4200000), doc['revision'])
        return {'path': str(store.root), 'projectId': store.project_id, 'name': 'Local Cut sample', 'license': 'CC0-1.0', 'notice': 'Synthetic test card with tone audio. Demo transcript is authored, not speech recognition.'}
    except Exception:
        # Keep the failed native project for inspection; never recursively remove
        # a user-selected path. Each attempt uses its own fresh sample ID.
        raise
