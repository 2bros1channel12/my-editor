from __future__ import annotations

import hashlib
import json
import localcut_service.persistence as persistence
import sqlite3
import subprocess
import sys
from pathlib import Path
import os

import pytest

from localcut_service.persistence import (
    CorruptProjectError,
    DATABASE_SCHEMA_VERSION,
    ProjectError,
    ProjectCatalog,
    ProjectStore,
    UnsupportedSchemaError,
    atomic_write,
    migrate_database,
)


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "lesson.mp4"
    path.write_bytes(b"immutable original media\x00\x01")
    return path


def test_create_schema_and_migrate_empty_database(tmp_path: Path) -> None:
    root = tmp_path / "Lesson.localcut"
    store = ProjectStore.create(root, "Lesson", {"fps": 30})
    with sqlite3.connect(store.database_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == DATABASE_SCHEMA_VERSION
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"projects", "assets", "media_metadata", "artifacts", "transcripts", "transcript_words", "analyses", "briefs", "style_traits", "edit_plans", "timelines", "timeline_items", "captions", "jobs", "models", "exports", "autosaves"} <= tables
        assert db.execute("SELECT COUNT(*) FROM migrations_applied").fetchone()[0] == DATABASE_SCHEMA_VERSION
        assert {"input_json", "output_json", "stage", "attempts", "cancel_requested", "dedupe_key", "error_json"} <= {row[1] for row in db.execute("PRAGMA table_info(jobs)")}


def test_nondestructive_source_reference_and_timeline_roundtrip(tmp_path: Path) -> None:
    source = _source(tmp_path)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    root = tmp_path / "project"
    store = ProjectStore.create(root, "Lesson")
    asset_id = store.add_asset(source, {"codec": "test"})
    source_range = {"id": "clip-1", "assetId": asset_id, "itemType": "clip", "trackId": "video-1", "timelineStartTicks": 0, "timelineDurationTicks": 3000, "sourceInTicks": 100, "sourceOutTicks": 3100}
    revision = store.autosave(store.project_id, {"settings": {"aspectRatio": "16:9"}, "timelineItems": [source_range]})
    reopened = ProjectStore.open(root)
    state = reopened.load_state()
    assert state["revision"] == revision
    assert state["timelineItems"][0] == {**source_range, "properties": {}}
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    with sqlite3.connect(reopened.database_path) as db:
        uri, mode, fingerprint = db.execute("SELECT original_uri,path_mode,sha256 FROM assets WHERE id=?", (asset_id,)).fetchone()
    assert mode == "file-uri"
    assert uri.startswith("file:")
    assert fingerprint == before


def test_caption_track_and_caption_text_reopen_with_timeline(tmp_path: Path) -> None:
    store = ProjectStore.create(tmp_path / "project", "Lesson")
    asset = store.add_asset(_source(tmp_path))
    clip = {"id": "clip", "assetId": asset, "itemType": "clip", "trackId": "video", "timelineStartTicks": 0, "timelineDurationTicks": 2_000_000, "sourceInTicks": 0, "sourceOutTicks": 2_000_000, "properties": {"linkedAudio": True}}
    caption = {"id": "caption", "assetId": asset, "itemType": "caption", "trackId": "captions", "timelineStartTicks": 0, "timelineDurationTicks": 2_000_000, "sourceInTicks": 0, "sourceOutTicks": 2_000_000, "properties": {"text": "Hello lesson", "linkedClipId": "clip"}}
    store.autosave(store.project_id, {"timelineItems": [clip, caption], "captions": [{"id": "caption-data", "timelineItemId": "caption", "text": "Hello lesson", "language": "en", "style": {"position": "bottom"}}]})
    reopened = ProjectStore.open(store.root).load_state()
    assert len(reopened["timelineItems"]) == 2
    assert reopened["timelineItems"][0]["properties"]["text"] == "Hello lesson"
    assert reopened["captions"] == [{"id": "caption-data", "timelineItemId": "caption", "text": "Hello lesson", "language": "en", "style": {"position": "bottom"}}]


