import json

import pytest
from localcut_service.briefs import DeterministicBriefParser

from localcut_service.config import AppConfig
from localcut_service.jobs import JobQueue
from localcut_service.persistence import ProjectCatalog, ProjectStore, _sha256_file
from localcut_service.server import dispatch
from localcut_service.suggestions import SuggestionHandler, proposal_job


@pytest.fixture
def project(tmp_path):
    config = AppConfig(data_dir=tmp_path / "app")
    store = ProjectStore.create(tmp_path / "project", "Guided brief")
    return store, config


def save(store, config, **params):
    return dispatch("brief_save", {"path": str(store.root), "briefText": "Purpose: Explain a lesson", **params}, config)["brief"]


def guided():
    return {
        "objective": "Teach camera setup", "audience": "Beginners", "platform": "youtube",
        "targetDurationMinutes": 3.5, "mustKeep": ["white balance"], "mustRemove": ["setup chatter"],
        "desiredOpening": "Show the finished result", "desiredEnding": "Recap the settings",
        "tone": "Clear and friendly", "recipeId": "fast-educational", "recipeVersion": "1",
        "styleTraits": {"pacing": "moderate", "punchInFrequencyPerMinute": 0},
    }


def test_labeled_guided_fields_and_neutral_traits_parse_without_identity_inference():
    value = DeterministicBriefParser().parse("Purpose: Teach testing\nPlatform: YouTube Shorts\nOpening: Show result\nEnding: Recap\nPacing: fast\nCaption density: 8\nPunch-in frequency: 0\nMusic energy: low")
    assert value["platform"] == "youtube-shorts"
    assert value["objective"] == "Teach testing"
    assert value["desiredOpening"] == "Show result"
    assert value["desiredEnding"] == "Recap"
    assert value["styleTraits"]["pacing"] == "fast"
    assert value["styleTraits"]["captionDensityPerMinute"] == 8
    assert value["styleTraits"]["punchInFrequencyPerMinute"] == 0
    assert value["styleTraits"]["musicEnergy"] == "low"


def test_guided_constraints_edited_recipe_and_provenance_roundtrip(project):
    store, config = project
    constraints = guided()
    brief = save(store, config, constraints=constraints, styleReviewed=True)
    reopened = ProjectStore.open(store.root).list_briefs()[0]
    assert reopened == brief
    assert reopened["constraints"]["schemaVersion"] == 2
    assert reopened["constraints"]["styleReviewed"] is True
    for key, value in constraints.items():
        if key != "styleTraits":
            assert reopened["constraints"][key] == value
    assert reopened["constraints"]["styleTraits"]["pacing"] == "moderate"
    assert reopened["constraints"]["styleTraits"]["punchInFrequencyPerMinute"] == 0
    assert reopened["traits"]["pacing"] == {
        "value": "moderate", "origin": "user", "generatorId": "localcut-neutral-style-recipe", "generatorVersion": "1",
    }


@pytest.mark.parametrize("origin", ["user", "reference-derived"])
@pytest.mark.parametrize("location", ["traits", "styleTraits"])
@pytest.mark.parametrize("reviewed", [None, False, "true", 1])
def test_style_requires_explicit_boolean_review(project, origin, location, reviewed):
    store, config = project
    params = {"traitOrigin": origin, "styleReviewed": reviewed}
    params.update({"traits": {"pacing": "fast"}} if location == "traits" else {"constraints": {"styleTraits": {"pacing": "fast"}, "styleReviewed": True}})
    with pytest.raises(ValueError, match="Review and edit"):
        save(store, config, **params)
    assert store.list_briefs() == []


@pytest.mark.parametrize("origin", ["user", "reference-derived"])
@pytest.mark.parametrize("key", ["voice", "likeness", "branding", "signaturePhrase", "musicUrl"])
def test_unknown_cloning_trait_keys_blocked_for_both_origins(project, origin, key):
    store, config = project
    with pytest.raises(ValueError, match="unsupported trait"):
        save(store, config, traits={key: "copy"}, traitOrigin=origin, styleReviewed=True)
    with pytest.raises(ValueError, match="unsupported trait"):
        save(store, config, constraints={"styleTraits": {key: "copy"}}, traitOrigin=origin, styleReviewed=True)


