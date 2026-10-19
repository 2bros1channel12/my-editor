"""Process-level offline acceptance for core local project workflows."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid


OFFLINE_GUARD = r'''
import ipaddress, sys
def deny_external_network(event, args):
    if event == "socket.getaddrinfo":
        host = args[0]
        if host not in {"localhost", "127.0.0.1", "::1"}:
            raise PermissionError("Offline acceptance denied DNS lookup")
    elif event == "socket.connect":
        address = args[1]
        host = address[0] if isinstance(address, tuple) and address else ""
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost"
        if not loopback:
            raise PermissionError("Offline acceptance denied external connection")
sys.addaudithook(deny_external_network)
from localcut_service.server import serve
raise SystemExit(serve())
'''


OFFLINE_MEDIA_GUARD = r'''
import ipaddress, sys
def deny_external_network(event, args):
    if event == "socket.getaddrinfo":
        host = args[0]
        if host not in {"localhost", "127.0.0.1", "::1"}:
            raise PermissionError("Offline acceptance denied DNS lookup")
    elif event == "socket.connect":
        address = args[1]
        host = address[0] if isinstance(address, tuple) and address else ""
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost"
        if not loopback:
            raise PermissionError("Offline acceptance denied external connection")
sys.addaudithook(deny_external_network)
from localcut_service import server, transcription
from localcut_service.transcription import TranscriptionHandler

# This test adapter exercises the same local service/job/artifact path without
# downloading model weights. It does not claim to validate Whisper inference.
class FixtureSpeechBackend:
    name = "offline-fixture"
    version = "1"
    def transcribe(self, audio, model, output, context, language):
        context.check_cancelled()
        tokens = ["Welcome", "to", "this", "offline", "editing", "workflow", "today"]
        words = [
            {"text": token, "startSeconds": 0.15 + index * 0.2,
             "endSeconds": 0.30 + index * 0.2, "confidence": 0.95}
            for index, token in enumerate(tokens)
        ]
        return {"language": "en", "segments": [{
            "text": " ".join(tokens), "startSeconds": words[0]["startSeconds"],
            "endSeconds": words[-1]["endSeconds"], "confidence": 0.95,
            "words": words,
        }]}

default_handlers = server.handlers
def offline_handlers(config):
    configured = default_handlers(config)
    configured["transcription"] = TranscriptionHandler(FixtureSpeechBackend())
    return configured

server.handlers = offline_handlers
def fixture_model_status(store, model, **kwargs):
    return {"id": model, "ready": True, "integrity": "test-fixture"}
server.model_status = fixture_model_status
transcription.model_status = fixture_model_status
raise SystemExit(server.serve())
'''


def test_core_local_workflow_completes_with_external_network_denied(tmp_path: Path) -> None:
    data_dir = tmp_path / "application data"
    project_path = tmp_path / "lesson.localcut"
    env = os.environ.copy()
    env["LOCAL_CUT_DATA_DIR"] = str(data_dir)
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    process = subprocess.Popen(
        [sys.executable, "-c", OFFLINE_GUARD],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=Path(__file__).parents[1],
        env=env,
    )
    requests = [
        {"protocolVersion": 1, "requestId": 1, "method": "ping", "params": {}},
        {"protocolVersion": 1, "requestId": 2, "method": "health", "params": {}},
        {"protocolVersion": 1, "requestId": 3, "method": "capabilities", "params": {}},
        {"protocolVersion": 1, "requestId": 4, "method": "project_create", "params": {"path": str(project_path), "name": "Offline lesson", "settings": {}}},
        {"protocolVersion": 1, "requestId": 5, "method": "brief_parse", "params": {"briefText": "Goal: Explain local caching\nMust keep: invalidation"}},
        {"protocolVersion": 1, "requestId": 6, "method": "analysis_catalog", "params": {}},
        {"protocolVersion": 1, "requestId": 7, "method": "project_open_last", "params": {}},
    ]
    try:
        assert process.stdin is not None
        process.stdin.write("".join(json.dumps(request) + "\n" for request in requests))
        process.stdin.close()
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        responses = [json.loads(line) for line in stdout.splitlines() if line.strip()]
        assert len(responses) == len(requests), stderr
        assert all(response["ok"] for response in responses), responses
        assert all(response["requestId"] == expected["requestId"] for response, expected in zip(responses, requests, strict=True))
        assert responses[1]["result"]["service"]["available"] is True
        assert responses[1]["result"]["storage"]["available"] is True
        assert responses[3]["result"]["path"] == str(project_path)
        assert responses[4]["result"]["constraints"]["mustKeep"] == ["invalidation"]
        assert len(responses[5]["result"]["detectors"]) >= 1
        assert responses[6]["result"]["projectId"] == responses[3]["result"]["projectId"]
        assert (project_path / "project.json").is_file()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_real_whisper_transcription_job_completes_with_external_network_denied(tmp_path: Path) -> None:
    """Exercise actual local Whisper inference and artifact persistence offline."""
    import hashlib
    import wave
    import pytest

    names = ("LOCAL_CUT_TEST_WHISPER", "LOCAL_CUT_TEST_WHISPER_MODEL", "LOCAL_CUT_TEST_SPEECH")
    if not all(os.environ.get(name) for name in names):
        pytest.skip("Supply local Whisper CLI, tiny model, and speech WAV fixtures for real offline inference.")
    from localcut_service.persistence import ProjectStore, _sha256_file
    from localcut_service.proxies import PIPELINE_VERSION
    from localcut_service.transcription import CATALOG, REVISION, model_file

    cli, weights, speech = (Path(os.environ[name]).resolve(strict=True) for name in names)
    assert cli.is_file() and weights.is_file() and speech.is_file()
    assert weights.stat().st_size == CATALOG["tiny"][0]
    assert _sha256_file(weights) == CATALOG["tiny"][1]
    with wave.open(str(speech), "rb") as wav:
        duration_ticks = round(wav.getnframes() / wav.getframerate() * 1_000_000)
    source_digest = hashlib.sha256(speech.read_bytes()).hexdigest()

    project_path = tmp_path / "offline real Whisper project.localcut"
    store = ProjectStore.create(project_path, "Offline Whisper acceptance")
    asset_id = store.add_asset(speech, {"durationTicks": duration_ticks})
    proxy_bundle = project_path / "cache" / "proxies" / "prepared speech fixture"
    proxy_bundle.mkdir(parents=True)
    (proxy_bundle / "analysis.wav").write_bytes(speech.read_bytes())
    (proxy_bundle / "mapping.json").write_text(json.dumps({
        "sourceStartSeconds": "0", "sourceMinusProxySeconds": "0",
    }), encoding="utf-8")
    (proxy_bundle / "manifest.json").write_text(json.dumps({
        "pipelineVersion": PIPELINE_VERSION,
        "mapping": "mapping.json",
        "analysis": {"path": "analysis.wav", "sourceStartSeconds": "0"},
    }), encoding="utf-8")
    store.register_artifact(
        asset_id=asset_id, kind="proxy", relative_path=proxy_bundle.relative_to(project_path).as_posix(),
        source_fingerprint=source_digest, generator_id="prepared-local-test-audio", generator_version="1",
        checksum_sha256=_sha256_file(proxy_bundle),
    )
    model_path = model_file(store, "tiny")
    model_path.parent.mkdir(parents=True)
    shutil.copyfile(weights, model_path)

    data_dir = tmp_path / "offline real Whisper application data"
    env = os.environ.copy()
    env.update({
        "LOCAL_CUT_DATA_DIR": str(data_dir),
        "LOCAL_CUT_WHISPER_CLI": str(cli),
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
    })
    process = subprocess.Popen(
        [sys.executable, "-c", OFFLINE_GUARD], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=Path(__file__).parents[1], env=env,
    )
    request_id = 0

    def request(method: str, params: dict) -> dict:
        nonlocal request_id
        request_id += 1
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(json.dumps({"protocolVersion": 1, "requestId": request_id,
                                        "method": method, "params": params}) + "\n")
        process.stdin.flush()
        line = process.stdout.readline()
        if not line:
            if process.poll() is None:
                process.terminate()
            _, diagnostic = process.communicate(timeout=5)
            raise AssertionError(f"Service produced no response during {method}; exit={process.returncode}; stderr={diagnostic}")
        response = json.loads(line)
        assert response["requestId"] == request_id
        assert response["ok"], response.get("error")
        return response["result"]

    try:
        submitted = request("transcription_generate", {
            "path": str(project_path), "assetId": asset_id, "model": "tiny",
        })
        assert submitted["state"] in {"queued", "running", "completed"}, submitted
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            jobs = request("job_list", {"projectId": store.project_id, "limit": 20})["jobs"]
            job = next(row for row in jobs if row["id"] == submitted["id"])
            if job["state"] in {"completed", "failed", "cancelled"}:
                assert job["state"] == "completed", job
                break
            time.sleep(0.1)
        else:
            raise AssertionError("Real Whisper job did not complete within 120 seconds.")

        transcript = ProjectStore.open(project_path).list_transcripts(asset_id)[0]
        assert transcript["language"] == "en"
        assert len(transcript["words"]) >= 20
        normalized = " ".join(re.findall(r"[a-z]+", " ".join(segment["text"] for segment in transcript["segments"]).lower()))
        assert normalized == "and so my fellow americans ask not what your country can do for you ask what you can do for your country"
        assert all(0 <= word["sourceInTicks"] < word["sourceOutTicks"] <= duration_ticks for word in transcript["words"])
        assert transcript["modelId"] == "tiny" and transcript["modelVersion"] == REVISION
        assert transcript["generatorId"] == "whisper.cpp"
        assert hashlib.sha256(speech.read_bytes()).hexdigest() == source_digest
    finally:
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_local_media_transcript_proposal_edit_and_export_with_external_network_denied(tmp_path: Path) -> None:
    ffmpeg = os.environ.get("LOCAL_CUT_FFMPEG") or shutil.which("ffmpeg")
    ffprobe = os.environ.get("LOCAL_CUT_FFPROBE") or shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        import pytest
        pytest.skip("Offline media acceptance requires locally installed FFmpeg and FFprobe.")

    source = tmp_path / "lesson footage [offline].mp4"
    generated = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
         "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", str(source)], capture_output=True, text=True, timeout=60,
    )
    assert generated.returncode == 0, generated.stderr
    original_bytes = source.read_bytes()

    data_dir = tmp_path / "offline app data"
    project_path = tmp_path / "offline lesson.localcut"
    env = os.environ.copy()
    env.update({
        "LOCAL_CUT_DATA_DIR": str(data_dir),
        "LOCAL_CUT_FFMPEG": str(ffmpeg),
        "LOCAL_CUT_FFPROBE": str(ffprobe),
        "LOCAL_CUT_JOB_CONCURRENCY": "1",
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
    })
    process = subprocess.Popen(
        [sys.executable, "-c", OFFLINE_MEDIA_GUARD], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        cwd=Path(__file__).parents[1], env=env,
    )
    request_id = 0

    def request(method: str, params: dict) -> dict:
        nonlocal request_id
        request_id += 1
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(json.dumps({"protocolVersion": 1, "requestId": request_id,
                                        "method": method, "params": params}) + "\n")
        process.stdin.flush()
        line = process.stdout.readline()
        if not line:
            if process.poll() is None:
                process.terminate()
            _, diagnostic = process.communicate(timeout=5)
            raise AssertionError(f"Service produced no response during {method}; exit={process.returncode}; stderr={diagnostic}")
        response = json.loads(line)
        assert response["requestId"] == request_id
        assert response["ok"], response.get("error")
        return response["result"]

    def wait_for_job(job_id: str, project_id: str) -> dict:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            jobs = request("job_list", {"projectId": project_id, "limit": 50})["jobs"]
            job = next(item for item in jobs if item["id"] == job_id)
            if job["state"] in {"completed", "failed", "cancelled"}:
                assert job["state"] == "completed", job
                return job
            time.sleep(0.1)
        raise AssertionError(f"Job {job_id} did not finish before the acceptance timeout.")

    try:
        project = request("project_create", {"path": str(project_path), "name": "Offline lesson", "settings": {}})
        imported = request("media_import", {"path": str(project_path), "inputs": [str(source)], "storageMode": "linked"})
        assert not imported["errors"] and len(imported["imported"]) == 1
        asset = imported["imported"][0]
        asset_id = asset["assetId"]
        proxy_job = request("proxy_generate", {"path": str(project_path), "assetId": asset_id})
        wait_for_job(proxy_job["id"], project["projectId"])

        transcript_job = request("transcription_generate", {"path": str(project_path), "assetId": asset_id, "model": "tiny"})
        wait_for_job(transcript_job["id"], project["projectId"])
        transcript = request("transcript_list", {"path": str(project_path), "assetId": asset_id})["transcripts"][0]
        assert transcript["words"] and transcript["words"][0]["sourceInTicks"] >= 0

        brief = request("brief_save", {"path": str(project_path), "briefText": "Goal: Explain an offline editing workflow\nTone: Clear and concise"})["brief"]
        proposal_job = request("proposal_generate", {"path": str(project_path), "assetId": asset_id,
                                                       "briefId": brief["id"], "transcriptId": transcript["id"]})
        wait_for_job(proposal_job["id"], project["projectId"])
        plans = request("proposal_list", {"path": str(project_path), "briefId": brief["id"]})["plans"]
        assert plans and plans[0]["plan"]["operations"]

        # Exercise explicit manual edit/save/export through the service protocol.
        state = request("project_open_last", {})["state"]
        duration = min(int(asset["metadata"]["durationTicks"]), 2_000_000)
        state["timelineItems"] = [{
            "id": str(uuid.uuid4()), "assetId": asset_id, "itemType": "clip", "trackId": "video",
            "timelineStartTicks": 0, "timelineDurationTicks": duration,
            "sourceInTicks": 0, "sourceOutTicks": duration,
            "properties": {"linkedAudio": True, "audioGainDb": 0},
        }]
        request("project_save", {"path": str(project_path), "state": state})
        export_job = request("export_generate", {"path": str(project_path), "burnCaptions": False})
        wait_for_job(export_job["id"], project["projectId"])
        exports = request("export_list", {"path": str(project_path)})["exports"]
        assert any(item["status"] == "completed" for item in exports)
        assert source.read_bytes() == original_bytes
    finally:
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