def test_timeline_requires_source_range_and_existing_asset(tmp_path: Path) -> None:
    store = ProjectStore.create(tmp_path / "project", "Lesson")
    invalid = {"id": "x", "itemType": "clip", "trackId": "v", "timelineStartTicks": 0, "timelineDurationTicks": 1}
    with pytest.raises(ValueError, match="source asset"):
        store.autosave(store.project_id, {"timelineItems": [invalid]})
    assert store.load_state()["timelineItems"] == []


def test_timeline_source_range_cannot_exceed_media_duration(tmp_path: Path) -> None:
    source = _source(tmp_path)
    store = ProjectStore.create(tmp_path / "project", "Lesson")
    asset_id = store.add_asset(source)
    store.set_media_metadata(asset_id, {"duration": 10}, probe_tool="stub", probe_version="1", duration_ticks=1000)
    item = {"id": "clip", "assetId": asset_id, "itemType": "clip", "trackId": "v", "timelineStartTicks": 0, "timelineDurationTicks": 500, "sourceInTicks": 0, "sourceOutTicks": 1001}
    with pytest.raises(ValueError, match="exceeds"):
        store.autosave(store.project_id, {"timelineItems": [item]})
    assert store.load_state()["timelineItems"] == []


def test_manifest_atomic_write_preserves_old_value_on_interruption(tmp_path: Path) -> None:
    target = tmp_path / "project.json"
    target.write_text('{"generation":1}', encoding="utf-8")

    def interrupt(_temporary: Path, _target: Path) -> None:
        raise OSError("simulated power loss before rename")

    with pytest.raises(OSError):
        atomic_write(target, b'{"generation":2}', before_replace=interrupt)
    assert json.loads(target.read_text(encoding="utf-8")) == {"generation": 1}
    assert list(tmp_path.glob("*.tmp")) == []


def test_migration_rolls_back_and_newer_schema_is_refused(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "rollback.sqlite3")
    connection.execute("CREATE TABLE projects(id TEXT PRIMARY KEY)")

    def fail_migration(db: sqlite3.Connection) -> None:
        db.execute("CREATE TABLE partial_table(id TEXT)")
        raise RuntimeError("injected migration fault")

    import localcut_service.persistence as persistence
    original = persistence.MIGRATIONS[1]
    persistence.MIGRATIONS[1] = fail_migration
    try:
        with pytest.raises(RuntimeError):
            migrate_database(connection)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='partial_table'").fetchone() is None
    finally:
        persistence.MIGRATIONS[1] = original
        connection.close()

    newer = sqlite3.connect(tmp_path / "newer.sqlite3")
    newer.execute(f"PRAGMA user_version={DATABASE_SCHEMA_VERSION + 1}")
    with pytest.raises(UnsupportedSchemaError):
        migrate_database(newer)
    newer.close()


