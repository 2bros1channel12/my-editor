"""Editable OpenTimelineIO JSON export; application effects remain explicit metadata."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import uuid

from .persistence import _connect, _json, _now, atomic_write
from .proxies import source_path
from .timeline import _base, _validate

RATE = 1_000_000


def _time(value):
    return {"OTIO_SCHEMA": "RationalTime.1", "value": value, "rate": RATE}


def _range(start, duration):
    return {"OTIO_SCHEMA": "TimeRange.1", "start_time": _time(start), "duration": _time(duration)}


def build_otio(store, state):
    doc = _base(state)
    _validate(doc, state)
    if any(c["itemType"] == "overlay" for c in doc["clips"]):
        raise ValueError("OTIO export does not yet support overlay items; remove the overlay before exporting.")
    assets = {asset["id"]: asset for asset in state["assets"]}
    tracks = []
    duration = max((c["timelineStartTicks"] + c["timelineDurationTicks"] for c in doc["clips"]), default=0)
    warnings = ["Caption styling, gain, fades, crop and music rights are preserved as Local Cut metadata; other editors may not apply them."]
    for track in doc["tracks"]:
        if track["kind"] == "caption":
            continue
        children = []
        cursor = 0
        for clip in sorted((c for c in doc["clips"] if c["trackId"] == track["id"] and c["itemType"] in ("clip", "audio")), key=lambda c: c["timelineStartTicks"]):
            gap = clip["timelineStartTicks"] - cursor
            if gap:
                children.append({"OTIO_SCHEMA": "Gap.1", "name": "Gap", "source_range": _range(0, gap), "effects": [], "markers": [], "metadata": {}})
            asset = assets[clip["assetId"]]
            ref = {"OTIO_SCHEMA": "ExternalReference.1", "name": clip["assetId"], "target_url": source_path(store, asset).as_uri(), "available_range": _range(0, asset["duration_ticks"]), "metadata": {"localcut": {"assetId": clip["assetId"], "sha256": asset.get("sha256")}}}
            children.append({"OTIO_SCHEMA": "Clip.2", "name": clip["id"], "source_range": _range(clip["sourceInTicks"], clip["sourceOutTicks"] - clip["sourceInTicks"]), "media_references": {"DEFAULT_MEDIA": ref}, "active_media_reference_key": "DEFAULT_MEDIA", "effects": [], "markers": [], "metadata": {"localcut": deepcopy(clip)}})
            cursor = clip["timelineStartTicks"] + clip["timelineDurationTicks"]
        if cursor < duration:
            children.append({"OTIO_SCHEMA": "Gap.1", "name": "Gap", "source_range": _range(0, duration-cursor), "effects": [], "markers": [], "metadata": {}})
        tracks.append({"OTIO_SCHEMA": "Track.1", "name": track["name"], "kind": "Audio" if track["kind"] == "audio" else "Video", "children": children, "source_range": None, "effects": [], "markers": [], "metadata": {"localcut": {"trackId": track["id"]}}})
        if track["kind"] == "video" and any(c.get("metadata", {}).get("localcut", {}).get("properties", {}).get("linkedAudio", True) for c in children if c["OTIO_SCHEMA"] == "Clip.2"):
            linked = deepcopy(tracks[-1])
            linked["kind"] = "Audio"
            linked["name"] += " (linked audio)"
            linked["metadata"]["localcut"]["linkedVideoTrackId"] = track["id"]
            for i, child in enumerate(linked["children"]):
                if child["OTIO_SCHEMA"] == "Clip.2" and not child["metadata"]["localcut"]["properties"].get("linkedAudio", True):
                    linked["children"][i] = {"OTIO_SCHEMA": "Gap.1", "name": "Muted linked audio", "source_range": _range(0, child["source_range"]["duration"]["value"]), "effects": [], "markers": [], "metadata": {}}
            tracks.append(linked)
    markers = [{"OTIO_SCHEMA": "Marker.2", "name": m["label"], "marked_range": _range(m["atTicks"], 0), "color": "GREEN", "metadata": {"localcut": {"id": m["id"]}}} for m in doc["markers"]]
    return {"OTIO_SCHEMA": "Timeline.1", "name": "Local Cut", "global_start_time": _time(0), "tracks": {"OTIO_SCHEMA": "Stack.1", "name": "Tracks", "children": tracks, "source_range": None, "effects": [], "markers": markers, "metadata": {}}, "metadata": {"localcut": {"schemaVersion": 1, "timeline": {k: deepcopy(doc[k]) for k in ("tracks", "clips", "captions", "markers", "policies")}, "warnings": warnings}}}


def export_otio(store):
    with _connect(store.database_path) as db:
        db.execute("BEGIN")
        state = store.load_state(db)
        timeline = db.execute("SELECT * FROM timelines WHERE project_id=? AND is_current=1", (store.project_id,)).fetchone()
        if timeline is None:
            raise ValueError("The current project has no timeline.")
        document = build_otio(store, state)
        db.rollback()
    data = (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False)+"\n").encode("utf-8")
    export_id = str(uuid.uuid4())
    relative = f"exports/timeline-{export_id}.otio"
    target = (store.root / relative).resolve()
    target.relative_to(store.root.resolve())
    atomic_write(target, data)
    try:
        with _connect(store.database_path) as db:
            db.execute("INSERT INTO exports(id,project_id,timeline_id,status,relative_path,format,settings_json,started_at,completed_at,checksum_sha256,error_code) VALUES (?,?,?,'completed',?,?,?,?,?,?,NULL)", (export_id, store.project_id, timeline["id"], relative, "otio", _json({"timelineRevision": timeline["revision"], "warnings": document["metadata"]["localcut"]["warnings"]}), _now(), _now(), hashlib.sha256(data).hexdigest()))
            db.commit()
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return {"relativePath": relative, "timelineRevision": int(timeline["revision"]), "warnings": document["metadata"]["localcut"]["warnings"]}
