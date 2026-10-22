"""Word-derived, source-bounded editable captions with explicit readability warnings."""
from copy import deepcopy
import math

from .persistence import _connect
from . import timeline

VERSION = "1"
DEFAULTS = {"charactersPerSecond": 20, "lineLength": 42, "maxLines": 2, "safeAreaPercent": 8, "style": "plain"}


def caption_settings(settings=None):
    if settings is None:
        return dict(DEFAULTS)
    if not isinstance(settings, dict) or set(settings) - set(DEFAULTS):
        raise ValueError("Unsupported caption settings.")
    value = {**DEFAULTS, **settings}
    for name, low, high in (("charactersPerSecond", 5, 40), ("lineLength", 10, 80), ("maxLines", 1, 3), ("safeAreaPercent", 5, 20)):
        number = value[name]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not low <= number <= high:
            raise ValueError(f"{name} must be between {low} and {high}.")
        if name in ("lineLength", "maxLines") and type(number) is not int:
            raise ValueError(f"{name} must be an integer.")
    if value["style"] not in ("plain", "boxed", "large"):
        raise ValueError("Choose plain, boxed, or large caption style.")
    return value


def segment_words(words, source_in, source_out, settings=None):
    """Never stretch a caption beyond its words; flag impossible reading speed."""
    config = caption_settings(settings)
    result, group = [], []

    def layout(items):
        lines = [""]
        for word in items:
            text = word["text"]
            if len(text) > config["lineLength"]:
                raise ValueError("A transcript word exceeds the caption line limit. Edit the word or increase line length.")
            candidate = (lines[-1] + " " + text).strip()
            if len(candidate) > config["lineLength"]:
                lines.append(text)
            else:
                lines[-1] = candidate
        return lines

    def emit():
        if not group:
            return
        text = "\n".join(layout(group))
        start, end = group[0]["sourceInTicks"], max(w["sourceOutTicks"] for w in group)
        cps = len(text.replace("\n", " ")) / ((end - start) / 1_000_000)
        result.append({"sourceInTicks": start, "sourceOutTicks": end, "text": text,
                       "charactersPerSecond": cps, "readingSpeedExceeded": cps > config["charactersPerSecond"]})

    for word in sorted(words, key=lambda w: (w["sourceInTicks"], w["sourceOutTicks"])):
        start, end = word["sourceInTicks"], word["sourceOutTicks"]
        if type(start) is not int or type(end) is not int or start < 0 or end <= start:
            raise ValueError("Invalid transcript word timestamp.")
        # Partial words cannot be timed accurately after trimming; omit them.
        if start < source_in or end > source_out:
            continue
        text = " ".join(str(word["text"]).split())
        if not text:
            continue
        clean = {**word, "text": text}
        proposed = group + [clean]
        if group and (start - group[-1]["sourceOutTicks"] > 700_000 or len(layout(proposed)) > config["maxLines"]):
            emit(); group = []
        group.append(clean)
        layout(group)
        if text.endswith((".", "!", "?")):
            emit(); group = []
    emit()
    return result


def generate_captions(store, expected_revision, settings=None):
    """Append captions for video clips as one reversible edit. Existing captions remain."""
    config = caption_settings(settings)
    warnings = []
    count = 0

    def mutate(doc, state):
        nonlocal count
        profile = state['settings'].get('outputProfile')
        if profile:
            from .export_profiles import validate_profile
            profile = validate_profile(profile)
            safe = profile['safeArea']
            config['lineLength'] = min(config['lineLength'], max(10, int(profile['width']*(1-safe['left']-safe['right']) / (profile['height']*.052))))
        before = timeline._snapshot(doc)
        clips = [c for c in doc["clips"] if c["itemType"] == "clip"]
        with _connect(store.database_path) as db:
            for clip in clips:
                if any(c["itemType"] == "caption" and c["properties"].get("linkedClipId") == clip["id"] for c in doc["clips"]):
                    continue
                transcript = db.execute("SELECT t.* FROM transcripts t JOIN assets a ON a.id=t.asset_id WHERE t.asset_id=? AND t.status='ready' AND t.source_fingerprint=a.sha256 ORDER BY t.created_at DESC,t.id DESC LIMIT 1", (clip["assetId"],)).fetchone()
                if transcript is None:
                    warnings.append({"clipId": clip["id"], "message": "No current word transcript; transcribe this source first."})
                    continue
                words = [{"sourceInTicks": r["source_in_ticks"], "sourceOutTicks": r["source_out_ticks"], "text": r["edited_text"] if r["edited_text"] is not None else r["text"]} for r in db.execute("SELECT * FROM transcript_words WHERE transcript_id=? ORDER BY ordinal", (transcript["id"],))]
                for segment in segment_words(words, clip["sourceInTicks"], clip["sourceOutTicks"], config):
                    timeline._apply(doc, state, {"type": "caption", "clipId": clip["id"], "text": segment["text"], "sourceInTicks": segment["sourceInTicks"], "sourceOutTicks": segment["sourceOutTicks"]})
                    record = doc["captions"][-1]
                    record["language"] = transcript["language"]
                    record["style"] = deepcopy(config)
                    item = doc["clips"][-1]
                    item["properties"].update(generatedBy="localcut-word-captions", generatorVersion=VERSION, transcriptId=transcript["id"])
                    count += 1
                    if segment["readingSpeedExceeded"]:
                        warnings.append({"captionId": record["id"], "message": "Reading speed exceeds the limit. Edit the caption or source cut.", "charactersPerSecond": segment["charactersPerSecond"]})
        if count:
            doc["undo"] = (doc["undo"] + [{"command": {"type": "generate_captions", "settings": config}, "before": before, "after": timeline._snapshot(doc)}])[-timeline.LIMIT:]
            doc["redo"] = []

    document = timeline._transaction(store, expected_revision, mutate)
    return {"timeline": document, "captionCount": count, "warnings": warnings}
