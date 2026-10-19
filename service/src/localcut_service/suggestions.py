"""Conservative, local first-cut proposals from a brief and verified transcript."""
from __future__ import annotations

import json
import hashlib
import math
from pathlib import Path
import re
from typing import Any
import uuid

from .jobs import JobContext, JobInput, JobOutput
from .persistence import ProjectStore, _connect, _sha256_file
from .proxies import source_digest, source_path

GENERATOR_ID = "localcut-conservative-cut-proposer"
GENERATOR_VERSION = "5"
PIPELINE_VERSION = "first-cut-1"
PACING_GAP_SECONDS = {"fast": 0.7, "moderate": 1.2, "slow": 1.8}


def _review_fingerprint(transcript: dict[str, Any]) -> str:
    content=json.dumps({"segments":transcript.get("segments",[]),"words":transcript.get("words",[]),"markers":transcript.get("markers",[])},ensure_ascii=False,sort_keys=True,separators=(",",":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _pacing_profile(connection, brief_id: str) -> dict[str, Any] | None:
    row = connection.execute("SELECT trait_value_json,origin,generator_id,generator_version FROM style_traits WHERE brief_id=? AND trait_key='pacing'", (brief_id,)).fetchone()
    if row is None:
        return None
    value = json.loads(row["trait_value_json"])
    if value not in PACING_GAP_SECONDS:
        return None
    return {"value": value, "origin": row["origin"], "generatorId": row["generator_id"], "generatorVersion": row["generator_version"]}


def _punch_in_profile(connection, brief_id: str) -> dict[str, Any] | None:
    row = connection.execute("SELECT trait_value_json,origin,generator_id,generator_version FROM style_traits WHERE brief_id=? AND trait_key='punchInFrequencyPerMinute'", (brief_id,)).fetchone()
    if row is None:
        return None
    value = json.loads(row["trait_value_json"])
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return {"value": float(value), "origin": row["origin"], "generatorId": row["generator_id"], "generatorVersion": row["generator_version"]}


def proposal_job(store: ProjectStore, asset_id: str, brief_id: str, transcript_id: str) -> JobInput:
    with _connect(store.database_path) as db:
        asset=db.execute("SELECT * FROM assets WHERE id=? AND project_id=?",(asset_id,store.project_id)).fetchone()
        brief=db.execute("SELECT * FROM briefs WHERE id=? AND project_id=?",(brief_id,store.project_id)).fetchone()
        transcript=db.execute("SELECT t.*,a.checksum_sha256 artifact_checksum,a.relative_path artifact_path FROM transcripts t JOIN artifacts a ON a.id=t.artifact_id WHERE t.id=? AND t.project_id=? AND t.asset_id=? AND a.status='ready'",(transcript_id,store.project_id,asset_id)).fetchone()
        pacing_profile = _pacing_profile(db, brief_id) if brief else None
        punch_in_profile = _punch_in_profile(db, brief_id) if brief else None
        if asset is None: raise ValueError("Select an imported source asset.")
        if brief is None: raise ValueError("Save a creative brief before generating a proposal.")
        if transcript is None or transcript["source_fingerprint"]!=asset["sha256"]: raise ValueError("Create a transcript for this source before generating a proposal.")
    review_fingerprint=_review_fingerprint(store.get_transcript(transcript_id))
    config={"briefId":brief_id,"briefRevision":brief["revision"],"transcriptId":transcript_id,"transcriptArtifactId":transcript["artifact_id"],"transcriptChecksum":transcript["artifact_checksum"],"transcriptReviewFingerprint":review_fingerprint,"pacingProfile":pacing_profile,"punchInProfile":punch_in_profile}
    return JobInput(str(store.root),"suggestion",{"assetId":asset_id,"briefId":brief_id,"transcriptId":transcript_id},asset["sha256"],pipeline_version=PIPELINE_VERSION,asset_id=asset_id,generator_id=GENERATOR_ID,generator_version=GENERATOR_VERSION,config=config)


def _normalized(text: str) -> str:
    return " ".join(re.findall(r"[\w']+",text.casefold()))


def _find_topic_ranges(words: list[dict[str, Any]], terms: list[str]) -> list[tuple[int,int,str]]:
    tokens=[_normalized(str(word.get("text",word.get("originalText","")))) for word in words]
    found=[]
    for phrase in terms:
        target=_normalized(phrase)
        if not target: continue
        for start in range(len(tokens)):
            assembled=""
            for end in range(start,min(len(tokens),start+40)):
                assembled=(assembled+" "+tokens[end]).strip()
                if assembled==target:
                    found.append((int(words[start]["sourceInTicks"]),int(words[end]["sourceOutTicks"]),phrase)); break
                if not target.startswith(assembled): break
    return found


def _subtract(start: int, end: int, cuts: list[tuple[int,int]]) -> list[tuple[int,int]]:
    intervals=[(start,end)]
    for cut_start,cut_end in cuts:
        next_intervals=[]
        for a,b in intervals:
            if cut_end<=a or cut_start>=b: next_intervals.append((a,b)); continue
            if a<cut_start: next_intervals.append((a,min(b,cut_start)))
            if cut_end<b: next_intervals.append((max(a,cut_end),b))
        intervals=next_intervals
    return [(a,b) for a,b in intervals if b>a]


class SuggestionHandler:
    kind="suggestion"
    retry_safe=True

    def validate_input(self,payload:dict[str,Any])->dict[str,str]:
        if not isinstance(payload,dict) or not all(isinstance(payload.get(key),str) and payload[key] for key in ("assetId","briefId","transcriptId")):
            raise ValueError("First-cut proposal requires an asset, saved brief, and transcript.")
        return {key:payload[key] for key in ("assetId","briefId","transcriptId")}

    def validate_job(self,job:JobInput)->None:
        store=ProjectStore.open(Path(job.project_path))
        if job.asset_id!=job.payload.get("assetId") or job.generator_id!=GENERATOR_ID or job.generator_version!=GENERATOR_VERSION:
            raise ValueError("Suggestion generator provenance does not match this job.")
        with _connect(store.database_path) as db:
            asset=db.execute("SELECT * FROM assets WHERE id=? AND project_id=?",(job.asset_id,store.project_id)).fetchone()
            brief=db.execute("SELECT * FROM briefs WHERE id=? AND project_id=?",(job.config.get("briefId"),store.project_id)).fetchone()
            pacing_profile = _pacing_profile(db, str(job.config.get("briefId"))) if brief else None
            punch_in_profile = _punch_in_profile(db, str(job.config.get("briefId"))) if brief else None
            transcript=db.execute("SELECT t.*,a.checksum_sha256 artifact_checksum,a.relative_path artifact_path FROM transcripts t JOIN artifacts a ON a.id=t.artifact_id WHERE t.id=? AND t.project_id=? AND t.asset_id=? AND a.status='ready'",(job.config.get("transcriptId"),store.project_id,job.asset_id)).fetchone()
        if asset is None or asset["sha256"]!=job.source_fingerprint or source_digest(source_path(store,asset))!=job.source_fingerprint:
            raise ValueError("Source changed since import; reimport before proposing edits.")
        if brief is None or int(brief["revision"])!=int(job.config.get("briefRevision",-1)):
            raise ValueError("The creative brief changed; generate a proposal from its latest saved revision.")
        if pacing_profile != job.config.get("pacingProfile"):
            raise ValueError("The saved pacing trait changed; resubmit the proposal to refresh its style guidance.")
        if punch_in_profile != job.config.get("punchInProfile"):
            raise ValueError("The saved punch-in frequency trait changed; resubmit the proposal to refresh its style guidance.")
        if transcript is None or transcript["source_fingerprint"]!=job.source_fingerprint or transcript["artifact_id"]!=job.config.get("transcriptArtifactId") or transcript["artifact_checksum"]!=job.config.get("transcriptChecksum"):
            raise ValueError("Transcript provenance changed; transcribe again before generating a proposal.")
        if _review_fingerprint(store.get_transcript(str(job.config.get("transcriptId")))) != job.config.get("transcriptReviewFingerprint"):
            raise ValueError("Transcript review edits or markers changed; resubmit the proposal so its cache matches the current review.")
        transcript_path=(store.root/transcript["artifact_path"]).resolve(); transcript_path.relative_to(store.root.resolve())
        if _sha256_file(transcript_path)!=transcript["artifact_checksum"]: raise ValueError("Transcript artifact failed checksum validation.")

    def run(self,context:JobContext,payload:dict[str,str])->JobOutput:
        self.validate_job(context.input)
        store=ProjectStore.open(Path(context.input.project_path))
        with _connect(store.database_path) as db:
            asset=db.execute("SELECT duration_ticks FROM assets WHERE id=? AND project_id=?",(payload["assetId"],store.project_id)).fetchone()
            brief=db.execute("SELECT * FROM briefs WHERE id=? AND project_id=?",(payload["briefId"],store.project_id)).fetchone()
            silence=db.execute("SELECT id,artifact_id,source_in_ticks,source_out_ticks,generator_id,generator_version FROM analyses WHERE asset_id=? AND kind='silence' AND status='ready' AND source_fingerprint=? ORDER BY source_in_ticks",(payload["assetId"],context.input.source_fingerprint)).fetchall()
        transcript=store.get_transcript(payload["transcriptId"])
        words=transcript["words"]
        if not words: raise ValueError("The transcript has no word timestamps. Review or transcribe this footage before proposing a cut.")
        constraints=json.loads(brief["constraints_json"])
        keep=[str(value) for value in constraints.get("mustKeep",[]) if str(value).strip()]
        remove=[str(value) for value in constraints.get("mustRemove",[]) if str(value).strip()]
        preferred_order=[str(value) for value in constraints.get("preferredOrder",[]) if str(value).strip()]
        pacing_profile=context.input.config.get("pacingProfile")
        pacing=pacing_profile.get("value") if isinstance(pacing_profile,dict) else None
        gap_threshold=PACING_GAP_SECONDS.get(str(pacing),PACING_GAP_SECONDS["moderate"])
        style_guidance={"trait":"pacing","value":pacing,"origin":pacing_profile.get("origin"),"generatorId":pacing_profile.get("generatorId"),"generatorVersion":pacing_profile.get("generatorVersion"),"gapThresholdSeconds":gap_threshold,"explanation":f"The saved neutral {pacing} pacing preference separates transcript passages at natural speech gaps of {gap_threshold:.1f} seconds or longer. This cue changes pause-based grouping only; it does not remove speech or authorize reordering. Other edits still follow the brief and protected ranges. Review each passage against playback."} if pacing in PACING_GAP_SECONDS else None
        punch_in_profile=context.input.config.get("punchInProfile")
        punch_rate=float(punch_in_profile["value"]) if isinstance(punch_in_profile,dict) else None
        punch_limit=8 if punch_rate is None else min(8,max(0,math.floor(punch_rate*max(0,int(asset["duration_ticks"] or 0))/60_000_000+0.5)))
        punch_guidance={"trait":"punchInFrequencyPerMinute","value":punch_rate,"origin":punch_in_profile.get("origin"),"generatorId":punch_in_profile.get("generatorId"),"generatorVersion":punch_in_profile.get("generatorVersion"),"maximumSuggestions":punch_limit,"explanation":f"The saved neutral profile sets a target of about {punch_rate:g} punch-ins per minute. At this source duration, no more than {punch_limit} optional punch-ins are proposed, and only on brief-protected passages. Every crop remains a separate review decision."} if punch_rate is not None else None
        markers=transcript.get("markers",[])
        excluded=[(int(m["sourceInTicks"]),int(m["sourceOutTicks"])) for m in markers if m["marker"]=="excluded"]
        required=[(int(m["sourceInTicks"]),int(m["sourceOutTicks"])) for m in markers if m["marker"]=="required"]
        uncertain=[(int(m["sourceInTicks"]),int(m["sourceOutTicks"])) for m in markers if m["marker"]=="uncertain"]
        remove_ranges=_find_topic_ranges(words,remove)
        keep_ranges=_find_topic_ranges(words,keep)
        for segment in transcript.get("segments",[]):
            if not segment.get("editedText"): continue
            segment_text=_normalized(str(segment["editedText"]))
            for phrase in keep:
                if _normalized(phrase) in segment_text and not any(found.casefold()==phrase.casefold() and a<int(segment["sourceOutTicks"]) and b>int(segment["sourceInTicks"]) for a,b,found in keep_ranges):
                    keep_ranges.append((int(segment["sourceInTicks"]),int(segment["sourceOutTicks"]),phrase))
            for phrase in remove:
                if _normalized(phrase) in segment_text and not any(found.casefold()==phrase.casefold() and a<int(segment["sourceOutTicks"]) and b>int(segment["sourceInTicks"]) for a,b,found in remove_ranges):
                    remove_ranges.append((int(segment["sourceInTicks"]),int(segment["sourceOutTicks"]),phrase))
        protected_ranges=required+[(a,b) for a,b,_ in keep_ranges]
        raw_cuts=excluded+[(a,b) for a,b,_ in remove_ranges]
        cuts=[piece for a,b in raw_cuts for piece in _subtract(a,b,protected_ranges)]
        existing_clips=[item for item in store.load_state().get("timelineItems",[]) if item.get("itemType")=="clip" and item.get("assetId")==payload["assetId"]]
        groups=[]; current=[]
        for word in words:
            context.check_cancelled()
            if current and int(word["sourceInTicks"])-int(current[-1]["sourceOutTicks"])>round(gap_threshold*1_000_000):
                groups.append(current); current=[]
            current.append(word)
        if current: groups.append(current)
        operations=[]; rationale=[]
        for start,end in cuts:
            context.check_cancelled()
            if not any(item["sourceInTicks"]<end and item["sourceOutTicks"]>start for item in existing_clips):
                rationale.append({"severity":"info","message":f"Source passage {start/1e6:.2f}–{end/1e6:.2f}s is omitted from the proposed first-cut passages. No timeline exclusion operation is needed until a matching clip exists."})
                continue
            matches=[phrase for a,b,phrase in remove_ranges if a<end and b>start]
            operations.append({"id":str(uuid.uuid4()),"type":"exclude_source_range","assetId":payload["assetId"],"sourceInTicks":start,"sourceOutTicks":end,"text":"; ".join(matches) or "Excluded transcript range","status":"pending","confidence":None,"rationale":"This source range overlaps an existing timeline clip and is excluded or matches a must-remove brief phrase. Review before accepting."})
        for group in groups:
            context.check_cancelled()
            start=int(group[0]["sourceInTicks"]); end=int(group[-1]["sourceOutTicks"])
            group_cuts=[(max(start,a),min(end,b)) for a,b in cuts if a<end and b>start]
            for a,b in _subtract(start,end,group_cuts):
                passage_words=[w for w in group if int(w["sourceInTicks"])<b and int(w["sourceOutTicks"])>a]
                matches_keep=[term for term in keep if any(a<right and b>left and term.casefold()==phrase.casefold() for left,right,phrase in keep_ranges)]
                matches_required=any(a<right and b>left for left,right in required)
                confidences=[float(w["confidence"]) for w in passage_words if w.get("confidence") is not None]
                uncertain_timing=any(bool(w.get("timingUncertain")) for w in passage_words) or any(a<right and b>left for left,right in uncertain)
                text=" ".join(str(w.get("text",w.get("originalText",""))).strip() for w in passage_words).strip()
                rationale_text="Keep this transcript-timed passage in source order. Long speech gaps are separated so accepting passages removes the empty pause from the assembled first cut."
                if matches_keep or matches_required: rationale_text="Protected by a must-keep topic or required transcript marker; preserve this passage in the first cut."
                if style_guidance: rationale_text += " " + style_guidance["explanation"]
                if uncertain_timing: rationale_text+=" Timing is uncertain or this range is marked uncertain; verify playback before accepting."
                operations.append({"id":str(uuid.uuid4()),"type":"include_source_range","assetId":payload["assetId"],"sourceInTicks":a,"sourceOutTicks":b,"text":text[:500],"status":"pending","confidence":sum(confidences)/len(confidences) if confidences else None,"protected":bool(matches_keep or matches_required),"timingUncertain":uncertain_timing,"rationale":rationale_text,"transcriptId":payload["transcriptId"],"analysisArtifactIds":[row["artifact_id"] for row in silence if row["artifact_id"] and row["source_in_ticks"]<b and row["source_out_ticks"]>a]})
        # Protected brief topics are eligible for subtle emphasis, but the crop is
        # a separate pending operation and is never applied with the include.
        seen_emphasis=set()
        for passage in [item for item in operations if item["type"]=="include_source_range"]:
            matching_topics=[phrase for a,b,phrase in keep_ranges if a<int(passage["sourceOutTicks"]) and b>int(passage["sourceInTicks"])]
            has_required=any(a<int(passage["sourceOutTicks"]) and b>int(passage["sourceInTicks"]) for a,b in required)
            key=passage["id"]
            if (not matching_topics and not has_required) or key in seen_emphasis or len(seen_emphasis)>=punch_limit: continue
            seen_emphasis.add(key)
            label=matching_topics[0] if matching_topics else "a required transcript range"
            # The existing crop control applies to a whole timeline clip, so the
            # suggestion range deliberately matches this related include operation.
            operations.append({"id":str(uuid.uuid4()),"type":"punch_in","assetId":payload["assetId"],"sourceInTicks":passage["sourceInTicks"],"sourceOutTicks":passage["sourceOutTicks"],"status":"pending","confidence":None,"cropPreset":"punch_in_110","rationale":f"This kept passage contains brief-protected material ({label}). A subtle 110% center punch-in would apply across this entire clip; review playback and accept it separately from keeping the passage.","relatedIncludeSuggestionId":passage["id"],"transcriptId":payload["transcriptId"]})
        include_ops=sorted((item for item in operations if item["type"]=="include_source_range"),key=lambda item:(item["sourceInTicks"],item["sourceOutTicks"]))
        if preferred_order:
            selected=[]; order_problems=[]
            for phrase in preferred_order:
                needle=_normalized(phrase)
                matches=[item for item in include_ops if f" {needle} " in f" {_normalized(item.get('text',''))} "] if needle else []
                if len(matches)==1:
                    selected.append(matches[0])
                elif not matches:
                    order_problems.append(f'"{phrase}" did not match a kept passage')
                else:
                    order_problems.append(f'"{phrase}" matched more than one kept passage')
            if len({item["id"] for item in selected}) != len(selected):
                order_problems.append("multiple order phrases matched the same passage")
            if order_problems:
                rationale.append({"severity":"warning","message":"Preferred passage order was not proposed because "+"; ".join(order_problems)+". Edit the phrases so each identifies one distinct kept passage."})
            elif len(selected)<2:
                rationale.append({"severity":"warning","message":"Preferred passage order needs at least two distinct kept passages; no reordering operation was proposed."})
            else:
                ordered=selected+[item for item in include_ops if item["id"] not in {chosen["id"] for chosen in selected}]
                operations.append({"id":str(uuid.uuid4()),"type":"reorder_passages","assetId":payload["assetId"],"sourceInTicks":min(item["sourceInTicks"] for item in include_ops),"sourceOutTicks":max(item["sourceOutTicks"] for item in include_ops),"text":f"Apply the brief's preferred order to {len(ordered)} kept passages.","status":"pending","confidence":None,"rationale":"The brief names a preferred order. This separate operation changes only the timeline order of these kept source ranges; accept every listed passage first, then review and accept this reorder.","orderedSuggestionIds":[item["id"] for item in ordered],"requiresSuggestionIds":[item["id"] for item in include_ops],"orderedPassages":[{"suggestionId":item["id"],"sourceInTicks":item["sourceInTicks"],"sourceOutTicks":item["sourceOutTicks"],"text":item.get("text","")} for item in ordered]})
        missing_keep=[term for term in keep if not any(term.casefold()==phrase.casefold() for _,_,phrase in keep_ranges)]
        for phrase in missing_keep: rationale.append({"severity":"warning","message":f"Must-keep topic not found in transcript: {phrase}. No related speech was removed automatically."})
        duration_minutes=constraints.get("targetDurationMinutes")
        planned_seconds=sum((op["sourceOutTicks"]-op["sourceInTicks"])/1_000_000 for op in operations if op["type"]=="include_source_range")
        if duration_minutes and planned_seconds>float(duration_minutes)*60:
            rationale.append({"severity":"warning","message":f"Transcript-timed passages total about {planned_seconds/60:.1f} minutes, above the {float(duration_minutes):g}-minute target. The proposal keeps speech instead of dropping material solely to meet duration; review passages manually."})
        operations.sort(key=lambda op:(op["sourceInTicks"],0 if op["type"]=="exclude_source_range" else 1))
        context.report(.85,"validating proposal",f"Prepared {len(operations)} reviewable operations.")
        plan={"schemaVersion":1,"assetId":payload["assetId"],"sourceDurationTicks":int(asset["duration_ticks"] or 0),"briefId":payload["briefId"],"briefRevision":brief["revision"],"transcriptId":payload["transcriptId"],"sourceFingerprint":context.input.source_fingerprint,"transcriptArtifactId":context.input.config["transcriptArtifactId"],"transcriptReviewFingerprint":context.input.config["transcriptReviewFingerprint"],"pacingProfile":pacing_profile,"styleGuidance":style_guidance,"punchInProfile":punch_in_profile,"punchInGuidance":punch_guidance,"generatorId":GENERATOR_ID,"generatorVersion":GENERATOR_VERSION,"constraints":constraints,"operations":operations,"warnings":rationale,"noAutomaticChanges":True}
        output=context.staging_file(".json"); output.write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding="utf-8")
        with _connect(store.database_path) as db:
            source_asset=db.execute("SELECT * FROM assets WHERE id=? AND project_id=?",(payload["assetId"],store.project_id)).fetchone()
        if source_asset is None or source_digest(source_path(store,source_asset),context)!=context.input.source_fingerprint: raise ValueError("Source changed during proposal generation; discard and retry.")
        self.validate_job(context.input)
        return JobOutput(str(output),f"cache/suggestions/{context.job_id}.json","edit-proposal",{"assetId":payload["assetId"],"briefId":payload["briefId"],"transcriptId":payload["transcriptId"],"generatorId":GENERATOR_ID,"generatorVersion":GENERATOR_VERSION,"config":context.input.config})

    def validate_output(self,output:JobOutput)->None:
        data=json.loads(Path(output.staging_path).read_text(encoding="utf-8"))
        if data.get("noAutomaticChanges") is not True or not isinstance(data.get("operations"),list) or not all(isinstance(data.get(key),str) and data[key] for key in ("assetId","briefId","transcriptId","sourceFingerprint","transcriptArtifactId","transcriptReviewFingerprint")) or data.get("generatorId")!=GENERATOR_ID or data.get("generatorVersion")!=GENERATOR_VERSION or type(data.get("sourceDurationTicks")) is not int or data["sourceDurationTicks"]<=0: raise ValueError("Proposal output is missing source, review, or generator provenance.")
        profile=data.get("pacingProfile"); guidance=data.get("styleGuidance")
        if profile is None:
            if guidance is not None: raise ValueError("Proposal contains pacing guidance without a saved trait profile.")
        elif not isinstance(profile,dict) or profile.get("value") not in PACING_GAP_SECONDS or not all(isinstance(profile.get(key),str) and profile[key] for key in ("origin","generatorId","generatorVersion")) or not isinstance(guidance,dict) or guidance.get("trait")!="pacing" or guidance.get("value")!=profile["value"] or guidance.get("gapThresholdSeconds")!=PACING_GAP_SECONDS[profile["value"]] or not isinstance(guidance.get("explanation"),str) or not guidance["explanation"].strip():
            raise ValueError("Proposal pacing guidance does not match its saved neutral trait provenance.")
        punch_profile=data.get("punchInProfile"); punch_guidance=data.get("punchInGuidance")
        if punch_profile is None:
            if punch_guidance is not None: raise ValueError("Proposal contains punch-in guidance without a saved trait profile.")
            punch_limit=8
        elif not isinstance(punch_profile,dict) or isinstance(punch_profile.get("value"),bool) or not isinstance(punch_profile.get("value"),(int,float)) or not math.isfinite(punch_profile["value"]) or punch_profile["value"]<0 or not all(isinstance(punch_profile.get(key),str) and punch_profile[key] for key in ("origin","generatorId","generatorVersion")) or not isinstance(punch_guidance,dict) or punch_guidance.get("trait")!="punchInFrequencyPerMinute" or punch_guidance.get("value")!=punch_profile["value"] or type(punch_guidance.get("maximumSuggestions")) is not int or not isinstance(punch_guidance.get("explanation"),str) or not punch_guidance["explanation"].strip():
            raise ValueError("Proposal punch-in guidance does not match its saved neutral trait provenance.")
        else:
            punch_limit=min(8,max(0,math.floor(punch_profile["value"]*data["sourceDurationTicks"]/60_000_000+0.5)))
            if punch_guidance["maximumSuggestions"]!=punch_limit: raise ValueError("Proposal punch-in limit does not match the saved trait and source duration.")
        punch_operations=[op for op in data["operations"] if op.get("type")=="punch_in"]
        if len(punch_operations)>punch_limit: raise ValueError("Proposal exceeds the approved punch-in suggestion count.")
        includes={op.get("id"):op for op in data["operations"] if op.get("type")=="include_source_range"}
        for op in punch_operations:
            include=includes.get(op.get("relatedIncludeSuggestionId"))
            if include is None or not include.get("protected") or op.get("sourceInTicks")!=include.get("sourceInTicks") or op.get("sourceOutTicks")!=include.get("sourceOutTicks"):
                raise ValueError("Punch-in suggestions must stay attached to an exact brief-protected passage.")
        for op in data["operations"]:
            if op.get("assetId")!=data["assetId"] or op.get("type") not in {"include_source_range","exclude_source_range","punch_in","reorder_passages"} or op.get("status")!="pending" or type(op.get("sourceInTicks")) is not int or type(op.get("sourceOutTicks")) is not int or op["sourceInTicks"]<0 or op["sourceOutTicks"]<=op["sourceInTicks"] or op["sourceOutTicks"]>data["sourceDurationTicks"] or not isinstance(op.get("rationale"),str) or not op["rationale"].strip(): raise ValueError("Proposal contains an invalid operation.")
            if op["type"]=="punch_in" and op.get("cropPreset")!="punch_in_110": raise ValueError("Punch-in suggestions must use the supported subtle crop preset.")
            if op["type"]=="reorder_passages":
                include_ids={item.get("id") for item in data["operations"] if item.get("type")=="include_source_range"}
                ordered=op.get("orderedSuggestionIds"); required=op.get("requiresSuggestionIds"); passages=op.get("orderedPassages")
                if not isinstance(ordered,list) or len(ordered)<2 or any(not isinstance(value,str) for value in ordered) or len(ordered)!=len(set(ordered)) or set(ordered)!=include_ids or not isinstance(required,list) or any(not isinstance(value,str) for value in required) or set(required)!=include_ids or not isinstance(passages,list) or any(not isinstance(row,dict) for row in passages) or [row.get("suggestionId") for row in passages]!=ordered:
                    raise ValueError("Passage order must reference every kept passage exactly once.")
                by_id={item["id"]:item for item in data["operations"] if item.get("type")=="include_source_range"}
                for row in passages:
                    source=by_id[row["suggestionId"]]
                    if row.get("sourceInTicks")!=source["sourceInTicks"] or row.get("sourceOutTicks")!=source["sourceOutTicks"] or not isinstance(row.get("text"),str):
                        raise ValueError("Passage order source ranges do not match their kept suggestions.")
            confidence=op.get("confidence")
            if confidence is not None and (not isinstance(confidence,(int,float)) or not 0<=confidence<=1): raise ValueError("Proposal confidence must be between 0 and 1 or unavailable.")

    def commit_output(self,connection,store:ProjectStore,job_input:JobInput,artifact_id:str,target:Path,metadata:dict[str,Any]|None=None)->None:
        plan=json.loads(target.read_text(encoding="utf-8"))
        store.add_edit_plan(plan,status="pending",rationale=plan.get("warnings",[]),generator_id=GENERATOR_ID,generator_version=GENERATOR_VERSION,brief_id=plan["briefId"],connection=connection)