@pytest.mark.parametrize("traits", [
    {"pacing": "exact imitation"}, {"musicEnergy": "copyrighted song"}, {"narrativeStructure": "signature structure"},
    {"punchInFrequencyPerMinute": True}, {"jumpCutFrequencyPerMinute": -1},
    {"captionDensityPerMinute": float("nan")}, {"averageShotLengthSeconds": float("inf")},
])
def test_invalid_style_values_rejected(project, traits):
    store, config = project
    with pytest.raises(ValueError):
        save(store, config, constraints={"styleTraits": traits}, styleReviewed=True)


def test_inspiration_never_survives_proposal_or_saved_profile(project):
    store, config = project
    inspiration = "Private inspiration name"
    proposed = dispatch("style_recipe_propose", {"recipeId": "calm-documentary", "inspiration": inspiration}, config)
    assert inspiration not in json.dumps(proposed)
    brief = save(store, config, constraints={"recipeId": proposed["recipeId"], "recipeVersion": proposed["recipeVersion"], "styleTraits": proposed["traits"]}, styleReviewed=True)
    assert inspiration not in json.dumps(brief)
    assert brief["constraints"]["styleTraits"]["punchInFrequencyPerMinute"] == 0


def test_legacy_brief_without_style_saves_and_creator_content_notes_stay_intact(project):
    store, config = project
    text = "Discuss a creator as a subject of this lesson"
    brief = save(store, config, briefText=text)
    reopened = ProjectStore.open(store.root).list_briefs()[0]
    assert reopened == brief
    assert brief["briefText"] == text
    assert brief["constraints"]["objective"] == text
    assert brief["constraints"]["schemaVersion"] == 2
    assert not brief["traits"]


def test_opening_selected_project_refreshes_reopen_last(project, tmp_path):
    store, config = project
    other = ProjectStore.create(tmp_path / "other", "Other")
    catalog = ProjectCatalog(config.data_dir)
    catalog.remember(store.root)
    catalog.remember(other.root)
    assert catalog.reopen_last().root == other.root
    dispatch("project_open", {"path": str(store.root)}, config)
    assert ProjectCatalog(config.data_dir).reopen_last().root == store.root


def test_planner_receives_selected_traits_and_full_guided_constraints(project, tmp_path):
    store, config = project
    source = tmp_path / "lesson.mov"
    source.write_bytes(b"untouched lesson fixture")
    fingerprint = _sha256_file(source)
    asset = store.import_asset_reference(source, {"durationSeconds": 12}, probe_tool="fixture", probe_version="1", duration_ticks=12_000_000)["assetId"]
    artifact_path = store.root / "cache" / "transcript.json"
    artifact_path.parent.mkdir(exist_ok=True)
    artifact_path.write_text('{"fixture":true}')
    artifact = store.register_artifact(asset_id=asset, kind="transcript", relative_path="cache/transcript.json", source_fingerprint=fingerprint, generator_id="fixture-asr", generator_version="1", checksum_sha256=_sha256_file(artifact_path))
    words = [{"text": text, "originalText": text, "sourceInTicks": start, "sourceOutTicks": start + 200_000, "confidence": .9} for text, start in [("white", 1_000_000), ("balance", 1_300_000)]]
    transcript = store.add_transcript(asset_id=asset, language="en", words=words, segments=[{"sourceInTicks": 1_000_000, "sourceOutTicks": 1_500_000, "text": "white balance", "confidence": .9}], generator_id="fixture-asr", generator_version="1", source_fingerprint=fingerprint, artifact_id=artifact)
    brief = save(store, config, constraints=guided(), styleReviewed=True)
    job = proposal_job(store, asset, brief["id"], transcript)
    assert job.config["pacingProfile"]["value"] == "moderate"
    assert job.config["punchInProfile"]["value"] == 0
    queue = JobQueue(tmp_path / "jobs", handlers={"suggestion": SuggestionHandler()}, concurrency=1)
    try:
        submitted = queue.submit(job)
        completed = queue.wait(submitted["id"], 5)
        assert completed["state"] == "completed", completed.get("error")
        plan = store.list_edit_plans(brief["id"])[0]["plan"]
        assert plan["constraints"] == brief["constraints"]
        assert plan["constraints"]["desiredOpening"] == "Show the finished result"
        assert plan["constraints"]["platform"] == "youtube"
        assert store.load_state()["timelineItems"] == []
        assert _sha256_file(source) == fingerprint
    finally:
        queue.close()