def test_migration_from_legacy_schema_zero(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA user_version=0")
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        migrate_database(db)
        assert db.execute("SELECT COUNT(*) FROM migrations_applied").fetchone()[0] == DATABASE_SCHEMA_VERSION


def test_schema_v2_migration_preserves_existing_job_rows(tmp_path: Path) -> None:
    import localcut_service.persistence as persistence
    db_path = tmp_path / "schema-v1.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        persistence._migration_1(db)
        db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES ('p','Lesson','now','now')")
        db.execute("INSERT INTO jobs(id,project_id,kind,state,progress,created_at,updated_at) VALUES ('j','p','proxy','queued',0,'now','now')")
        db.execute("INSERT INTO migrations_applied(version,applied_at) VALUES (1,'now')")
        db.execute("PRAGMA user_version=1")
        db.commit()
        migrate_database(db)
        row = db.execute("SELECT id,state,input_json,attempts FROM jobs WHERE id='j'").fetchone()
        assert tuple(row) == ("j", "queued", "{}", 0)
        assert db.execute("PRAGMA user_version").fetchone()[0] == persistence.DATABASE_SCHEMA_VERSION


def test_last_project_pointer_reopens_portably_relative_to_catalog(tmp_path: Path) -> None:
    data = tmp_path / "appdata"
    root = tmp_path / "projects" / "lesson"
    store = ProjectStore.create(root, "Lesson")
    catalog = ProjectCatalog(data)
    catalog.remember(root)
    reopened = catalog.reopen_last()
    assert reopened is not None
    assert reopened.project_id == store.project_id
    assert json.loads(catalog.pointer_path.read_text(encoding="utf-8"))["path"] == "../projects/lesson"


def test_checkpoint_restores_corrupt_database_and_preserves_bad_generation(tmp_path: Path) -> None:
    root = tmp_path / "project"
    store = ProjectStore.create(root, "Lesson")
    checkpoint = store.checkpoint()
    store.database_path.write_bytes(b"not a sqlite database")
    reopened = ProjectStore.open(root)
    assert reopened.project_id == store.project_id
    assert reopened.recovery_report is not None
    assert reopened.recovery_report["checkpointRevision"] == 0
    assert "may be missing" in reopened.recovery_report["message"]
    assert checkpoint.exists()
    assert list((root / "recovery").glob("project-corrupt-*.sqlite3"))
    with sqlite3.connect(checkpoint) as db:
        assert db.execute("SELECT state_revision FROM projects WHERE id=?", (store.project_id,)).fetchone()[0] == 0


def test_twenty_forced_process_terminations_preserve_last_committed_edit(tmp_path: Path) -> None:
    """Exercise SQLite recovery across real process death, not an exception hook."""
    import time

    service_src = str(Path(__file__).parents[1] / "src")
    env = os.environ.copy()
    env["PYTHONPATH"] = service_src + os.pathsep + env.get("PYTHONPATH", "")
    child = r"""
import json, sys, time
from pathlib import Path
from localcut_service.persistence import ProjectStore, _connect
root, marker = Path(sys.argv[1]), Path(sys.argv[2])
store = ProjectStore.open(root)
state = store.load_state()
accepted = dict(state['timelineItems'][0])
accepted.update({'id':'clip-latest-accepted-edit','timelineStartTicks':1_000_000,'sourceInTicks':250_000,'sourceOutTicks':1_250_000,'timelineDurationTicks':1_000_000})
state['timelineItems'].append(accepted)
accepted_revision = store.autosave(store.project_id, state)
with _connect(store.database_path) as db:
    db.execute("BEGIN IMMEDIATE")
    db.execute("UPDATE projects SET settings_json=? WHERE id=?", ('{\"uncommittedCrashTrial\":true}', store.project_id))
    marker.write_text(json.dumps({'acceptedRevision':accepted_revision,'acceptedAt':time.monotonic()}), encoding="utf-8")
    time.sleep(60)
"""

    lost_accepted_seconds: list[float] = []
    recovered_accepted_edits = 0

    for trial in range(20):
        root = tmp_path / f"crash trial {trial:02d} [雪]"
        store = ProjectStore.create(root, f"Crash trial {trial}")
        source = tmp_path / f"source {trial}.mp4"
        source.write_bytes(b"immutable source fixture")
        asset_id = store.add_asset(source)
        transcript_id = store.add_transcript(
            asset_id=asset_id,
            language="en",
            segments=[{"id": "seg", "sourceInTicks": 0, "sourceOutTicks": 1_000_000, "text": "accepted transcript"}],
            words=[{"id": "word", "segmentId": "seg", "sourceInTicks": 0, "sourceOutTicks": 500_000, "text": "accepted"}],
            generator_id="fixture-asr",
            generator_version="1",
            source_fingerprint=hashlib.sha256(source.read_bytes()).hexdigest(),
        )
        clip = {"id": "clip", "assetId": asset_id, "itemType": "clip", "trackId": "video", "timelineStartTicks": 0, "timelineDurationTicks": 1_000_000, "sourceInTicks": 0, "sourceOutTicks": 1_000_000}
        store.autosave(store.project_id, {"timelineItems": [clip]})
        marker = tmp_path / f"crash-ready-{trial:02d}"
        process = subprocess.Popen([sys.executable, "-c", child, str(root), str(marker)], env=env)
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert marker.exists(), f"trial {trial}: child did not reach its uncommitted transaction"
            process.kill()
            process.wait(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)

        reopened = ProjectStore.open(root)
        state = reopened.load_state()
        accepted = {**clip, "id": "clip-latest-accepted-edit", "timelineStartTicks": 1_000_000, "sourceInTicks": 250_000, "sourceOutTicks": 1_250_000, "properties": {}}
        marker_data = json.loads(marker.read_text(encoding="utf-8"))
        assert state["timelineItems"][0] == {**clip, "properties": {}}, f"trial {trial}: baseline committed timeline was not intact"
        if accepted in state["timelineItems"]:
            recovered_accepted_edits += 1
            lost_accepted_seconds.append(0.0)
        else:
            lost_accepted_seconds.append(max(0.0, time.monotonic() - marker_data["acceptedAt"]))
        saved = reopened.get_transcript(transcript_id)
        assert saved["segments"][0]["originalText"] == "accepted transcript", f"trial {trial}: transcript was not intact"
        with sqlite3.connect(reopened.database_path) as db:
            assert db.execute("SELECT state_revision FROM projects WHERE id=?", (store.project_id,)).fetchone()[0] >= marker_data["acceptedRevision"], f"trial {trial}: autosave revision was lost"
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", f"trial {trial}: database integrity failed"
    assert recovered_accepted_edits == 20, f"only {recovered_accepted_edits}/20 latest accepted edits were recovered"
    assert len(lost_accepted_seconds) == 20
    assert max(lost_accepted_seconds, default=0.0) == 0, "a forced-process-termination trial lost an accepted edit"


def test_autosave_revision_survives_reopen(tmp_path: Path) -> None:
    root = tmp_path / "project"
    store = ProjectStore.create(root, "Lesson")
    assert store.autosave(store.project_id, {"settings": {"frameRate": 25}}) == 1
    assert ProjectStore.open(root).load_state()["settings"] == {"frameRate": 25}


def test_timeline_history_returns_distinct_checksummed_prior_snapshots(tmp_path: Path) -> None:
    source = _source(tmp_path)
    store = ProjectStore.create(tmp_path / "timeline-history", "Timeline history")
    asset_id = store.add_asset(source)
    first = {"id": "clip", "assetId": asset_id, "itemType": "clip", "trackId": "video", "timelineStartTicks": 0, "timelineDurationTicks": 1_000_000, "sourceInTicks": 0, "sourceOutTicks": 1_000_000, "properties": {"cropPreset": "full_frame"}}
    second = {**first, "sourceInTicks": 250_000, "sourceOutTicks": 1_250_000}
    store.autosave(store.project_id, {"timelineItems": [first], "captions": []})
    store.autosave(store.project_id, {"timelineItems": [second], "captions": []})
    result = ProjectStore.open(store.root).timeline_history()
    assert result["snapshots"][-1] == {"items": [{**first, "properties": {"cropPreset": "full_frame"}}], "captions": []}
    assert result["snapshots"][-2] == {"items": [], "captions": []}
    assert result["truncated"] is False


def test_autosave_upserts_current_job_without_dropping_history(tmp_path: Path) -> None:
    store = ProjectStore.create(tmp_path / "project", "Lesson")
    job = {"id": "job-1", "kind": "proxy", "state": "running", "progress": 0.2, "resumable": False, "checkpoint": {"stage": 1}}
    store.autosave(store.project_id, {"jobs": [job]})
    changed = {**job, "state": "completed", "progress": 1.0}
    store.autosave(store.project_id, {"jobs": [changed]})
    with sqlite3.connect(store.database_path) as db:
        rows = db.execute("SELECT id,state,progress FROM jobs").fetchall()
    assert rows == [("job-1", "completed", 1.0)]
    assert len(list((store.root / "recovery").glob("project-*-checkpoint-*.sqlite3"))) == 1


def test_recovery_checkpoints_are_scheduled_and_retained_within_bounds(tmp_path: Path) -> None:
    store = ProjectStore.create(tmp_path / "scheduled", "Scheduled")
    pattern = f"project-{store.project_id}-checkpoint-*.sqlite3"
    checkpoints = list((store.root / "recovery").glob(pattern))
    assert [path.name.rsplit("-", 1)[-1].removesuffix(".sqlite3") for path in checkpoints] == ["00000000"]

    for revision in range(1, 56):
        store.autosave(store.project_id, {"settings": {"revision": revision}})

    checkpoints = sorted((store.root / "recovery").glob(pattern))
    revisions = [int(path.stem.rsplit("-", 1)[-1]) for path in checkpoints]
    assert revisions == [10, 20, 30, 40, 50]
    assert len(checkpoints) <= persistence.RECOVERY_CHECKPOINT_RETENTION
    for path, revision in zip(checkpoints, revisions):
        with sqlite3.connect(path) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert db.execute("SELECT state_revision FROM projects WHERE id=?", (store.project_id,)).fetchone()[0] == revision


def test_recovery_checkpoint_runs_on_next_edit_after_age_limit(tmp_path: Path):
    store = ProjectStore.create(tmp_path / "aged", "Aged")
    initial = next((store.root / "recovery").glob(f"project-{store.project_id}-checkpoint-*.sqlite3"))
    old = initial.stat().st_mtime - persistence.RECOVERY_CHECKPOINT_MAX_AGE_SECONDS - 1
    os.utime(initial, (old, old))
    store.autosave(store.project_id, {"settings": {"changed": True}})
    checkpoints = list((store.root / "recovery").glob(f"project-{store.project_id}-checkpoint-*.sqlite3"))
    assert len(checkpoints) == 2
    latest = max(checkpoints, key=lambda path: int(path.stem.rsplit("-", 1)[-1]))
    with sqlite3.connect(latest) as db:
        assert db.execute("SELECT state_revision FROM projects WHERE id=?", (store.project_id,)).fetchone()[0] == 1


def test_transcript_review_edit_advances_recovery_and_survives_database_corruption(tmp_path: Path) -> None:
    store = ProjectStore.create(tmp_path / "transcript-recovery", "Transcript recovery")
    source = _source(tmp_path)
    asset_id = store.add_asset(source)
    fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
    transcript_id = store.add_transcript(
        asset_id=asset_id,
        language="en",
        segments=[{"id": "segment-1", "sourceInTicks": 0, "sourceOutTicks": 1_000_000, "text": "original inference"}],
        words=[{"id": "word-1", "segmentId": "segment-1", "sourceInTicks": 0, "sourceOutTicks": 1_000_000, "text": "original"}],
        generator_id="fixture-asr",
        generator_version="1",
        source_fingerprint=fingerprint,
    )
    initial = next((store.root / "recovery").glob(f"project-{store.project_id}-checkpoint-*.sqlite3"))
    old = initial.stat().st_mtime - persistence.RECOVERY_CHECKPOINT_MAX_AGE_SECONDS - 1
    os.utime(initial, (old, old))
    store.update_transcript_segment(transcript_id, "segment-1", "corrected review text")
    assert store._current_revision() == 2  # asset registration and review edit
    checkpoint = next((store.root / "recovery").glob(f"project-{store.project_id}-checkpoint-00000002.sqlite3"))
    store.database_path.write_bytes(b"damaged project database")

    recovered = ProjectStore.open(store.root)
    transcript = recovered.get_transcript(transcript_id)
    assert transcript["segments"][0]["originalText"] == "original inference"
    assert transcript["segments"][0]["editedText"] == "corrected review text"
    assert checkpoint.exists()


def test_generated_artifact_and_transcript_keep_detector_model_provenance(tmp_path: Path) -> None:
    store = ProjectStore.create(tmp_path / "project", "Lesson")
    source = _source(tmp_path)
    asset_id = store.add_asset(source)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    artifact_id = store.register_artifact(
        asset_id=asset_id,
        kind="proxy",
        relative_path="cache/proxies/source.mp4",
        source_fingerprint=source_hash,
        generator_id="proxy-generator",
        generator_version="2.1",
        config={"height": 540},
    )
    transcript_id = store.add_transcript(
        asset_id=asset_id,
        language="en",
        words=[{"sourceInTicks": 0, "sourceOutTicks": 30, "text": "Hello", "confidence": 0.97}],
        generator_id="whisper-adapter",
        generator_version="1",
        source_fingerprint=source_hash,
        model_id="whisper-small",
        model_version="2026-01",
    )
    with sqlite3.connect(store.database_path) as db:
        artifact = db.execute("SELECT generator_id,generator_version,model_id FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        transcript = db.execute("SELECT generator_id,model_id,model_version FROM transcripts WHERE id=?", (transcript_id,)).fetchone()
        word = db.execute("SELECT source_in_ticks,source_out_ticks,text FROM transcript_words WHERE transcript_id=?", (transcript_id,)).fetchone()
    assert artifact == ("proxy-generator", "2.1", None)
    assert transcript == ("whisper-adapter", "whisper-small", "2026-01")
    assert word == (0, 30, "Hello")


def test_brief_traits_and_edit_plan_keep_provenance(tmp_path: Path) -> None:
    store = ProjectStore.create(tmp_path / "project", "Lesson")
    brief_id = store.add_brief(
        "Teach a short lesson",
        constraints={"targetMinutes": 5},
        traits={"averageShotLength": 7.5, "captionDensity": "medium"},
        trait_origin="reference-derived",
        generator_id="trait-detector",
        generator_version="3",
        model_id="local-vision",
        model_version="2026.2",
    )
    plan_id = store.add_edit_plan({"operations": []}, status="proposed", rationale=[], generator_id="cut-rules", generator_version="1", model_id="local-llm", model_version="small-v2", brief_id=brief_id)
    with sqlite3.connect(store.database_path) as db:
        trait = db.execute("SELECT origin,generator_id,model_id,model_version FROM style_traits WHERE brief_id=? AND trait_key='averageShotLength'", (brief_id,)).fetchone()
        plan = db.execute("SELECT generator_id,model_id,model_version FROM edit_plans WHERE id=?", (plan_id,)).fetchone()
    assert trait == ("reference-derived", "trait-detector", "local-vision", "2026.2")
    assert plan == ("cut-rules", "local-llm", "small-v2")


def test_repeat_timeline_autosave_preserves_stable_ids_and_captions(tmp_path: Path) -> None:
    store=ProjectStore.create(tmp_path/"stable-project","Lesson")
    asset_id=store.add_asset(_source(tmp_path))
    clip={"id":"clip-stable","assetId":asset_id,"itemType":"clip","trackId":"video","timelineStartTicks":0,"timelineDurationTicks":1000,"sourceInTicks":0,"sourceOutTicks":1000,"properties":{}}
    caption_item={"id":"caption-stable","assetId":asset_id,"itemType":"caption","trackId":"captions","timelineStartTicks":0,"timelineDurationTicks":1000,"sourceInTicks":0,"sourceOutTicks":1000,"properties":{"linkedClipId":"clip-stable"}}
    state={"timelineItems":[clip,caption_item],"captions":[{"id":"caption-record","timelineItemId":"caption-stable","text":"Hello","language":"en","style":{}}]}
    store.autosave(store.project_id,state)
    store.autosave(store.project_id,state)
    loaded=ProjectStore.open(store.root).load_state()
    assert {item["id"] for item in loaded["timelineItems"]}=={"clip-stable","caption-stable"}
    assert loaded["captions"][0]["text"]=="Hello"
    with sqlite3.connect(store.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM timelines WHERE project_id=? AND is_current=1",(store.project_id,)).fetchone()[0]==1


def test_accept_proposal_applies_once_and_revision_guarded_undo_restores_timeline(tmp_path: Path) -> None:
    store=ProjectStore.create(tmp_path/"proposal-project","Lesson")
    asset_id=store.add_asset(_source(tmp_path))
    base={"settings":{},"timelineItems":[],"captions":[]}
    revision=store.autosave(store.project_id,base)
    operation={"id":"suggestion-1","type":"include_source_range","assetId":asset_id,"sourceInTicks":200,"sourceOutTicks":700,"status":"pending","rationale":"Keep this passage.","confidence":0.9}
    plan_id=store.add_edit_plan({"operations":[operation]},status="pending",rationale=[],generator_id="fixture",generator_version="1")
    accepted=store.decide_edit_plan_suggestion(plan_id,operation["id"],"accepted",expected_revision=revision)
    assert accepted["revision"]==revision+1
    state=store.load_state()
    assert [(item["sourceInTicks"],item["sourceOutTicks"]) for item in state["timelineItems"]]==[(200,700)]
    with pytest.raises(ValueError,match="requires the project revision"):
        store.decide_edit_plan_suggestion(plan_id,operation["id"],"accepted")
    undone=store.decide_edit_plan_suggestion(plan_id,operation["id"],"pending",expected_revision=state["revision"])
    assert undone["revision"]==revision+2
    assert store.load_state()["timelineItems"]==[]


def test_accept_exclusion_keeps_linked_caption_aligned_to_split_source_ranges(tmp_path: Path) -> None:
    store=ProjectStore.create(tmp_path/"caption-proposal-project","Lesson")
    asset_id=store.add_asset(_source(tmp_path))
    clip={"id":"clip","assetId":asset_id,"itemType":"clip","trackId":"video","timelineStartTicks":0,"timelineDurationTicks":1000,"sourceInTicks":0,"sourceOutTicks":1000,"properties":{}}
    caption_item={"id":"caption","assetId":asset_id,"itemType":"caption","trackId":"captions","timelineStartTicks":0,"timelineDurationTicks":1000,"sourceInTicks":0,"sourceOutTicks":1000,"properties":{"linkedClipId":"clip"}}
    captions=[{"id":"caption-text","timelineItemId":"caption","text":"A full caption","language":"en","style":{}}]
    revision=store.autosave(store.project_id,{"timelineItems":[clip,caption_item],"captions":captions})
    operation={"id":"exclude-1","type":"exclude_source_range","assetId":asset_id,"sourceInTicks":400,"sourceOutTicks":600,"status":"pending"}
    plan=store.add_edit_plan({"operations":[operation]},status="pending",rationale=[],generator_id="fixture",generator_version="1")
    store.decide_edit_plan_suggestion(plan,operation["id"],"accepted",expected_revision=revision)
    state=store.load_state()
    clips=sorted((item for item in state["timelineItems"] if item["itemType"]=="clip"),key=lambda item:item["sourceInTicks"])
    captions_items=[item for item in state["timelineItems"] if item["itemType"]=="caption"]
    assert [(item["sourceInTicks"],item["sourceOutTicks"]) for item in clips]==[(0,400),(600,1000)]
    assert len(captions_items)==2 and len(state["captions"])==2
    assert {item["properties"]["linkedClipId"] for item in captions_items}=={item["id"] for item in clips}
    store.decide_edit_plan_suggestion(plan,operation["id"],"pending",expected_revision=state["revision"])
    restored=store.load_state()
    assert {item["id"] for item in restored["timelineItems"]}=={"clip","caption"}
    assert next(item for item in restored["timelineItems"] if item["id"]=="clip")==clip
    assert next(item for item in restored["timelineItems"] if item["id"]=="caption")==caption_item
    assert restored["captions"]==captions


def test_reorder_requires_accepted_passages_preserves_source_ranges_and_is_undoable(tmp_path: Path) -> None:
    store=ProjectStore.create(tmp_path/"reorder-project","Lesson")
    source=_source(tmp_path)
    asset_id=store.import_asset_reference(source,{"durationSeconds":0.001},probe_tool="fixture",probe_version="1",duration_ticks=1000)["assetId"]
    clips=[]; caption_items=[]; caption_rows=[]
    for index,(start,end) in enumerate(((0,100),(100,300),(300,400)),start=1):
        suggestion_id=f"include-{index}"; clip_id=f"clip-{index}"; caption_id=f"cue-{index}"
        clips.append({"id":clip_id,"assetId":asset_id,"itemType":"clip","trackId":"video","timelineStartTicks":start,"timelineDurationTicks":end-start,"sourceInTicks":start,"sourceOutTicks":end,"properties":{"originSuggestionId":suggestion_id,"linkedAudio":True}})
        caption_items.append({"id":caption_id,"assetId":asset_id,"itemType":"caption","trackId":"captions","timelineStartTicks":start,"timelineDurationTicks":end-start,"sourceInTicks":start,"sourceOutTicks":end,"properties":{"linkedClipId":clip_id}})
        caption_rows.append({"id":f"caption-record-{index}","timelineItemId":caption_id,"text":f"Passage {index}","language":"en","style":{}})
    before_revision=store.autosave(store.project_id,{"timelineItems":clips+caption_items,"captions":caption_rows})
    include_ops=[{"id":f"include-{index}","type":"include_source_range","assetId":asset_id,"sourceInTicks":start,"sourceOutTicks":end,"status":"pending","rationale":"Keep this passage.","confidence":None} for index,(start,end) in enumerate(((0,100),(100,300),(300,400)),start=1)]
    order={"id":"order-1","type":"reorder_passages","assetId":asset_id,"sourceInTicks":0,"sourceOutTicks":400,"status":"pending","confidence":None,"rationale":"Use the preferred order.","orderedSuggestionIds":["include-2","include-3","include-1"],"requiresSuggestionIds":["include-1","include-2","include-3"]}
    plan_id=store.add_edit_plan({"operations":include_ops+[order]},status="pending",rationale=[],generator_id="fixture",generator_version="1")
    with pytest.raises(ProjectError,match="Accept every kept passage"):
        store.decide_edit_plan_suggestion(plan_id,"order-1","accepted",expected_revision=before_revision)
    for operation in include_ops:
        revision=store.load_state()["revision"]
        store.decide_edit_plan_suggestion(plan_id,operation["id"],"accepted",expected_revision=revision)
    before_order=store.load_state()
    accepted=store.decide_edit_plan_suggestion(plan_id,"order-1","accepted",expected_revision=before_order["revision"])
    reordered=store.load_state()
    starts={item["id"]:item["timelineStartTicks"] for item in reordered["timelineItems"] if item["itemType"]=="clip"}
    assert starts=={"clip-1":300,"clip-2":0,"clip-3":200}
    source_ranges={item["id"]:(item["sourceInTicks"],item["sourceOutTicks"]) for item in reordered["timelineItems"] if item["itemType"]=="clip"}
    assert source_ranges=={"clip-1":(0,100),"clip-2":(100,300),"clip-3":(300,400)}
    cue_starts={item["id"]:item["timelineStartTicks"] for item in reordered["timelineItems"] if item["itemType"]=="caption"}
    assert cue_starts=={"cue-1":300,"cue-2":0,"cue-3":200}
    assert all(item["properties"].get("orderSuggestionId")=="order-1" for item in reordered["timelineItems"] if item["itemType"]=="clip")
    with pytest.raises(ProjectError,match="Undo the accepted passage reordering"):
        store.decide_edit_plan_suggestion(plan_id,"include-1","pending",expected_revision=reordered["revision"])
    store.decide_edit_plan_suggestion(plan_id,"order-1","pending",expected_revision=reordered["revision"])
    restored=store.load_state()
    assert restored["timelineItems"]==before_order["timelineItems"]
    assert restored["captions"]==before_order["captions"]
    assert accepted["revision"]==reordered["revision"]

