import json

from localcut_service.briefs import DeterministicBriefParser
from localcut_service.jobs import JobQueue
from localcut_service.persistence import ProjectStore, _connect, _sha256_file
from localcut_service.suggestions import SuggestionHandler, proposal_job


def prepared(tmp_path):
    store=ProjectStore.create(tmp_path/"project","Proposal test")
    source=tmp_path/"lesson source.mov"; source.write_bytes(b"untouched lesson")
    asset=store.import_asset_reference(source,{"durationSeconds":12},probe_tool="test",probe_version="1",duration_ticks=12_000_000)["assetId"]
    transcript_path=store.root/"cache"/"transcript.json"; transcript_path.parent.mkdir(exist_ok=True); transcript_path.write_text('{"fixture":true}')
    artifact=store.register_artifact(asset_id=asset,kind="transcript",relative_path=transcript_path.relative_to(store.root).as_posix(),source_fingerprint=_sha256_file(source),generator_id="fixture-asr",generator_version="1",checksum_sha256=_sha256_file(transcript_path))
    tokens=[("we",1_000_000),("need",1_300_000),("keep",1_600_000),("this",1_900_000),("lesson",2_200_000)]
    words=[{"text":word,"originalText":word,"sourceInTicks":start,"sourceOutTicks":start+200_000,"confidence":.9} for word,start in tokens]
    transcript=store.add_transcript(asset_id=asset,language="en",words=words,segments=[{"sourceInTicks":1_000_000,"sourceOutTicks":2_400_000,"text":"We need keep this lesson","confidence":.9}],generator_id="fixture-asr",generator_version="1",source_fingerprint=_sha256_file(source),artifact_id=artifact)
    brief=store.add_brief("Audience: New learners\nObjective: Teach the lesson\nMust keep: keep this",constraints={"audience":"New learners","objective":"Teach the lesson","tone":"clear","aspectRatio":"source","targetDurationMinutes":None,"mustKeep":["keep this"],"mustRemove":[]})
    return store,source,asset,transcript,brief


def test_brief_parser_is_deterministic_and_overrides_visible_fields():
    parser=DeterministicBriefParser()
    parsed=parser.parse("Audience: new developers\nGoal: Explain debugging\nMust keep: profiler, state updates\nTarget duration: 8")
    assert parsed=={"audience":"new developers","objective":"Explain debugging","tone":"","targetDurationMinutes":8.0,"aspectRatio":"source","mustKeep":["profiler","state updates"],"mustRemove":[],"preferredOrder":[],"schemaVersion":2,"platform":"unspecified","desiredOpening":"","desiredEnding":"","recipeId":"custom","recipeVersion":"1"}
    assert parser.parse("Order: recap, introduction")["preferredOrder"]==["recap","introduction"]
    assert parser.parse("raw objective",{"tone":"friendly"})["objective"]=="raw objective"


def test_suggestion_job_commits_reviewable_plan_without_mutating_timeline(tmp_path):
    store,source,asset,transcript,brief=prepared(tmp_path)
    queue=JobQueue(tmp_path/"jobs",handlers={"suggestion":SuggestionHandler()},concurrency=1)
    try:
        job=proposal_job(store,asset,brief,transcript)
        submitted=queue.submit(job)
        completed=queue.wait(submitted["id"],5)
        assert completed["state"]=="completed", completed.get("error")
        plans=store.list_edit_plans(brief)
        assert len(plans)==1
        plan=plans[0]["plan"]
        assert plan["noAutomaticChanges"] is True
        assert plan["sourceFingerprint"]==_sha256_file(source)
        include=next(op for op in plan["operations"] if op["type"]=="include_source_range")
        assert (include["sourceInTicks"],include["sourceOutTicks"])==(1_000_000,2_400_000)
        assert include["status"]=="pending" and include["assetId"]==asset
        assert store.load_state()["timelineItems"]==[]
        duplicate=queue.submit(job)
        assert duplicate["deduplicated"] is True
    finally:
        queue.close()


