# Local privacy validation

Implemented privacy/storage controls and remaining release gates are documented in [storage controls](RELEASE_STORAGE.md) and [release readiness](RELEASE_READINESS.md). The current service-level QA evidence, including the 13-stage workflow under denied Python DNS/socket access, is in [release QA](RELEASE_QA.md). The packaged GUI still needs OS-level network-deny validation.

The Python service has three subprocess tests with an audit hook that rejects DNS lookups and outbound socket connections from the service process. Loopback connections remain permitted for local IPC.

- `test_core_local_workflow_completes_with_external_network_denied` starts the real stdio service and checks health, capabilities, project creation, brief parsing, detector catalog loading, and reopening the last project.
- `test_real_whisper_transcription_job_completes_with_external_network_denied` runs the actual local Whisper CPU backend through a persistent job and verifies the saved transcript while external networking is denied to the service process.
- `test_local_media_transcript_proposal_edit_and_export_with_external_network_denied` uses locally installed FFmpeg/FFprobe and generated synthetic footage to exercise linked media import, proxy generation, the transcription job interface, brief/proposal generation, project/timeline persistence, and H.264/AAC export. It verifies the original media bytes remain unchanged. A deterministic test-only speech backend and test-only model readiness override are injected into the service subprocess so this acceptance test never acquires model weights.

Run it on Windows with:

```powershell
cd service
.\.venv\Scripts\python.exe -m pytest tests/test_privacy_offline.py -q
```

This is a service-level regression check for accidental networking across local workflows. The test-only speech backend proves the transcription job and artifact plumbing; the separate real-Whisper test exercises the native inference executable with an already acquired, integrity-verified model. Neither test proves the full release privacy gate: exercise the entire workflow with representative footage and the packaged desktop application while capturing outbound traffic on a network-deny host. Model acquisition is intentionally excluded because it requires network access before offline use. The broader release gate remains open.

The media integration test requires FFmpeg and FFprobe. Set `LOCAL_CUT_FFMPEG` and `LOCAL_CUT_FFPROBE` to their local executable paths if they are not on `PATH`. The core service-only test does not require either tool.

`test_real_whisper_transcription_job_completes_with_external_network_denied` adds a no-FFmpeg real-inference check. It builds a prepared local analysis-audio artifact, verifies the pinned tiny-model bytes, then launches the actual service with an audit hook denying external DNS and sockets. The service queues the real whisper.cpp CPU backend, persists transcript words through the normal job/artifact path, and the test checks the expected JFK sample text, bounded source timestamps, model/backend provenance, and unchanged source WAV. It does not exercise media import/proxy/export or a packaged Tauri app. Run with local fixtures:

```powershell
$env:LOCAL_CUT_TEST_WHISPER = 'C:/tools/whisper/whisper-cli.exe'
$env:LOCAL_CUT_TEST_WHISPER_MODEL = 'C:/fixtures/ggml-tiny.bin'
$env:LOCAL_CUT_TEST_SPEECH = 'C:/fixtures/jfk.wav'
service/.venv/Scripts/python.exe -m pytest service/tests/test_privacy_offline.py::test_real_whisper_transcription_job_completes_with_external_network_denied -q
```
