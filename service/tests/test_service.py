import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from localcut_service.config import AppConfig, load_config
from localcut_service.server import dispatch, serve
from localcut_service.persistence import ProjectStore


def test_service_startup_ping_and_shutdown(tmp_path: Path) -> None:
    incoming = io.StringIO('{"protocolVersion":1,"requestId":1,"method":"ping","params":{}}\n')
    outgoing = io.StringIO()
    assert serve(incoming, outgoing, AppConfig(data_dir=tmp_path)) == 0
    response = json.loads(outgoing.getvalue())
    assert response["ok"] is True
    assert response["requestId"] == 1
    assert response["result"]["service"] == "localcut"
    assert (tmp_path / "logs" / "service.jsonl").exists()


def test_service_rejects_bad_protocol_without_exiting(tmp_path: Path) -> None:
    incoming = io.StringIO(
        '{"protocolVersion":2,"requestId":1,"method":"ping","params":{}}\n'
        '{"protocolVersion":1,"requestId":2,"method":"ping","params":{}}\n'
    )
    outgoing = io.StringIO()
    serve(incoming, outgoing, AppConfig(data_dir=tmp_path))
    responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    assert responses[0]["ok"] is False
    assert responses[1]["ok"] is True


def test_service_rejects_non_object_and_bad_request_id(tmp_path: Path) -> None:
    incoming = io.StringIO(
        '[]\n'
        '{"protocolVersion":1,"requestId":-1,"method":"ping","params":{}}\n'
        '{"protocolVersion":1,"requestId":0,"method":"ping","params":{}}\n'
        '{"protocolVersion":1,"method":"ping","params":{}}\n'
        '{"protocolVersion":1,"requestId":3,"method":"ping","params":{}}\n'
    )
    outgoing = io.StringIO()
    serve(incoming, outgoing, AppConfig(data_dir=tmp_path))
    responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    assert [response["ok"] for response in responses] == [False, False, False, False, True]
    assert [response["requestId"] for response in responses[:4]] == [1, 1, 1, 1]


def test_config_precedence_and_validation(tmp_path: Path, monkeypatch) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"dataDir": str(tmp_path / "from-file"), "logLevel": "WARNING"}), encoding="utf-8")
    monkeypatch.setenv("LOCAL_CUT_DATA_DIR", str(tmp_path / "from-env"))
    config = load_config(config_file)
    assert config.data_dir == tmp_path / "from-env"
    assert config.log_level == "WARNING"


def test_job_concurrency_config_is_bounded(tmp_path: Path, monkeypatch) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"jobConcurrency": 4}), encoding="utf-8")
    assert load_config(config_file).job_concurrency == 4
    config_file.write_text(json.dumps({"jobConcurrency": 0}), encoding="utf-8")
    with pytest.raises(ValueError, match="between 1 and 16"):
        load_config(config_file)


def test_health_method_has_independent_checks(tmp_path: Path) -> None:
    incoming = io.StringIO('{"protocolVersion":1,"requestId":3,"method":"health","params":{}}\n')
    outgoing = io.StringIO()
    serve(incoming, outgoing, AppConfig(data_dir=tmp_path, ffmpeg_path="missing-ffmpeg"))
    result = json.loads(outgoing.getvalue())["result"]
    assert result["service"]["available"] is True
    assert result["storage"]["available"] is True
    assert result["ffmpeg"]["available"] is False


