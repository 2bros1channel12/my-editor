import json
from pathlib import Path

import pytest

from localcut_service.maintenance import cache_snapshot, clear_generated_cache
from localcut_service.persistence import ProjectError, ProjectStore, _connect, _sha256_file


def fixture_project(tmp_path: Path):
    store = ProjectStore.create(tmp_path / "project", "Cache maintenance")
    source = tmp_path / "lesson.mov"
    source.write_bytes(b"immutable original bytes")
    asset_id = store.add_asset(source)

    proxy = store.root / "cache" / "proxies" / "proxy-1"
    proxy.mkdir(parents=True)
    (proxy / "proxy.mp4").write_bytes(b"proxy bytes")
    (proxy / "manifest.json").write_text(json.dumps({"fixture": True}))
    proxy_id = store.register_artifact(asset_id=asset_id, kind="proxy", relative_path=proxy.relative_to(store.root).as_posix(), source_fingerprint=_sha256_file(source), generator_id="fixture", generator_version="1", checksum_sha256=_sha256_file(proxy))

    analysis = store.root / "cache" / "analysis" / "silence" / "analysis-1.json"
    analysis.parent.mkdir(parents=True)
    analysis.write_text('{"suggestionOnly":true}')
    analysis_id = store.register_artifact(asset_id=asset_id, kind="analysis:silence", relative_path=analysis.relative_to(store.root).as_posix(), source_fingerprint=_sha256_file(source), generator_id="fixture", generator_version="1", checksum_sha256=_sha256_file(analysis))
    store.add_analysis(asset_id=asset_id, kind="silence", source_in_ticks=1, source_out_ticks=2, result={"suggestionOnly": True}, generator_id="fixture", generator_version="1", source_fingerprint=_sha256_file(source), artifact_id=analysis_id)

    transcript_path = store.root / "cache" / "transcripts" / "inference.json"
    transcript_path.parent.mkdir(parents=True)
    transcript_path.write_text('{"originalInference":true}')
    transcript_artifact = store.register_artifact(asset_id=asset_id, kind="transcript", relative_path=transcript_path.relative_to(store.root).as_posix(), source_fingerprint=_sha256_file(source), generator_id="fixture-asr", generator_version="1", checksum_sha256=_sha256_file(transcript_path))
    transcript_id = store.add_transcript(asset_id=asset_id, language="en", words=[], segments=[], generator_id="fixture-asr", generator_version="1", source_fingerprint=_sha256_file(source), artifact_id=transcript_artifact)

    export = store.root / "exports" / "final.mp4"
    export.parent.mkdir(parents=True, exist_ok=True)
    export.write_bytes(b"completed export")
    export_id = store.register_artifact(asset_id=asset_id, kind="export", relative_path=export.relative_to(store.root).as_posix(), source_fingerprint=_sha256_file(source), generator_id="fixture-render", generator_version="1", checksum_sha256=_sha256_file(export))
    return store, source, proxy, analysis, transcript_path, export, proxy_id, analysis_id, transcript_id, export_id


def test_cache_cleanup_requires_review_and_preserves_user_content(tmp_path):
    store, source, proxy, analysis, transcript_path, export, proxy_id, analysis_id, transcript_id, export_id = fixture_project(tmp_path)
    before = cache_snapshot(store)
    assert before["canClear"] is True
    assert before["artifactCount"] == 2
    assert before["totalBytes"] == sum(item["sizeBytes"] for item in before["entries"])
    assert {item["relativePath"] for item in before["entries"]} == {
        "cache/proxies/proxy-1", "cache/analysis/silence/analysis-1.json",
    }

    result = clear_generated_cache(store, before["confirmationToken"])
    assert result["clearedCount"] == 2
    assert result["failures"] == []
    assert not proxy.exists()
    assert not analysis.exists()
    assert source.read_bytes() == b"immutable original bytes"
    assert transcript_path.is_file() and store.get_transcript(transcript_id)["status"] == "ready"
    assert export.read_bytes() == b"completed export"
    assert result["snapshot"]["entries"] == []
    with _connect(store.database_path) as db:
        status = {row["id"]: row["status"] for row in db.execute("SELECT id,status FROM artifacts")}
        assert status[proxy_id] == status[analysis_id] == "stale"
        assert status[export_id] == "ready"
        assert db.execute("SELECT status FROM analyses WHERE artifact_id=?", (analysis_id,)).fetchone()[0] == "stale"
    assert store.list_analyses(asset_id=store.load_state()["assets"][0]["id"]) == []


def test_cache_cleanup_rejects_stale_confirmation_token(tmp_path):
    store, _, _, _, _, _, _, _, _, _ = fixture_project(tmp_path)
    old = cache_snapshot(store)
    extra = store.root / "cache" / "proxies" / "proxy-2"
    extra.mkdir(parents=True)
    (extra / "proxy.mp4").write_bytes(b"new cache")
    store.register_artifact(asset_id=None, kind="proxy", relative_path=extra.relative_to(store.root).as_posix(), source_fingerprint="f" * 64, generator_id="fixture", generator_version="1", checksum_sha256=_sha256_file(extra))
    with pytest.raises(ProjectError, match="changed after review"):
        clear_generated_cache(store, old["confirmationToken"])
    assert extra.exists()


def test_cache_cleanup_refuses_active_jobs(tmp_path):
    store, _, proxy, _, _, _, _, _, _, _ = fixture_project(tmp_path)
    with _connect(store.database_path) as db:
        db.execute(
            "INSERT INTO jobs(id,project_id,kind,state,progress,resumable,checkpoint_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("active-job", store.project_id, "proxy", "running", .4, 1, "{}", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        db.commit()
    snapshot = cache_snapshot(store)
    assert snapshot["canClear"] is False
    assert snapshot["confirmationToken"] is None
    with pytest.raises(ProjectError, match="jobs to finish"):
        clear_generated_cache(store, "0" * 64)
    assert proxy.exists()


def test_cache_cleanup_rejects_unsafe_paths_without_touching_external_files(tmp_path):
    store, source, _, _, _, _, _, _, _, _ = fixture_project(tmp_path)
    with _connect(store.database_path) as db:
        db.execute("UPDATE artifacts SET relative_path=? WHERE kind='proxy'", ("media/" + source.name,))
        db.commit()
    with pytest.raises(ProjectError, match="unsafe project-relative path"):
        cache_snapshot(store)
    assert source.read_bytes() == b"immutable original bytes"