def test_saved_neutral_pacing_guides_only_natural_pause_grouping(tmp_path):
    store, source, asset, transcript, _ = prepared(tmp_path)
    original = store.get_transcript(transcript)
    words = [{key:value for key,value in word.items() if key not in {"id", "segmentId", "ordinal"}} for word in original["words"]]
    words.extend({"text": word, "originalText": word, "sourceInTicks": start, "sourceOutTicks": start + 200_000, "confidence": .85}
                 for word, start in [("then", 3_200_000), ("we", 3_500_000), ("summarize", 3_800_000)])
    expanded = store.add_transcript(asset_id=asset, language="en", words=words, segments=[], generator_id="fixture-asr", generator_version="2", source_fingerprint=_sha256_file(source), artifact_id=original["artifactId"])
    constraints = {"audience":"New learners", "objective":"Teach the lesson", "tone":"clear", "aspectRatio":"source", "targetDurationMinutes":None, "mustKeep":[], "mustRemove":[], "preferredOrder":[]}
    fast_brief = store.add_brief("Fast pacing", constraints=constraints, traits={"pacing":"fast"}, trait_origin="reference-derived", generator_id="localcut-neutral-reference-traits", generator_version="1")
    slow_brief = store.add_brief("Slow pacing", constraints=constraints, traits={"pacing":"slow"}, trait_origin="reference-derived", generator_id="localcut-neutral-reference-traits", generator_version="1")
    fast_input = proposal_job(store, asset, fast_brief, expanded)
    slow_input = proposal_job(store, asset, slow_brief, expanded)
    assert fast_input.config["pacingProfile"]["value"] == "fast"
    assert slow_input.config["pacingProfile"]["value"] == "slow"
    assert fast_input.config["pacingProfile"] != slow_input.config["pacingProfile"]

    queue = JobQueue(tmp_path / "jobs-pacing", handlers={"suggestion": SuggestionHandler()}, concurrency=1)
    try:
        fast_job = queue.submit(fast_input)
        fast_done = queue.wait(fast_job["id"], 5)
        assert fast_done["state"] == "completed", fast_done.get("error")
        fast_plan = store.list_edit_plans(fast_brief)[0]["plan"]
        fast_includes = [op for op in fast_plan["operations"] if op["type"] == "include_source_range"]
        assert len(fast_includes) == 2
        assert [op["text"] for op in fast_includes] == ["we need keep this lesson", "then we summarize"]
        assert fast_plan["styleGuidance"]["value"] == "fast"
        assert fast_plan["styleGuidance"]["gapThresholdSeconds"] == .7
        assert all("does not remove speech or authorize reordering" in op["rationale"] for op in fast_includes)
        assert store.load_state()["timelineItems"] == []

        slow_job = queue.submit(slow_input)
        slow_done = queue.wait(slow_job["id"], 5)
        assert slow_done["state"] == "completed", slow_done.get("error")
        slow_plan = store.list_edit_plans(slow_brief)[0]["plan"]
        slow_includes = [op for op in slow_plan["operations"] if op["type"] == "include_source_range"]
        assert len(slow_includes) == 1
        assert slow_plan["styleGuidance"]["gapThresholdSeconds"] == 1.8
        assert "neutral slow pacing preference" in slow_plan["styleGuidance"]["explanation"]
    finally:
        queue.close()