def test_stdio_child_process_communication(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["LOCAL_CUT_DATA_DIR"] = str(tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-m", "localcut_service"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        cwd=Path(__file__).parents[1],
        env=env,
    )
    try:
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write('{"protocolVersion":1,"requestId":42,"method":"ping","params":{}}\n')
        process.stdin.flush()
        response = json.loads(process.stdout.readline())
        assert response["ok"] is True
        assert response["requestId"] == 42
        assert response["result"]["protocolVersion"] == 1
    finally:
        if process.stdin is not None:
            process.stdin.close()
        assert process.wait(timeout=5) == 0


def test_project_service_create_save_and_reopen(tmp_path: Path) -> None:
    config = AppConfig(data_dir=tmp_path / "appdata")
    project_path = tmp_path / "lesson.localcut"
    create = dispatch("project_create", {"path": str(project_path), "name": "Lesson", "settings": {"fps": 30}}, config)
    saved = dispatch("project_save", {"path": str(project_path), "state": {"settings": {"aspectRatio": "16:9"}, "timelineItems": []}}, config)
    reopened = dispatch("project_open_last", {}, config)
    assert saved["revision"] == 1
    assert reopened["projectId"] == create["projectId"]
    assert reopened["state"]["settings"] == {"aspectRatio": "16:9"}


def test_project_open_last_reports_checkpoint_recovery(tmp_path: Path) -> None:
    config = AppConfig(data_dir=tmp_path / "appdata")
    project_path = tmp_path / "recovered.localcut"
    dispatch("project_create", {"path": str(project_path), "name": "Recovered"}, config)
    store = ProjectStore.open(project_path)
    store.database_path.write_bytes(b"corrupt database")
    reopened = dispatch("project_open_last", {}, config)
    assert reopened["recovery"]["checkpointRevision"] == 0
    assert "may be missing" in reopened["recovery"]["message"]


def test_project_open_last_marks_missing_and_changed_source_media(tmp_path: Path) -> None:
    config = AppConfig(data_dir=tmp_path / "appdata")
    project_path = tmp_path / "media-status.localcut"
    dispatch("project_create", {"path": str(project_path), "name": "Media status"}, config)
    source = tmp_path / "lesson.mp4"
    source.write_bytes(b"original media")
    store = ProjectStore.open(project_path)
    asset_id = store.add_asset(source)

    available = dispatch("project_open_last", {}, config)["state"]["assets"][0]
    assert available["id"] == asset_id and available["sourceStatus"] == "available"
    source.write_bytes(b"modified media with a different size")
    changed = dispatch("project_open_last", {}, config)["state"]["assets"][0]
    assert changed["sourceStatus"] == "changed_since_import"
    source.unlink()
    missing = dispatch("project_open_last", {}, config)["state"]["assets"][0]
    assert missing["sourceStatus"] == "unavailable"


def test_brief_methods_parse_locally_save_visible_constraints_and_list(tmp_path: Path) -> None:
    config=AppConfig(data_dir=tmp_path/"appdata")
    project_path=tmp_path/"brief.localcut"
    dispatch("project_create",{"path":str(project_path),"name":"Brief"},config)
    parsed=dispatch("brief_parse",{"briefText":"Audience: developers\nGoal: Explain caches\nMust keep: invalidation"},config)
    assert parsed["constraints"]["audience"]=="developers"
    assert parsed["constraints"]["mustKeep"]==["invalidation"]
    saved=dispatch("brief_save",{"path":str(project_path),"briefText":"Explain caches","constraints":parsed["constraints"]},config)
    listed=dispatch("brief_list",{"path":str(project_path)},config)
    assert listed["briefs"][0]["id"]==saved["brief"]["id"]
    assert listed["briefs"][0]["constraints"]["objective"]=="Explain caches"


def test_analysis_list_warns_when_source_metadata_changes_or_disappears(tmp_path: Path) -> None:
    config=AppConfig(data_dir=tmp_path/"analysis-data")
    project_path=tmp_path/"analysis.localcut"
    store=ProjectStore.create(project_path,"Analysis")
    source=tmp_path/"lesson.mov"; source.write_bytes(b"original")
    asset_id=store.add_asset(source)
    current=dispatch("analysis_list",{"path":str(project_path),"assetId":asset_id},config)
    assert current["sourceStatus"]=="metadata_matches_import"
    source.write_bytes(b"replacement source with another size")
    changed=dispatch("analysis_list",{"path":str(project_path),"assetId":asset_id},config)
    assert changed["sourceStatus"]=="changed_since_import"
    source.unlink()
    missing=dispatch("analysis_list",{"path":str(project_path),"assetId":asset_id},config)
    assert missing["sourceStatus"]=="unavailable"


def test_analysis_list_exposes_warning_for_empty_incomplete_run(tmp_path: Path) -> None:
    from localcut_service.persistence import _sha256_file
    config = AppConfig(data_dir=tmp_path / "analysis-warning-data")
    project_path = tmp_path / "analysis-warning.localcut"
    store = ProjectStore.create(project_path, "Analysis")
    source = tmp_path / "lesson.mov"; source.write_bytes(b"source")
    asset_id = store.add_asset(source)
    result_path = store.root / "cache" / "analysis" / "repeated_takes" / "result.json"
    result_path.parent.mkdir(parents=True)
    result_path.write_text(json.dumps({"assetId": asset_id, "warnings": [{"code": "repeated_take_comparison_limit", "message": "Search was incomplete."}], "results": []}), encoding="utf-8")
    store.register_artifact(asset_id=asset_id, kind="analysis:repeated_takes", relative_path=result_path.relative_to(store.root).as_posix(), source_fingerprint=_sha256_file(source), generator_id="localcut-repeated_takes", generator_version="4", checksum_sha256=_sha256_file(result_path))
    listed = dispatch("analysis_list", {"path": str(project_path), "assetId": asset_id}, config)
    assert listed["results"] == []
    assert len(listed["warnings"]) == 1
    assert listed["warnings"][0]["detectorId"] == "repeated_takes"
    assert listed["warnings"][0]["code"] == "repeated_take_comparison_limit"
    assert listed["warnings"][0]["message"] == "Search was incomplete."


def test_analysis_list_reports_proxy_audio_and_transcript_prerequisites(tmp_path: Path) -> None:
    from localcut_service.persistence import _sha256_file
    config=AppConfig(data_dir=tmp_path/"analysis-prereq-data")
    project_path=tmp_path/"analysis-prereq.localcut"
    store=ProjectStore.create(project_path,"Analysis")
    source=tmp_path/"lesson.mp4"; source.write_bytes(b"source media")
    asset_id=store.add_asset(source)
    initial=dispatch("analysis_list",{"path":str(project_path),"assetId":asset_id},config)
    assert initial["prerequisites"]=={"proxyReady":False,"hasAudio":False,"transcriptReady":False,"detail":"Generate valid preview assets for this source in Projects."}

    bundle=store.root/"cache"/"proxies"/"fixture"; bundle.mkdir(parents=True)
    (bundle/"proxy.mp4").write_bytes(b"validated proxy bytes")
    (bundle/"analysis.wav").write_bytes(b"a"*128)
    (bundle/"manifest.json").write_text(json.dumps({"pipelineVersion":"preview-2","proxy":"proxy.mp4","analysis":{"path":"analysis.wav"}}),encoding="utf-8")
    proxy_id=store.register_artifact(asset_id=asset_id,kind="proxy",relative_path=bundle.relative_to(store.root).as_posix(),source_fingerprint=_sha256_file(source),generator_id="fixture-proxy",generator_version="1",checksum_sha256=_sha256_file(bundle))
    with_audio=dispatch("analysis_list",{"path":str(project_path),"assetId":asset_id},config)
    assert with_audio["prerequisites"]["proxyReady"] is True
    assert with_audio["prerequisites"]["hasAudio"] is True
    assert with_audio["prerequisites"]["transcriptReady"] is False

    transcript_file=store.root/"cache"/"transcript.json"; transcript_file.write_text("{}",encoding="utf-8")
    transcript_artifact=store.register_artifact(asset_id=asset_id,kind="transcript",relative_path=transcript_file.relative_to(store.root).as_posix(),source_fingerprint=_sha256_file(source),generator_id="fixture-asr",generator_version="1",checksum_sha256=_sha256_file(transcript_file))
    store.add_transcript(asset_id=asset_id,language="en",words=[],generator_id="fixture-asr",generator_version="1",source_fingerprint=_sha256_file(source),artifact_id=transcript_artifact)
    complete=dispatch("analysis_list",{"path":str(project_path),"assetId":asset_id},config)
    assert complete["prerequisites"]=={"proxyReady":True,"hasAudio":True,"transcriptReady":True,"detail":"Ready"}


def test_transcript_list_returns_bounded_source_timed_waveform(tmp_path: Path, monkeypatch) -> None:
    import localcut_service.server as server_module
    project_path = tmp_path / "waveform.localcut"
    store = ProjectStore.create(project_path, "Waveform")
    source = tmp_path / "lesson.mp4"
    source.write_bytes(b"fixture")
    asset_id = store.add_asset(source)
    bundle = tmp_path / "proxy-bundle"
    bundle.mkdir()
    (bundle / "mapping.json").write_text(json.dumps({"sourceMinusProxySeconds": "37/4", "sourceStartSeconds": "9"}), encoding="utf-8")
    peaks = [[-0.25, 0.5] for _ in range(2401)]
    waveform = {"audio": {"sampleRate": 16000, "samples": 16000, "sourceStartSeconds": "19/2"}, "peaks": peaks}
    (bundle / "waveform.json").write_text(json.dumps(waveform), encoding="utf-8")
    (bundle / "manifest.json").write_text(json.dumps({"mapping": "mapping.json", "waveform": "waveform.json"}), encoding="utf-8")
    monkeypatch.setattr(server_module, "ready_proxy", lambda *_args, **_kwargs: bundle)
    result = dispatch("transcript_list", {"path": str(project_path), "assetId": asset_id}, AppConfig(data_dir=tmp_path / "appdata"))
    assert result["sourceMinusProxySeconds"] == 0.25
    compact = result["waveform"]
    assert compact["sourceStartTicks"] == 500_000
    assert compact["durationTicks"] == 1_000_000
    assert compact["sampleCount"] == 16000
    assert compact["originalBinCount"] == 2401
    assert len(compact["peaks"]) <= 1200
    assert compact["peaks"][0] == [-0.25, 0.5]
    (bundle / "waveform.json").write_text("not a waveform", encoding="utf-8")
    degraded = dispatch("transcript_list", {"path": str(project_path), "assetId": asset_id}, AppConfig(data_dir=tmp_path / "appdata"))
    assert degraded["sourceMinusProxySeconds"] == 0.25
    assert degraded["waveform"] is None


def test_media_import_dispatch_uses_configured_ffprobe(tmp_path: Path, monkeypatch) -> None:
    import localcut_service.server as server
    project_path = tmp_path / "lesson.localcut"
    store = server.ProjectStore.create(project_path, "Lesson")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"mock")
    expected = {"imported": [], "errors": [], "candidateCount": 0}

    class StubProbe:
        def __init__(self, executable):
            assert executable == "configured-ffprobe"

    monkeypatch.setattr(server, "FFprobe", StubProbe)
    monkeypatch.setattr(server, "import_media", lambda actual_store, inputs, probe, storage_mode="linked": expected if actual_store.project_id == store.project_id and inputs == [str(source)] and isinstance(probe, StubProbe) and storage_mode == "linked" else None)
    result = dispatch("media_import", {"path": str(project_path), "inputs": [str(source)]}, AppConfig(data_dir=tmp_path, ffprobe_path="configured-ffprobe"))
    assert result is expected
