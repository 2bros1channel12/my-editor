from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from localcut_service.media import FFprobe, ProbeError, discover_files, import_media, normalize_probe, relink_media
from localcut_service.persistence import ProjectStore, _connect


def probe_fixture() -> dict:
    return {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "12.5", "start_time": "0"},
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264", "profile": "High", "width": 1920, "height": 1080, "avg_frame_rate": "30000/1001", "r_frame_rate": "30/1", "time_base": "1/30000", "start_time": "0", "side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}]},
            {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2, "channel_layout": "stereo", "sample_rate": "48000", "disposition": {"default": 1}},
            {"index": 2, "codec_type": "audio", "codec_name": "aac", "channels": 1, "sample_rate": "44100", "disposition": {"default": 0}},
        ],
    }


def test_normalize_probe_preserves_rationals_rotation_audio_and_vfr_evidence() -> None:
    result = normalize_probe(probe_fixture())
    assert result["durationSeconds"] == 12.5
    assert result["durationTicks"] == 12_500_000
    assert result["video"]["avgFrameRate"]["raw"] == "30000/1001"
    assert result["video"]["timebase"]["raw"] == "1/30000"
    assert result["video"]["rotation"]["degrees"] == 270
    assert result["video"]["variableFrameRate"]["likely"] is True
    assert len(result["audio"]) == 2


def test_rotation_legacy_tag_and_unknown_rationals() -> None:
    data = probe_fixture()
    data["streams"][0]["side_data_list"] = []
    data["streams"][0]["tags"] = {"rotate": "90"}
    data["streams"][0]["avg_frame_rate"] = "0/0"
    data["streams"][0]["r_frame_rate"] = "N/A"
    result = normalize_probe(data)
    assert result["video"]["rotation"]["degrees"] == 90
    assert result["video"]["avgFrameRate"]["value"] is None
    assert result["video"]["variableFrameRate"]["confidence"] == "low"


def test_normalize_rejects_no_video_and_wrong_container() -> None:
    with pytest.raises(ProbeError, match="No video"):
        normalize_probe({"format": {"format_name": "mov"}, "streams": [{"codec_type": "audio"}]})
    with pytest.raises(ProbeError, match="not a supported"):
        normalize_probe({"format": {"format_name": "matroska"}, "streams": [{"codec_type": "video"}]})