def test_saved_pacing_trait_change_invalidates_proposal_cache(tmp_path):
    store, source, asset, transcript, _ = prepared(tmp_path)
    brief = store.add_brief("Pacing cache", constraints={"audience":"learners", "objective":"lesson", "tone":"clear", "aspectRatio":"source", "targetDurationMinutes":None, "mustKeep":[], "mustRemove":[], "preferredOrder":[]}, traits={"pacing":"fast", "punchInFrequencyPerMinute":5.0}, trait_origin="reference-derived", generator_id="localcut-neutral-reference-traits", generator_version="1")
    queue = JobQueue(tmp_path / "jobs-pacing-cache", handlers={"suggestion": SuggestionHandler()}, concurrency=1)
    try:
        first_input = proposal_job(store, asset, brief, transcript)
        first = queue.submit(first_input)
        assert queue.wait(first["id"], 5)["state"] == "completed"
        with _connect(store.database_path) as db:
            db.execute("UPDATE style_traits SET trait_value_json=? WHERE brief_id=? AND trait_key='pacing'", (json.dumps("slow"), brief))
            db.commit()
        changed_input = proposal_job(store, asset, brief, transcript)
        assert changed_input.config["pacingProfile"] != first_input.config["pacingProfile"]
        changed = queue.submit(changed_input)
        assert changed["id"] != first["id"]
        assert queue.wait(changed["id"], 5)["state"] == "completed"
        with _connect(store.database_path) as db:
            db.execute("UPDATE style_traits SET trait_value_json=? WHERE brief_id=? AND trait_key='punchInFrequencyPerMinute'", (json.dumps(0.0), brief))
            db.commit()
        punch_changed_input = proposal_job(store, asset, brief, transcript)
        assert punch_changed_input.config["punchInProfile"] != changed_input.config["punchInProfile"]
        punch_changed = queue.submit(punch_changed_input)
        assert punch_changed["id"] != changed["id"]
        assert queue.wait(punch_changed["id"], 5)["state"] == "completed"
        plans = store.list_edit_plans(brief)
        assert {plan["plan"]["styleGuidance"]["value"] for plan in plans} == {"fast", "slow"}
        assert {plan["plan"]["punchInGuidance"]["maximumSuggestions"] for plan in plans} == {0, 1}
    finally:
        queue.close()


def test_punch_in_frequency_limits_only_separate_protected_suggestions(tmp_path):
    store, source, asset, transcript, _ = prepared(tmp_path)
    original = store.get_transcript(transcript)
    words = [{key:value for key,value in word.items() if key not in {"id", "segmentId", "ordinal"}} for word in original["words"]]
    words.extend({"text": word, "sourceInTicks": start, "sourceOutTicks": start + 200_000, "confidence": .85}
                 for word, start in [("then", 5_000_000), ("keep", 5_300_000), ("the", 5_600_000), ("recap", 5_900_000)])
    expanded = store.add_transcript(asset_id=asset, language="en", words=words, segments=[], generator_id="fixture-asr", generator_version="2", source_fingerprint=_sha256_file(source), artifact_id=original["artifactId"])
    constraints = {"audience":"learners", "objective":"lesson", "tone":"clear", "aspectRatio":"source", "targetDurationMinutes":None, "mustKeep":["keep this", "keep the recap"], "mustRemove":[], "preferredOrder":[]}
    one_per_minute = store.add_brief("One emphasis per minute", constraints=constraints, traits={"punchInFrequencyPerMinute":5.0}, trait_origin="reference-derived", generator_id="localcut-neutral-reference-traits", generator_version="1")
    no_punches = store.add_brief("No emphasis", constraints=constraints, traits={"punchInFrequencyPerMinute":0.0}, trait_origin="reference-derived", generator_id="localcut-neutral-reference-traits", generator_version="1")
    queue = JobQueue(tmp_path / "jobs-punch-rate", handlers={"suggestion": SuggestionHandler()}, concurrency=1)
    try:
        proposed = queue.submit(proposal_job(store, asset, one_per_minute, expanded))
        assert queue.wait(proposed["id"], 5)["state"] == "completed"
        plan = store.list_edit_plans(one_per_minute)[0]["plan"]
        includes = [op for op in plan["operations"] if op["type"] == "include_source_range"]
        punches = [op for op in plan["operations"] if op["type"] == "punch_in"]
        assert len([op for op in includes if op["protected"]]) == 2
        assert len(punches) == 1
        assert plan["punchInGuidance"]["maximumSuggestions"] == 1
        protected_ids = {op["id"] for op in includes if op["protected"]}
        assert punches[0]["relatedIncludeSuggestionId"] in protected_ids

        zero = queue.submit(proposal_job(store, asset, no_punches, expanded))
        assert queue.wait(zero["id"], 5)["state"] == "completed"
        zero_plan = store.list_edit_plans(no_punches)[0]["plan"]
        assert not any(op["type"] == "punch_in" for op in zero_plan["operations"])
        assert zero_plan["punchInGuidance"]["maximumSuggestions"] == 0
    finally:
        queue.close()


