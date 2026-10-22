import shutil
from pathlib import Path
import pytest
from localcut_service.config import AppConfig
from localcut_service.persistence import ProjectStore
from localcut_service.sample_project import create_sample_project
from localcut_service.timeline import timeline_state, timeline_undo


def test_sample_missing_tools_is_actionable(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil,'which',lambda _:None)
    with pytest.raises(ValueError,match='FFmpeg and FFprobe'):
        create_sample_project(AppConfig(data_dir=tmp_path))


def test_real_generated_sample_is_managed_editable_and_reopens(tmp_path):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'): pytest.skip('FFmpeg pair required')
    result=create_sample_project(AppConfig(data_dir=tmp_path))
    store=ProjectStore.open(Path(result['path']))
    state=store.load_state()
    assert state['settings']['sampleLicense']=='CC0-1.0'
    assert state['assets'][0]['path_mode']=='project-relative'
    assert state['assets'][0]['original_uri'].startswith('media/')
    doc=timeline_state(store)
    assert len(doc['captions'])==1
    assert timeline_undo(store,doc['revision'])['captions']==[]
