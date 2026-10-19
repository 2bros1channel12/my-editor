"""Optional end-to-end H.264 export check using locally installed FFmpeg tools."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from localcut_service.jobs import JobQueue
from localcut_service.media import FFprobe, import_media
from localcut_service.persistence import ProjectStore
from localcut_service.render import RenderHandler, export_srt, list_exports, render_job


def _caption_band_frame(ffmpeg: str, media: Path) -> bytes:
    region = "crop=1720:180:100:900,format=rgb24"
    return subprocess.run(
        [ffmpeg, "-hide_banner", "-v", "error", "-nostdin", "-ss", "1.5", "-protocol_whitelist", "file,pipe", "-i", str(media), "-map", "0:v:0", "-frames:v", "1", "-vf", region, "-f", "rawvideo", "pipe:1"],
        check=True, capture_output=True, timeout=30,
    ).stdout


@pytest.mark.parametrize(
    ("duration_seconds", "size"),
    [
        (4, "320x180"),
        pytest.param(
            600, "1920x1080",
            marks=pytest.mark.skipif(os.environ.get("LOCAL_CUT_RUN_LONG_RENDER") != "1", reason="Set LOCAL_CUT_RUN_LONG_RENDER=1 to run the 10-minute 1080p release check"),
        ),
    ],
)
def test_real_h264_export_is_playable_synced_captioned_and_nondestructive(tmp_path: Path, duration_seconds: int, size: str) -> None:
    ffmpeg = os.environ.get("LOCAL_CUT_TEST_FFMPEG")
    ffprobe = os.environ.get("LOCAL_CUT_TEST_FFPROBE")
    if not ffmpeg or not ffprobe:
        pytest.skip("Set LOCAL_CUT_TEST_FFMPEG and LOCAL_CUT_TEST_FFPROBE for the real export test")

    source = tmp_path / "talking head & tutorial [雪].mp4"
    subprocess.run(
        [
            ffmpeg, "-hide_banner", "-v", "error", "-nostdin",
            "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:duration={duration_seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={duration_seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(source),
        ], check=True, capture_output=True, timeout=600,
    )
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()

    store = ProjectStore.create(tmp_path / "local project", "Render integration")
    imported = import_media(store, [source], probe=FFprobe(ffprobe))
    assert imported["errors"] == []
    asset_id = imported["imported"][0]["assetId"]
    transcript_words = [
        {"text": "A", "sourceInTicks": 1_000_000, "sourceOutTicks": 1_200_000},
        {"text": "verified", "sourceInTicks": 1_250_000, "sourceOutTicks": 1_650_000},
        {"text": "local", "sourceInTicks": 1_700_000, "sourceOutTicks": 1_950_000},
        {"text": "caption", "sourceInTicks": 2_000_000, "sourceOutTicks": 2_500_000},
    ]
    transcript_text = " ".join(word["text"] for word in transcript_words)
    transcript_path = store.root / "cache" / "fixture-transcript.json"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(json.dumps({"fixture": "approved transcript text"}), encoding="utf-8")
    transcript_checksum = hashlib.sha256(transcript_path.read_bytes()).hexdigest()
    transcript_artifact_id = store.register_artifact(
        asset_id=asset_id, kind="transcript", relative_path=transcript_path.relative_to(store.root).as_posix(),
        source_fingerprint=source_digest, generator_id="fixture-asr", generator_version="integration-test",
        checksum_sha256=transcript_checksum,
    )
    store.add_transcript(
        asset_id=asset_id, language="en", words=transcript_words,
        segments=[{"text": transcript_text, "sourceInTicks": 1_000_000, "sourceOutTicks": 2_500_000}],
        generator_id="fixture-asr", generator_version="integration-test", source_fingerprint=source_digest,
        artifact_id=transcript_artifact_id,
    )
    duration_ticks = duration_seconds * 1_000_000
    clip = {
        "id": "clip-1", "assetId": asset_id, "itemType": "clip", "trackId": "video",
        "timelineStartTicks": 0, "timelineDurationTicks": duration_ticks,
        "sourceInTicks": 0, "sourceOutTicks": duration_ticks,
        "properties": {"linkedAudio": True, "audioGainDb": 0, "fadeInTicks": 0, "fadeOutTicks": 0},
    }
    caption = {
        "id": "caption-1", "assetId": asset_id, "itemType": "caption", "trackId": "captions",
        "timelineStartTicks": 1_000_000, "timelineDurationTicks": 2_000_000,
        "sourceInTicks": 1_000_000, "sourceOutTicks": 3_000_000,
        "properties": {},
    }
    caption_text = transcript_text
    store.autosave(store.project_id, {
        "timelineItems": [clip, caption],
        "captions": [{"id": "caption-text-1", "timelineItemId": "caption-1", "text": caption_text, "language": "en", "style": {}}],
    })

    handler = RenderHandler(ffmpeg, ffprobe)
    queue = JobQueue(tmp_path / "jobs", handlers={"rendering": handler}, concurrency=1)
    try:
        clean_request = render_job(store, ffmpeg, ffprobe, burn_captions=False)
        clean_submitted = queue.submit(clean_request)
        clean_completed = queue.wait(clean_submitted["id"], timeout=3600)
        assert clean_completed["state"] == "completed", clean_completed.get("error")
        request = render_job(store, ffmpeg, ffprobe, burn_captions=True)
        submitted = queue.submit(request)
        completed = queue.wait(submitted["id"], timeout=3600)
        assert completed["state"] == "completed", completed.get("error")

        output = store.root / "exports" / f"{request.config['exportId']}.mp4"
        clean_output = store.root / "exports" / f"{clean_request.config['exportId']}.mp4"
        assert output.is_file() and output.stat().st_size > 1024
        assert clean_output.is_file() and clean_output.stat().st_size > 1024
        probe = json.loads(subprocess.run(
            [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)],
            check=True, capture_output=True, text=True, timeout=30,
        ).stdout)
        streams = probe["streams"]
        assert any(item.get("codec_type") == "video" and item.get("codec_name") == "h264" for item in streams)
        assert any(item.get("codec_type") == "audio" and item.get("codec_name") == "aac" for item in streams)
        assert float(probe["format"]["duration"]) == pytest.approx(duration_seconds, abs=1 / 30)
        clean_band = _caption_band_frame(ffmpeg, clean_output)
        rendered_band = _caption_band_frame(ffmpeg, output)
        assert len(clean_band) == len(rendered_band)
        changed_caption_band_pixels = sum(
            max(abs(clean_band[index + channel] - rendered_band[index + channel]) for channel in range(3)) > 40
            for index in range(0, len(clean_band), 3)
        )
        assert changed_caption_band_pixels > 1000, "Burned-in caption did not create the expected visible change in the caption band."
        srt = export_srt(store)
        assert caption_text in (store.root / srt["relativePath"]).read_text(encoding="utf-8")
        assert hashlib.sha256(source.read_bytes()).hexdigest() == source_digest
        mp4_export = next(item for item in list_exports(store) if item["format"] == "mp4-h264-aac")
        assert mp4_export["settings"]["audioVideoStartAlignmentValidated"] is True
        assert mp4_export["settings"]["burnCaptions"] is True
        assert abs(mp4_export["settings"]["audioVideoStartOffsetSeconds"]) <= 1 / 30
        assert mp4_export["settings"]["audioVideoEndAlignmentValidated"] is True
        assert abs(mp4_export["settings"]["audioVideoEndOffsetSeconds"]) <= 1 / 30
    finally:
        queue.close()