def test_proposal_acceptance_is_explicit_and_edits_only_selected_range(tmp_path):
    store,_,asset,transcript,brief=prepared(tmp_path)
    queue=JobQueue(tmp_path/"jobs-apply",handlers={"suggestion":SuggestionHandler()},concurrency=1)
    try:
        submitted=queue.submit(proposal_job(store,asset,brief,transcript))
        assert queue.wait(submitted["id"],5)["state"]=="completed"
    finally:
        queue.close()
    proposal=store.list_edit_plans(brief)[0]
    operation=next(op for op in proposal["plan"]["operations"] if op["type"]=="include_source_range")
    before=store.load_state()["revision"]
    response=store.decide_edit_plan_suggestion(proposal["id"],operation["id"],"accepted",expected_revision=before)
    state=store.load_state()
    assert response["revision"]==state["revision"]
    assert len(state["timelineItems"])==1
    assert state["timelineItems"][0]["properties"]["originSuggestionId"]==operation["id"]
    assert store.decide_edit_plan_suggestion(proposal["id"],operation["id"],"pending",expected_revision=state["revision"])["status"]=="pending"
    assert store.load_state()["timelineItems"]==[]


def test_transcript_review_edits_and_range_markers_invalidate_proposal_cache(tmp_path):
    store,_,asset,transcript,brief=prepared(tmp_path)
    before=proposal_job(store,asset,brief,transcript)
    store.set_transcript_range_marker(transcript,1_000_000,2_000_000,"required")
    after_marker=proposal_job(store,asset,brief,transcript)
    assert before.config["transcriptReviewFingerprint"] != after_marker.config["transcriptReviewFingerprint"]
    segment=store.get_transcript(transcript)["segments"]
    if segment:
        store.update_transcript_segment(transcript,segment[0]["id"],"reviewed wording")
        after_edit=proposal_job(store,asset,brief,transcript)
        assert after_marker.config["transcriptReviewFingerprint"] != after_edit.config["transcriptReviewFingerprint"]


def test_must_remove_phrase_is_omitted_from_candidate_passages(tmp_path):
    store,_,asset,transcript,_=prepared(tmp_path)
    brief=store.add_brief("Remove setup",constraints={"audience":"learners","objective":"lesson","tone":"clear","aspectRatio":"source","targetDurationMinutes":None,"mustKeep":[],"mustRemove":["this"]})
    queue=JobQueue(tmp_path/"jobs-remove",handlers={"suggestion":SuggestionHandler()},concurrency=1)
    try:
        submitted=queue.submit(proposal_job(store,asset,brief,transcript))
        result=queue.wait(submitted["id"],5)
        assert result["state"]=="completed",result.get("error")
        operations=store.list_edit_plans(brief)[0]["plan"]["operations"]
        included=[op for op in operations if op["type"]=="include_source_range"]
        assert all("this" not in op.get("text","").split() for op in included)
        assert not any(op["type"]=="exclude_source_range" for op in operations)
    finally:
        queue.close()


def test_required_transcript_marker_overrides_matching_must_remove_phrase(tmp_path):
    store,_,asset,transcript,_=prepared(tmp_path)
    store.set_transcript_range_marker(transcript,1_900_000,2_100_000,"required")
    brief=store.add_brief("Preserve required range",constraints={"audience":"learners","objective":"lesson","tone":"clear","aspectRatio":"source","targetDurationMinutes":None,"mustKeep":[],"mustRemove":["this"]})
    queue=JobQueue(tmp_path/"jobs-required",handlers={"suggestion":SuggestionHandler()},concurrency=1)
    try:
        submitted=queue.submit(proposal_job(store,asset,brief,transcript))
        result=queue.wait(submitted["id"],5)
        assert result["state"]=="completed",result.get("error")
        operations=store.list_edit_plans(brief)[0]["plan"]["operations"]
        included=[op for op in operations if op["type"]=="include_source_range"]
        assert any(op["protected"] and "this" in op["text"].split() for op in included)
        assert not any(op["type"]=="exclude_source_range" for op in operations)
    finally:
        queue.close()


