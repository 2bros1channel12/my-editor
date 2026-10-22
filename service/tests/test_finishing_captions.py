import pytest
from localcut_service.finishing_captions import caption_settings, segment_words


def word(text, start, end):
    return {"text": text, "sourceInTicks": start, "sourceOutTicks": end}


def test_word_bounds_and_line_limits():
    rows = segment_words([word("trimmed", 0, 200_000), word("hello", 500_000, 900_000), word("world", 1_000_000, 1_500_000), word("outside", 1_600_000, 2_100_000)], 400_000, 2_000_000, {"lineLength": 10, "maxLines": 1})
    assert [r["text"] for r in rows] == ["hello", "world"]
    assert all(400_000 <= r["sourceInTicks"] < r["sourceOutTicks"] <= 2_000_000 for r in rows)


def test_fast_speech_is_flagged_without_inventing_time():
    rows = segment_words([word("quickword", 1_000_000, 1_100_000)], 0, 2_000_000)
    assert rows[0]["readingSpeedExceeded"]
    assert rows[0]["sourceOutTicks"] == 1_100_000


def test_gap_and_sentence_split():
    rows = segment_words([word("First.", 0, 600_000), word("Second", 700_000, 1_200_000), word("Third", 2_000_000, 2_500_000)], 0, 3_000_000)
    assert [r["text"] for r in rows] == ["First.", "Second", "Third"]


@pytest.mark.parametrize("settings", [{"lineLength": 10.5}, {"style": "{\\evil}"}, {"safeAreaPercent": 0}, {"charactersPerSecond": float("nan")}, {"maxLines": True}, {"shell": "bad"}])
def test_invalid_settings(settings):
    with pytest.raises(ValueError):
        caption_settings(settings)


def test_long_token_fails_actionably():
    with pytest.raises(ValueError, match="word exceeds"):
        segment_words([word("a" * 50, 0, 1_000_000)], 0, 1_000_000)


def test_invalid_timestamps():
    with pytest.raises(ValueError, match="timestamp"):
        segment_words([word("bad", 10, 1)], 0, 1_000_000)


def test_generation_persists_provenance_and_is_one_undoable_edit(tmp_path):
    import sqlite3
    from localcut_service.persistence import ProjectStore
    from localcut_service.timeline import timeline_state, timeline_command, timeline_undo, timeline_redo
    from localcut_service.finishing_captions import generate_captions
    source = tmp_path / "source.mp4"
    source.write_bytes(b"unchanged media")
    store = ProjectStore.create(tmp_path / "project", "Captions")
    asset = store.add_asset(source)
    with sqlite3.connect(store.database_path) as db:
        db.execute("UPDATE assets SET duration_ticks=3000000 WHERE id=?", (asset,))
        digest = db.execute("SELECT sha256 FROM assets WHERE id=?", (asset,)).fetchone()[0]
    transcript_id = store.add_transcript(asset_id=asset, language="en", words=[word("Hello.", 500_000, 1_200_000), word("Again!", 1_500_000, 2_500_000)], generator_id="fixture", generator_version="1", source_fingerprint=digest)
    doc = timeline_command(store, {"type": "insert", "assetId": asset, "trackId": "video", "sourceInTicks": 0, "sourceOutTicks": 3_000_000}, timeline_state(store)["revision"])
    result = generate_captions(store, doc["revision"], {"style": "boxed"})
    assert result["captionCount"] == 2
    doc = result["timeline"]
    assert all(c["style"]["style"] == "boxed" for c in doc["captions"])
    assert all(c["properties"]["transcriptId"] == transcript_id for c in doc["clips"] if c["itemType"] == "caption")
    reopened = ProjectStore.open(store.root)
    undone = timeline_undo(reopened, doc["revision"])
    assert undone["captions"] == []
    redone = timeline_redo(reopened, undone["revision"])
    assert len(redone["captions"]) == 2
    assert generate_captions(reopened, redone["revision"])["captionCount"] == 0
    assert source.read_bytes() == b"unchanged media"
