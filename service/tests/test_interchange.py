import json
import sqlite3

import pytest

from localcut_service.interchange import build_otio, export_otio
from localcut_service.persistence import ProjectStore, _connect
from localcut_service.timeline import timeline_command, timeline_state


def setup(tmp_path):
    source = tmp_path / "source [雪] #1.mp4"
    source.write_bytes(b"original")
    store = ProjectStore.create(tmp_path / "project", "Interchange")
    asset = store.add_asset(source)
    with sqlite3.connect(store.database_path) as db:
        db.execute("UPDATE assets SET duration_ticks=? WHERE id=?", (10_000_000, asset))
    def command(**cmd):
        return timeline_command(store, cmd, timeline_state(store)["revision"])
    doc = command(type="insert", assetId=asset, trackId="video", sourceInTicks=123456, sourceOutTicks=2123456, atTicks=500000)
    clip = doc["clips"][0]["id"]
    command(type="caption", clipId=clip, text="Editable captions", sourceInTicks=123456, sourceOutTicks=1123456)
    command(type="gain", clipId=clip, gainDb=-6)
    command(type="marker", atTicks=500000, label="Opening")
    return store, source


def test_otio_preserves_source_ticks_gap_caption_gain_and_uri(tmp_path):
    store, source = setup(tmp_path)
    output = export_otio(store)
    doc = json.loads((store.root / output["relativePath"]).read_text("utf-8"))
    track = doc["tracks"]["children"][0]
    gap, clip = track["children"]
    assert gap["source_range"]["duration"]["value"] == 500000
    assert clip["source_range"]["start_time"] == {"OTIO_SCHEMA": "RationalTime.1", "rate": 1000000, "value": 123456}
    assert clip["media_references"]["DEFAULT_MEDIA"]["target_url"] == source.as_uri()
    assert clip["metadata"]["localcut"]["properties"]["audioGainDb"] == -6
    assert doc["metadata"]["localcut"]["timeline"]["captions"][0]["text"] == "Editable captions"
    assert doc["tracks"]["markers"][0]["marked_range"]["start_time"]["value"] == 500000
    assert source.read_bytes() == b"original"
    with _connect(store.database_path) as db:
        assert db.execute("SELECT format FROM exports").fetchone()[0] == "otio"


def test_missing_source_and_invalid_bounds_publish_nothing(tmp_path):
    store, source = setup(tmp_path)
    state = store.load_state()
    state["timelineItems"][0]["sourceOutTicks"] = 999999999
    with pytest.raises(ValueError):
        build_otio(store, state)
    source.unlink()
    with pytest.raises(FileNotFoundError):
        export_otio(store)
    assert not list((store.root / "exports").glob("*.otio"))


def test_otio_reads_with_official_adapter_when_installed(tmp_path):
    otio = pytest.importorskip("opentimelineio")
    store, _ = setup(tmp_path)
    output = export_otio(store)
    timeline = otio.adapters.read_from_file(str(store.root / output["relativePath"]))
    assert timeline.tracks[0][0].duration().to_seconds() == .5
    assert timeline.tracks[0][1].source_range.start_time.to_seconds() == .123456
    assert timeline.duration().to_seconds() == 2.5