def test_preferred_order_creates_separate_ordered_review_operation(tmp_path):
    store,source,asset,transcript,brief_id=prepared(tmp_path)
    original=store.get_transcript(transcript)
    second_group=[{"text":word,"originalText":word,"sourceInTicks":start,"sourceOutTicks":start+200_000,"confidence":.85} for word,start in [("then",5_000_000),("show",5_300_000),("the",5_600_000),("recap",5_900_000)]]
    expanded_words=[{"text":word["text"],"sourceInTicks":word["sourceInTicks"],"sourceOutTicks":word["sourceOutTicks"],"confidence":word["confidence"]} for word in original["words"]]+second_group
    expanded_id=store.add_transcript(asset_id=asset,language="en",words=expanded_words,segments=[],generator_id="fixture-asr",generator_version="2",source_fingerprint=_sha256_file(source),artifact_id=original["artifactId"])
    brief_id=store.add_brief("Preferred order: recap, lesson",constraints={"audience":"New learners","objective":"Teach the lesson","tone":"clear","aspectRatio":"source","targetDurationMinutes":None,"mustKeep":[],"mustRemove":[],"preferredOrder":["recap","lesson"]})
    queue=JobQueue(tmp_path/"jobs-order-proposal",handlers={"suggestion":SuggestionHandler()},concurrency=1)
    try:
        submitted=queue.submit(proposal_job(store,asset,brief_id,expanded_id))
        completed=queue.wait(submitted["id"],5)
        assert completed["state"]=="completed",completed.get("error")
        plan=store.list_edit_plans(brief_id)[0]
        includes={op["id"]:op for op in plan["plan"]["operations"] if op["type"]=="include_source_range"}
        reorder=next(op for op in plan["plan"]["operations"] if op["type"]=="reorder_passages")
        assert reorder["status"]=="pending"
        assert set(reorder["orderedSuggestionIds"])==set(includes)
        assert reorder["orderedPassages"][0]["text"].endswith("recap")
        assert reorder["orderedPassages"][1]["text"].endswith("lesson")
        assert reorder["requiresSuggestionIds"]==[op["id"] for op in sorted(includes.values(),key=lambda op:op["sourceInTicks"])]
    finally:
        queue.close()


def test_ambiguous_preferred_order_phrase_does_not_create_reorder_operation(tmp_path):
    store,source,asset,transcript,_=prepared(tmp_path)
    original=store.get_transcript(transcript)
    second_group=[{"text":word,"sourceInTicks":start,"sourceOutTicks":start+200_000,"confidence":.85} for word,start in [("this",5_000_000),("is",5_300_000),("a",5_600_000),("recap",5_900_000)]]
    expanded_words=[{"text":word["text"],"sourceInTicks":word["sourceInTicks"],"sourceOutTicks":word["sourceOutTicks"],"confidence":word["confidence"]} for word in original["words"]]+second_group
    expanded_id=store.add_transcript(asset_id=asset,language="en",words=expanded_words,segments=[],generator_id="fixture-asr",generator_version="2",source_fingerprint=_sha256_file(source),artifact_id=original["artifactId"])
    brief_id=store.add_brief("Ambiguous order",constraints={"audience":"learners","objective":"lesson","tone":"clear","aspectRatio":"source","targetDurationMinutes":None,"mustKeep":[],"mustRemove":[],"preferredOrder":["this","recap"]})
    queue=JobQueue(tmp_path/"jobs-order-ambiguous",handlers={"suggestion":SuggestionHandler()},concurrency=1)
    try:
        submitted=queue.submit(proposal_job(store,asset,brief_id,expanded_id))
        completed=queue.wait(submitted["id"],5)
        assert completed["state"]=="completed",completed.get("error")
        plan=store.list_edit_plans(brief_id)[0]["plan"]
        assert not any(op["type"]=="reorder_passages" for op in plan["operations"])
        assert any("matched more than one kept passage" in warning["message"] for warning in plan["warnings"])
    finally:
        queue.close()