def test_ffprobe_missing_timeout_failure_and_invalid_json(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("localcut_service.media.shutil.which", lambda _name: None)
    with pytest.raises(ProbeError) as missing:
        FFprobe(None).probe(tmp_path / "x.mp4")
    assert missing.value.code == "ffprobe_unavailable"

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("ffprobe", 1)
    with pytest.raises(ProbeError) as timed:
        FFprobe("ffprobe", runner=timeout).probe(tmp_path / "x.mp4")
    assert timed.value.code == "probe_timeout"

    def bad_exit(*_args, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="moov atom not found")
    with pytest.raises(ProbeError, match="moov atom"):
        FFprobe("ffprobe", runner=bad_exit).probe(tmp_path / "x.mp4")

    def malformed(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="{", stderr="")
    with pytest.raises(ProbeError) as invalid:
        FFprobe("ffprobe", runner=malformed).probe(tmp_path / "x.mp4")
    assert invalid.value.code == "invalid_probe"


def test_discover_folder_sorted_deduplicated_and_skips_symlink_dirs(tmp_path: Path) -> None:
    root = tmp_path / "media"
    (root / "nested").mkdir(parents=True)
    (root / "b.MOV").write_bytes(b"b")
    (root / "nested" / "a.mp4").write_bytes(b"a")
    (root / "ignore.mkv").write_bytes(b"x")
    try:
        (root / "cycle").symlink_to(root, target_is_directory=True)
    except OSError:
        pass
    files, errors = discover_files([root, root / "nested" / "a.mp4", tmp_path / "missing"])
    assert [path.name for path in files] == ["b.MOV", "a.mp4"]
    assert any(error.code == "unavailable" for error in errors)


def test_import_is_reference_only_detects_path_and_content_duplicates(tmp_path: Path) -> None:
    source = tmp_path / "lesson.mp4"
    source.write_bytes(b"source-media-original")
    before_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    before_stat = source.stat().st_mtime_ns
    project = ProjectStore.create(tmp_path / "lesson.localcut", "Lesson")

    class StubProbe:
        def version(self):
            return "ffprobe test"
        def probe(self, _path):
            return normalize_probe(probe_fixture())

    first = import_media(project, [source], probe=StubProbe())
    second = import_media(project, [source], probe=StubProbe())
    assert first["imported"][0]["status"] == "imported"
    assert second["imported"][0]["status"] == "duplicate"
    assert first["imported"][0]["assetId"] == second["imported"][0]["assetId"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before_hash
    assert source.stat().st_mtime_ns == before_stat
    assert not (project.root / "media" / source.name).exists()

    copy = tmp_path / "copy.mp4"
    copy.write_bytes(source.read_bytes())
    duplicate = project.find_duplicate_asset(copy)
    assert duplicate and duplicate["match"] == "content"


def test_import_reports_per_file_probe_errors_without_aborting(tmp_path: Path) -> None:
    good = tmp_path / "good.mp4"
    bad = tmp_path / "bad.mov"
    good.write_bytes(b"good")
    bad.write_bytes(b"bad")
    project = ProjectStore.create(tmp_path / "project", "Project")

    class MixedProbe:
        def version(self): return "mock"
        def probe(self, path):
            if path.name == "bad.mov":
                raise ProbeError("corrupt_or_unsupported", "Invalid atom")
            return normalize_probe(probe_fixture())

    result = import_media(project, [good, bad], probe=MixedProbe())
    assert len(result["imported"]) == 1
    assert result["errors"][0]["code"] == "corrupt_or_unsupported"


def test_managed_import_is_atomic_project_relative_and_leaves_original_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "tutorial [original].MP4"
    source.write_bytes(b"managed-copy-source")
    original = source.read_bytes()
    project = ProjectStore.create(tmp_path / "project.localcut", "Project")

    class StubProbe:
        def version(self): return "ffprobe test"
        def probe(self, _path): return normalize_probe(probe_fixture())

    result = import_media(project, [source], probe=StubProbe(), storage_mode="managed")
    assert result["imported"][0]["status"] == "imported"
    state = project.load_state()
    asset = state["assets"][0]
    assert asset["path_mode"] == "project-relative"
    managed = project.root / asset["original_uri"]
    assert managed.is_file() and managed.read_bytes() == original
    assert managed.parent == project.root / "media"
    assert source.read_bytes() == original
    duplicate = import_media(project, [source], probe=StubProbe(), storage_mode="managed")
    assert duplicate["imported"][0]["status"] == "duplicate"
    assert len(list((project.root / "media").iterdir())) == 1


def test_managed_import_upgrades_existing_link_without_changing_asset_id(tmp_path: Path) -> None:
    source = tmp_path / "linked.mp4"; source.write_bytes(b"linked-source")
    project = ProjectStore.create(tmp_path / "upgrade.localcut", "Upgrade")
    class StubProbe:
        def version(self): return "ffprobe test"
        def probe(self, _path): return normalize_probe(probe_fixture())
    linked = import_media(project,[source],probe=StubProbe())["imported"][0]["assetId"]
    managed = import_media(project,[source],probe=StubProbe(),storage_mode="managed")["imported"][0]
    assert managed["assetId"] == linked and managed["convertedExistingAsset"] is True
    asset=project.load_state()["assets"][0]
    assert asset["path_mode"] == "project-relative"
    assert (project.root / asset["original_uri"]).read_bytes() == source.read_bytes()
    assert source.read_bytes() == b"linked-source"


def test_changed_file_at_same_path_is_not_silently_classified_as_duplicate(tmp_path: Path) -> None:
    source=tmp_path/"lesson.mp4"; source.write_bytes(b"first version")
    project=ProjectStore.create(tmp_path/"changed.localcut","Changed")
    class StubProbe:
        def version(self): return "ffprobe test"
        def probe(self,_path): return normalize_probe(probe_fixture())
    first=import_media(project,[source],probe=StubProbe())
    asset_id=first["imported"][0]["assetId"]
    source.write_bytes(b"different second version")
    result=import_media(project,[source],probe=StubProbe())
    assert result["imported"] == []
    assert result["errors"][0]["code"] == "source_changed"
    assert "Use Relink source" in result["errors"][0]["message"]
    assert project.load_state()["assets"][0]["id"] == asset_id


def test_managed_media_resolves_after_project_folder_moves(tmp_path: Path) -> None:
    source=tmp_path/"original.mp4"; source.write_bytes(b"portable-media")
    root=tmp_path/"before"/"lesson.localcut"
    project=ProjectStore.create(root,"Portable")
    class StubProbe:
        def version(self): return "ffprobe test"
        def probe(self,_path): return normalize_probe(probe_fixture())
    result=import_media(project,[source],probe=StubProbe(),storage_mode="managed")
    asset_id=result["imported"][0]["assetId"]
    moved_root=tmp_path/"after"/"lesson.localcut"
    moved_root.parent.mkdir(parents=True)
    shutil.move(str(root),str(moved_root))
    reopened=ProjectStore.open(moved_root)
    with _connect(reopened.database_path) as db:
        asset=db.execute("SELECT * FROM assets WHERE id=?",(asset_id,)).fetchone()
    from localcut_service.proxies import source_path
    resolved=source_path(reopened,asset)
    assert resolved == moved_root / asset["original_uri"]
    assert resolved.read_bytes() == b"portable-media"
    assert reopened.load_state()["assets"][0]["id"] == asset_id


def test_relink_requires_matching_fingerprint_or_explicit_replacement_and_invalidates_derivatives(tmp_path: Path) -> None:
    source = tmp_path / "original.mov"
    moved = tmp_path / "moved.mov"
    replacement = tmp_path / "replacement.mov"
    source.write_bytes(b"same-content")
    moved.write_bytes(b"same-content")
    replacement.write_bytes(b"different-content")
    project = ProjectStore.create(tmp_path / "relink.localcut", "Relink")
    asset_id = project.import_asset_reference(source, {"old": True}, probe_tool="fixture", probe_version="1", duration_ticks=12_500_000)["assetId"]
    artifact_path = project.root / "cache" / "proxy.mp4"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(b"proxy")
    from localcut_service.persistence import _sha256_file
    artifact_id=project.register_artifact(asset_id=asset_id, kind="proxy", relative_path=artifact_path.relative_to(project.root).as_posix(), source_fingerprint=_sha256_file(source), generator_id="fixture", generator_version="1", checksum_sha256=_sha256_file(artifact_path))
    project.add_transcript(asset_id=asset_id,language="en",words=[{"text":"hello","sourceInTicks":1_000_000,"sourceOutTicks":1_100_000}],generator_id="fixture-asr",generator_version="1",source_fingerprint=_sha256_file(source),artifact_id=artifact_id)
    project.add_analysis(asset_id=asset_id,kind="silence",source_in_ticks=1_000_000,source_out_ticks=2_000_000,result={"suggestionOnly":True},generator_id="fixture-detector",generator_version="1",source_fingerprint=_sha256_file(source),artifact_id=artifact_id)

    class StubProbe:
        def version(self): return "ffprobe test"
        def probe(self, _path): return normalize_probe(probe_fixture())

    exact = relink_media(project, asset_id, moved, probe=StubProbe())
    assert exact["status"] == "relinked"
    assert project.load_state()["assets"][0]["last_known_path"] == str(moved.resolve())
    confirmation = relink_media(project, asset_id, replacement, probe=StubProbe())
    assert confirmation["status"] == "confirmation_required"
    replaced = relink_media(project, asset_id, replacement, probe=StubProbe(), allow_changed=True)
    assert replaced["status"] == "replaced" and replaced["invalidatedArtifacts"]
    with _connect(project.database_path) as db:
        assert db.execute("SELECT status FROM artifacts WHERE kind='proxy'").fetchone()[0] == "stale"
        assert db.execute("SELECT status FROM transcripts").fetchone()[0] == "stale"
        assert db.execute("SELECT status FROM analyses").fetchone()[0] == "stale"
        assert db.execute("SELECT sha256 FROM assets WHERE id=?", (asset_id,)).fetchone()[0] == _sha256_file(replacement)
    assert source.read_bytes() == b"same-content"
    assert moved.read_bytes() == b"same-content"


def test_changed_relink_cannot_shorten_existing_timeline_source_ranges(tmp_path: Path) -> None:
    source = tmp_path / "long.mp4"; source.write_bytes(b"long")
    replacement = tmp_path / "short.mp4"; replacement.write_bytes(b"short")
    project = ProjectStore.create(tmp_path / "shorten.localcut", "Shorten")
    asset_id = project.import_asset_reference(source, {}, probe_tool="fixture", probe_version="1", duration_ticks=12_500_000)["assetId"]
    project.autosave(project.project_id, {"timelineItems": [{"id":"clip","assetId":asset_id,"itemType":"clip","trackId":"video","timelineStartTicks":0,"timelineDurationTicks":10_000_000,"sourceInTicks":0,"sourceOutTicks":10_000_000}]})
    class ShortProbe:
        def version(self): return "fixture"
        def probe(self, _path):
            data=normalize_probe(probe_fixture()); data["durationTicks"]=5_000_000; data["durationSeconds"]=5
            return data
    with pytest.raises(Exception, match="shorter than a source range"):
        relink_media(project, asset_id, replacement, probe=ShortProbe(), allow_changed=True)
