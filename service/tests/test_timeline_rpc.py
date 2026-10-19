from pathlib import Path
import pytest
from localcut_service.config import AppConfig
from localcut_service.persistence import ProjectStore, ProjectError
from localcut_service.server import dispatch


def fixture(tmp_path):
    store = ProjectStore.create(tmp_path / "edit.localcut", "Edit")
    source = tmp_path / "my original.mov"
    source.write_bytes(b"immutable source")
    asset = store.import_asset_reference(source, {}, probe_tool="fixture", probe_version="1", duration_ticks=10_000_000)["assetId"]
    config = AppConfig(data_dir=tmp_path / "app")
    return store, source, asset, config


def test_command_rpc_autosaves_reopens_and_restores_redo(tmp_path):
    store, source, asset, config = fixture(tmp_path)
    def call(method, **params): return dispatch(method, {"path": str(store.root), **params}, config)["timeline"]
    empty = call("timeline_get")
    inserted = call("timeline_command", expectedRevision=empty["revision"], command={"type":"insert", "assetId":asset, "trackId":"video", "sourceInTicks":1_000_000, "sourceOutTicks":4_000_000})
    assert inserted["durationTicks"] == 3_000_000
    clip_id = inserted["clips"][0]["id"]
    with pytest.raises(ProjectError, match="changed"):
        call("timeline_command", expectedRevision=empty["revision"], command={"type":"delete", "clipId":clip_id})
    undone = call("timeline_undo", expectedRevision=inserted["revision"])
    assert undone["clips"] == [] and undone["canRedo"]
    restored = ProjectStore.open(store.root).load_state()
    assert restored["timelineItems"] == []
    reopened = call("timeline_get")
    assert reopened["canRedo"]
    redone = call("timeline_redo", expectedRevision=reopened["revision"])
    assert redone["clips"][0]["id"] == clip_id
    assert source.read_bytes() == b"immutable source"


@pytest.mark.parametrize("revision", [True, -1, 1.5, "1", None])
def test_rpc_rejects_noninteger_revisions_before_mutation(tmp_path, revision):
    store, _, _, config = fixture(tmp_path)
    before = store.load_state()
    with pytest.raises(ValueError):
        dispatch("timeline_undo", {"path": str(store.root), "expectedRevision": revision}, config)
    assert store.load_state() == before


def test_rpc_rejects_extra_operation_fields(tmp_path):
    store, _, _, config = fixture(tmp_path)
    with pytest.raises(ValueError, match="exactly"):
        dispatch("timeline_get", {"path": str(store.root), "execute":"delete originals"}, config)


def test_legacy_music_and_reorder_properties_bootstrap_without_loss(tmp_path):
    store, _, asset, config = fixture(tmp_path)
    state = store.load_state()
    state['timelineItems'] = [
        {'id':'legacy-video','assetId':asset,'itemType':'clip','trackId':'video','timelineStartTicks':0,'timelineDurationTicks':2_000_000,'sourceInTicks':0,'sourceOutTicks':2_000_000,'properties':{'orderSuggestionId':'reviewed-order','cropPreset':'full_frame'}},
        {'id':'legacy-audio','assetId':asset,'itemType':'audio','trackId':'music','timelineStartTicks':0,'timelineDurationTicks':2_000_000,'sourceInTicks':0,'sourceOutTicks':2_000_000,'properties':{'role':'music-bed','userSupplied':True,'audioGainDb':-18}}
    ]
    store.save_state(store.project_id, state)
    doc = dispatch('timeline_get', {'path':str(store.root)}, config)['timeline']
    assert len(doc['clips']) == 2
    assert next(c for c in doc['clips'] if c['id']=='legacy-audio')['properties']['userSupplied']
    assert all(c['properties']['sourceProvenance']['assetId']==asset for c in doc['clips'])


@pytest.mark.parametrize('text', ['', '   ', 'x' * 4001])
def test_caption_text_validation_is_atomic(tmp_path, text):
    store, _, asset, config = fixture(tmp_path)
    doc = dispatch('timeline_get', {'path':str(store.root)}, config)['timeline']
    doc = dispatch('timeline_command', {'path':str(store.root), 'expectedRevision':doc['revision'], 'command':{'type':'insert','assetId':asset,'trackId':'video','sourceInTicks':0,'sourceOutTicks':1_000_000}}, config)['timeline']
    before = store.load_state()
    with pytest.raises(ValueError, match='Caption'):
        dispatch('timeline_command', {'path':str(store.root), 'expectedRevision':doc['revision'], 'command':{'type':'caption','clipId':doc['clips'][0]['id'],'text':text}}, config)
    assert store.load_state() == before
