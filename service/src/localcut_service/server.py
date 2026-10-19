"""Newline-delimited JSON stdio server used only by the local Tauri supervisor."""

import json
import sys
import math
from fractions import Fraction
from pathlib import Path
from typing import Any
from . import __version__
from .config import AppConfig, load_config
from .health import check_ffmpeg, check_storage
from .logging_setup import configure_logging
from .contracts import PROTOCOL_VERSION
from .persistence import ProjectCatalog, ProjectError, ProjectStore, UnsupportedSchemaError, _connect
from .integrity import IntegrityDigestCache
from .media import FFprobe, import_media, relink_media
from .capabilities import collect_capabilities
from .jobs import JobInput, JobQueue
from .proxies import ProxyHandler, proxy_job, source_path
from .proxies import PIPELINE_VERSION as PROXY_PIPELINE_VERSION
from .transcription import CATALOG, REVISION, choose_model, model_status, ModelDownloadHandler, TranscriptionHandler, transcription_job, ready_proxy
from .preview import PreviewServer
from .analysis import AnalysisHandler, DETECTORS, analysis_job, catalog as analysis_catalog, _transcript_review_fingerprint
from .briefs import DeterministicBriefParser
from .style_recipes import recipe_catalog, propose_recipe, GENERATOR_ID as RECIPE_GENERATOR_ID, GENERATOR_VERSION as RECIPE_GENERATOR_VERSION
from .runtime_service import configured_runtime, runtime_status, summarize_asset
from .model_runtime import RuntimeFailure
from .footage_index import IndexHandler, indexing_job, search_index, index_status
from .planning import PlanningHandler, planning_job
from .revisions import revision_list, revision_generate, revision_propose, revision_decide
from .timeline import timeline_state, timeline_command, timeline_undo, timeline_redo, compile_plan
from .plan_review import list_written_plans, review_plan
from .suggestions import SuggestionHandler, proposal_job
from .render import RenderHandler, render_job, list_exports, export_srt
from .maintenance import cache_snapshot, clear_generated_cache
from .reference_traits import analyze_reference, GENERATOR_ID as REFERENCE_GENERATOR_ID, GENERATOR_VERSION as REFERENCE_GENERATOR_VERSION, validate_traits

_INTEGRITY_DIGEST_CACHE = IntegrityDigestCache()


def _waveform_preview(bundle: Path, manifest: dict[str, Any], *, source_origin_seconds: Fraction = Fraction(0), max_bins: int = 1200) -> dict[str, Any] | None:
    try:
        relative = manifest.get("waveform")
        if not isinstance(relative, str) or not relative:
            return None
        path = (bundle / relative).resolve()
        path.relative_to(bundle.resolve())
        if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        audio = payload["audio"]
        peaks = payload["peaks"]
        sample_rate = int(audio["sampleRate"])
        sample_count = int(audio["samples"])
        if not isinstance(peaks, list) or not peaks or sample_rate <= 0 or sample_count <= 0:
            return None
        stride = max(1, math.ceil(len(peaks) / max_bins))
        compact = []
        for start in range(0, len(peaks), stride):
            group = peaks[start:start + stride]
            if not group or any(not isinstance(pair, list) or len(pair) != 2 for pair in group):
                return None
            values = [float(value) for pair in group for value in pair]
            if any(not math.isfinite(value) or value < -1 or value > 1 for value in values):
                return None
            compact.append([min(values[::2]), max(values[1::2])])
        start_ticks = round(float(Fraction(str(audio["sourceStartSeconds"])) - source_origin_seconds) * 1_000_000)
        duration_ticks = round(sample_count * 1_000_000 / sample_rate)
        return {"sourceStartTicks": start_ticks, "durationTicks": duration_ticks,
                "sampleCount": sample_count, "originalBinCount": len(peaks), "peaks": compact}
    except (OSError, KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError, json.JSONDecodeError):
        return None

def _analysis_artifact_matches_row(data: dict[str, Any], row: dict[str, Any]) -> bool:
    if (
        data.get("assetId") != row.get("assetId")
        or data.get("detectorId") != row.get("detectorId")
        or data.get("detectorVersion") != row.get("detectorVersion")
        or data.get("sourceFingerprint") != row.get("sourceFingerprint")
        or type(data.get("sourceDurationTicks")) is not int
        or data["sourceDurationTicks"] <= 0
    ):
        return False
    results = data.get("results")
    if not isinstance(results, list):
        return False
    matches = [
        result for result in results
        if isinstance(result, dict)
        and result.get("assetId") == row.get("assetId")
        and result.get("detectorId") == row.get("detectorId")
        and result.get("detectorVersion") == row.get("detectorVersion")
        and result.get("sourceInTicks") == row.get("sourceInTicks")
        and result.get("sourceOutTicks") == row.get("sourceOutTicks")
    ]
    return len(matches) == 1 and matches[0] == row.get("result")


def handlers(config):
    return {"proxy": ProxyHandler(config.ffmpeg_path, config.ffprobe_path), "transcription": TranscriptionHandler(), "model_download": ModelDownloadHandler(), "analysis": AnalysisHandler(config.ffmpeg_path), "suggestion": SuggestionHandler(), "rendering": RenderHandler(config.ffmpeg_path, config.ffprobe_path), "indexing": IndexHandler(configured_runtime(config)), "planning": PlanningHandler(configured_runtime(config))}


def _annotate_source_availability(store: ProjectStore, state: dict[str, Any]) -> None:
    """Add a cheap UI staleness hint without hashing source media on project open."""
    for asset in state.get("assets", []):
        try:
            if asset.get("path_mode") == "project-relative":
                path = (store.root / str(asset["original_uri"])).resolve()
                path.relative_to(store.root.resolve())
            else:
                path = Path(str(asset["last_known_path"]))
            stat = path.stat()
            if not path.is_file():
                asset["sourceStatus"] = "unavailable"
            elif stat.st_size != int(asset["size_bytes"]) or stat.st_mtime_ns != int(asset["mtime_ns"]):
                asset["sourceStatus"] = "changed_since_import"
            else:
                asset["sourceStatus"] = "available"
        except (OSError, KeyError, TypeError, ValueError):
            asset["sourceStatus"] = "unavailable"


def dispatch(method: str, params: dict[str, Any], config: AppConfig, jobs: JobQueue | None = None, preview: PreviewServer | None = None) -> dict[str, Any]:
    if (method.startswith("job_") or method in {"proxy_generate", "transcription_generate", "model_download", "analysis_run", "proposal_generate", "planning_generate", "export_generate", "index_build"}) and jobs is None:
        owned = JobQueue(config.data_dir, concurrency=config.job_concurrency, handlers=handlers(config), autostart=False)
        try:
            return dispatch(method, params, config, owned, preview)
        finally:
            owned.close()
    if method == "runtime_status":
        return runtime_status(config)
    if method == "runtime_summarize":
        return summarize_asset(ProjectStore.open(Path(params["path"])), str(params["assetId"]), configured_runtime(config))
    if method == "index_build":
        store = ProjectStore.open(Path(params["path"]))
        semantic, force = params.get("semantic", False), params.get("force", False)
        if type(semantic) is not bool or type(force) is not bool:
            raise ValueError("Index semantic and rebuild choices must be booleans.")
        return jobs.submit(indexing_job(store, configured_runtime(config) if semantic else None, semantic=semantic, force=force))
    if method == "index_status":
        return index_status(ProjectStore.open(Path(params["path"])))
    if method == "index_search":
        return search_index(ProjectStore.open(Path(params["path"])), params.get("query", ""), mode=params.get("mode", "exact"), runtime=configured_runtime(config) if params.get("mode") == "semantic" else None, filters=params.get("filters"), limit=params.get("limit", 30))
    if method == "models_status":
        store = ProjectStore.open(Path(params["path"]))
        from .capabilities import _memory_bytes
        total, available, _ = _memory_bytes()
        profile = {"memory": {"totalBytes": total, "availableBytes": available}}
        backend = jobs.handlers["transcription"].backend if jobs else TranscriptionHandler().backend
        return {"models": [model_status(store, key) for key in CATALOG], "recommended": choose_model(profile), "backendAvailable": bool(backend.executable), "backend": backend.name, "detail": "Set LOCAL_CUT_WHISPER_CLI to a whisper.cpp whisper-cli executable. CPU inference is used conservatively."}
    if method == "model_download":
        store = ProjectStore.open(Path(params["path"]))
        model = str(params["model"])
        if model not in CATALOG: raise ValueError("Unknown model")
        if model_status(store, model)["ready"]: return {"state": "completed", "deduplicated": True}
        return jobs.submit(JobInput(str(store.root), "model_download", {"model": model}, CATALOG[model][1], model_id=model, model_version=REVISION, generator_id="huggingface-catalog", generator_version=REVISION))
    if method == "transcription_generate":
        store = ProjectStore.open(Path(params["path"]))
        return jobs.submit(transcription_job(store, str(params["assetId"]), str(params["model"]), jobs.handlers["transcription"].backend))
    if method == "transcript_list":
        store = ProjectStore.open(Path(params["path"]))
        result = {"transcripts": store.list_transcripts(params.get("assetId"))}
        try:
            bundle = ready_proxy(store, str(params["assetId"]), force=False)
            manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
            mapping_path = (bundle / str(manifest["mapping"])).resolve()
            mapping_path.relative_to(bundle.resolve())
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
            source_origin = Fraction(mapping.get("sourceStartSeconds", "0"))
            result["sourceMinusProxySeconds"] = float(Fraction(mapping["sourceMinusProxySeconds"]) - source_origin)
            result["waveform"] = _waveform_preview(bundle, manifest, source_origin_seconds=source_origin)
            if preview: result["playbackUrl"] = preview.register(bundle / "proxy.mp4")
        except ValueError:
            pass
        return result
    if method == "transcript_edit":
        return ProjectStore.open(Path(params["path"])).update_transcript_segment(str(params["transcriptId"]), str(params["segmentId"]), params.get("text"))
    if method == "transcript_marker":
        store = ProjectStore.open(Path(params["path"]))
        if params.get("delete"):
            return store.delete_transcript_range_marker(str(params["transcriptId"]), str(params["markerId"]))
        return store.set_transcript_range_marker(str(params["transcriptId"]), params["sourceInTicks"], params["sourceOutTicks"], str(params["marker"]), params.get("markerId"))
    if method == "analysis_catalog":
        return {"detectors": analysis_catalog()}
    if method == "analysis_run":
        store = ProjectStore.open(Path(params["path"]))
        detector = str(params["detector"])
        if detector not in DETECTORS: raise ValueError("Unknown analysis detector.")
        return jobs.submit(analysis_job(store, str(params["assetId"]), detector, params.get("settings", {})))
    if method == "analysis_list":
        store = ProjectStore.open(Path(params["path"]))
        force_integrity = params.get("verifyIntegrity") is True
        request_digests: dict[str, str] = {}

        def verified_digest(path: Path) -> str:
            key = str(path.resolve())
            if key not in request_digests:
                request_digests[key], _cache_hit = _INTEGRITY_DIGEST_CACHE.digest(path, force=force_integrity)
            return request_digests[key]

        results = store.list_analyses(params.get("assetId"), params.get("detectorId"))
        warnings = []
        with _connect(store.database_path) as db:
            artifact_rows = db.execute(
                "SELECT id,kind,relative_path,checksum_sha256 FROM artifacts WHERE project_id=? AND status='ready' AND kind LIKE 'analysis:%' ORDER BY created_at",
                (store.project_id,),
            ).fetchall()
        for artifact in artifact_rows:
            detector_id = artifact["kind"].removeprefix("analysis:")
            if params.get("detectorId") and detector_id != params["detectorId"]:
                continue
            try:
                artifact_path = (store.root / artifact["relative_path"]).resolve()
                artifact_path.relative_to(store.root.resolve())
                if verified_digest(artifact_path) != artifact["checksum_sha256"]:
                    continue
                data = json.loads(artifact_path.read_text(encoding="utf-8"))
                if params.get("assetId") and data.get("assetId") != params["assetId"]:
                    continue
                warnings.extend({"detectorId": detector_id, "artifactId": artifact["id"], **warning} for warning in data.get("warnings", []) if isinstance(warning, dict) and isinstance(warning.get("message"), str))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        source_status = "unknown"
        prerequisites = {"proxyReady": False, "hasAudio": False, "transcriptReady": False}
        asset_id = params.get("assetId")
        verified_artifacts: dict[str, str] = {}
        verified_analysis_payloads: dict[str, dict[str, Any]] = {}
        proxy_integrity_ok = False
        transcript_integrity_ok = False
        if asset_id:
            with _connect(store.database_path) as db:
                asset = db.execute("SELECT * FROM assets WHERE id=? AND project_id=?", (asset_id, store.project_id)).fetchone()
                proxy = db.execute("SELECT * FROM artifacts WHERE asset_id=? AND project_id=? AND kind='proxy' AND status='ready' AND source_fingerprint=? ORDER BY created_at DESC LIMIT 1", (asset_id, store.project_id, asset["sha256"])).fetchone() if asset else None
                transcript = db.execute("SELECT t.id,t.artifact_id,a.checksum_sha256,a.relative_path FROM transcripts t JOIN artifacts a ON a.id=t.artifact_id WHERE t.asset_id=? AND t.project_id=? AND t.source_fingerprint=? AND t.status='ready' AND a.status='ready' ORDER BY t.created_at DESC LIMIT 1", (asset_id, store.project_id, asset["sha256"])).fetchone() if asset else None
            if transcript:
                try:
                    transcript_path = (store.root / transcript["relative_path"]).resolve()
                    transcript_path.relative_to(store.root.resolve())
                    transcript_integrity_ok = transcript_path.is_file() and verified_digest(transcript_path) == transcript["checksum_sha256"]
                except (OSError, ValueError):
                    transcript_integrity_ok = False
            if asset is None:
                source_status = "missing_asset"
            else:
                try:
                    media_path = source_path(store, asset)
                    stat = media_path.stat()
                    source_status = "metadata_matches_import" if stat.st_size == asset["size_bytes"] and stat.st_mtime_ns == asset["mtime_ns"] else "changed_since_import"
                except OSError:
                    source_status = "unavailable"
                if source_status == "metadata_matches_import" and proxy is not None:
                    try:
                        bundle=(store.root/proxy["relative_path"]).resolve(); bundle.relative_to(store.root.resolve())
                        if bundle.is_dir():
                            proxy_integrity_ok = verified_digest(bundle) == proxy["checksum_sha256"]
                            manifest=json.loads((bundle/"manifest.json").read_text(encoding="utf-8"))
                            proxy_path=(bundle/str(manifest["proxy"])).resolve(); proxy_path.relative_to(bundle.resolve())
                            prerequisites["proxyReady"]=proxy_integrity_ok and manifest.get("pipelineVersion") == PROXY_PIPELINE_VERSION and proxy_path.is_file() and proxy_path.stat().st_size>0
                            analysis_audio=manifest.get("analysis")
                            if isinstance(analysis_audio,dict) and analysis_audio.get("path"):
                                audio_path=(bundle/str(analysis_audio["path"])).resolve(); audio_path.relative_to(bundle.resolve())
                                prerequisites["hasAudio"]=audio_path.is_file() and audio_path.stat().st_size>44
                    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                        prerequisites["proxyReady"]=False
                prerequisites["transcriptReady"]=transcript is not None and transcript_integrity_ok
            for row in results:
                row["sourceStatus"] = source_status
                artifact_config = row.pop("_artifactConfig", {})
                relative = row.pop("_artifactPath", None)
                checksum = row.pop("_artifactChecksum", None)
                artifact_status = row.pop("_artifactStatus", None)
                row["validity"] = "unknown"
                row["validityReason"] = "This saved result predates input-lineage tracking or is missing artifact metadata."
                if artifact_status != "ready" or not relative or not checksum:
                    row["validity"] = "invalid"
                    row["validityReason"] = "The result artifact is missing or is no longer registered as ready. Rerun this detector."
                    continue
                try:
                    artifact_path = (store.root / relative).resolve()
                    artifact_path.relative_to(store.root.resolve())
                    digest = verified_artifacts.get(row["artifactId"])
                    if digest is None:
                        digest = verified_digest(artifact_path)
                        verified_artifacts[row["artifactId"]] = digest
                    if digest != checksum:
                        raise ValueError("checksum mismatch")
                    payload = verified_analysis_payloads.get(row["artifactId"])
                    if payload is None:
                        loaded = json.loads(artifact_path.read_text(encoding="utf-8"))
                        if not isinstance(loaded, dict):
                            raise ValueError("invalid artifact payload")
                        payload = loaded
                        verified_analysis_payloads[row["artifactId"]] = payload
                    if not _analysis_artifact_matches_row(payload, row):
                        row["validity"] = "invalid"
                        row["validityReason"] = "The indexed result does not match its validated artifact. Rerun this detector."
                        continue
                except (OSError, ValueError):
                    row["validity"] = "invalid"
                    row["validityReason"] = "The result artifact failed its integrity check. Rerun this detector."
                    continue
                expected_proxy = artifact_config.get("proxyArtifactId")
                expected_proxy_checksum = artifact_config.get("proxyChecksum")
                if source_status in {"changed_since_import", "unavailable", "missing_asset"} or not asset or row["sourceFingerprint"] != asset["sha256"]:
                    row["validity"] = "stale"
                    row["validityReason"] = "The imported source is unavailable or has changed since this result was created."
                elif not expected_proxy or not expected_proxy_checksum:
                    row["validityReason"] = "The result artifact is intact, but its proxy lineage is unavailable."
                elif not proxy_integrity_ok:
                    row["validity"] = "stale"
                    row["validityReason"] = "The current proxy artifact failed its integrity check. Regenerate preview assets before relying on this result."
                elif not proxy or expected_proxy != proxy["id"] or expected_proxy_checksum != proxy["checksum_sha256"]:
                    row["validity"] = "stale"
                    row["validityReason"] = "The validated proxy used by this result is no longer the current proxy."
                elif artifact_config.get("transcriptArtifactId") and (not transcript or not transcript_integrity_ok or artifact_config.get("transcriptArtifactId") != transcript["artifact_id"] or artifact_config.get("transcriptChecksum") != transcript["checksum_sha256"]):
                    row["validity"] = "stale"
                    row["validityReason"] = "The transcript used by this result is no longer the current transcript."
                elif artifact_config.get("transcriptId") and artifact_config.get("transcriptReviewFingerprint") != _transcript_review_fingerprint(store.get_transcript(str(artifact_config["transcriptId"]))):
                    row["validity"] = "stale"
                    row["validityReason"] = "The reviewed transcript text or range markers changed after this result was created."
                elif row.get("detectorId") in DETECTORS and row.get("detectorVersion") != DETECTORS[row["detectorId"]]["version"]:
                    row["validity"] = "stale"
                    row["validityReason"] = f"This result was created by detector version {row.get('detectorVersion')}; current version is {DETECTORS[row['detectorId']]['version']}. Rerun the detector to review current results."
                else:
                    row["validity"] = "integrity_ok"
                    row["validityReason"] = "The result artifact checksum and recorded input lineage match the current imported project records. Source freshness on this screen uses file size and modification time; jobs hash source media before and after processing."
        if preview:
            for row in results:
                relative = row["result"].get("thumbnailPath")
                if not relative: continue
                path = (store.root / relative).resolve()
                try:
                    path.relative_to(store.root.resolve())
                    if path.is_file(): row["thumbnailUrl"] = preview.register(path)
                except ValueError:
                    continue
        prerequisites["detail"] = "Ready" if prerequisites["proxyReady"] else "Generate valid preview assets for this source in Projects."
        return {"results": results, "warnings": warnings, "sourceStatus": source_status, "prerequisites": prerequisites}
    if method == "brief_parse":
        return {"constraints": DeterministicBriefParser().parse(params.get("briefText", ""), params.get("overrides")), "parserId": DeterministicBriefParser.name, "parserVersion": DeterministicBriefParser.version}
    if method == "style_recipe_catalog":
        return {"recipes": recipe_catalog()}
    if method == "style_recipe_propose":
        return propose_recipe(params.get("recipeId"), params.get("inspiration", ""))
    if method == "brief_save":
        store = ProjectStore.open(Path(params["path"]))
        text = params.get("briefText", "")
        constraints = DeterministicBriefParser().parse(text, params.get("constraints"))
        origin = params.get("traitOrigin", "user")
        traits = validate_traits(params.get("traits", {}))
        generator_id, generator_version = "user", "1"
        if "styleTraits" in constraints:
            if traits and traits != constraints["styleTraits"]:
                raise ValueError("The style profile differs from the brief. Review one consistent set of neutral traits.")
            traits = constraints["styleTraits"]
            if constraints["recipeId"] != "custom":
                generator_id, generator_version = RECIPE_GENERATOR_ID, RECIPE_GENERATOR_VERSION
        if traits:
            if params.get("styleReviewed") is not True:
                raise ValueError("Review and edit the proposed neutral style traits before saving.")
            constraints["styleTraits"] = traits
            constraints["styleReviewed"] = True
        if origin == "reference-derived":
            generator_id, generator_version = REFERENCE_GENERATOR_ID, REFERENCE_GENERATOR_VERSION
        elif origin != "user":
            raise ValueError("Unsupported trait origin.")
        brief_id = store.add_brief(text, constraints=constraints, traits=traits, trait_origin=origin, generator_id=generator_id, generator_version=generator_version)
        return {"brief": next(item for item in store.list_briefs() if item["id"] == brief_id)}
    if method == "reference_analyze":
        return analyze_reference(str(params.get("referencePath", "")), ffmpeg=config.ffmpeg_path, ffprobe=config.ffprobe_path)
    if method == "brief_list":
        return {"briefs": ProjectStore.open(Path(params["path"])).list_briefs()}
    if method == "planning_generate":
        store = ProjectStore.open(Path(params["path"]))
        return jobs.submit(planning_job(store, str(params["briefId"]), str(params["assetId"]), configured_runtime(config), params.get("query", ""), params.get("searchMode", "exact")))
    if method == "planning_list":
        return {"plans": list_written_plans(ProjectStore.open(Path(params["path"])), params.get("briefId"), runtime=configured_runtime(config))}
    if method == "planning_review":
        store = ProjectStore.open(Path(params["path"]))
        return {"plan": review_plan(store, params["planId"], params["action"], section_id=params.get("sectionId"), candidate_id=params.get("candidateId"), order=params.get("order"), expected_review_version=params.get("expectedReviewVersion"), runtime=configured_runtime(config))}
    if method == "proposal_generate":
        store = ProjectStore.open(Path(params["path"]))
        return jobs.submit(proposal_job(store, str(params["assetId"]), str(params["briefId"]), str(params["transcriptId"])))
    if method == "proposal_list":
        return {"plans": [record for record in ProjectStore.open(Path(params["path"])).list_edit_plans(params.get("briefId")) if record["plan"].get("planType") not in {"written-rough-cut", "timeline-revision"}]}
    if method == "proposal_decide":
        store = ProjectStore.open(Path(params["path"]))
        record = next((record for record in store.list_edit_plans() if record["id"] == str(params["planId"])), None)
        if record and record["plan"].get("planType") in {"written-rough-cut", "timeline-revision"}:
            raise ValueError("Review written plans through planning_review; they cannot execute timeline operations.")
        return store.decide_edit_plan_suggestion(str(params["planId"]), str(params["suggestionId"]), str(params["decision"]), expected_revision=params.get("expectedRevision"), create_captions=params.get("createCaptions", False))
    if method == "export_generate":
        store = ProjectStore.open(Path(params["path"]))
        return jobs.submit(render_job(store, config.ffmpeg_path, config.ffprobe_path, burn_captions=bool(params.get("burnCaptions", False))))
    if method == "export_list":
        return {"exports": list_exports(ProjectStore.open(Path(params["path"]))) }
    if method == "export_srt":
        return export_srt(ProjectStore.open(Path(params["path"])))
    if method == "proxy_generate":
        store = ProjectStore.open(Path(params["path"]))
        profile = collect_capabilities(store.root, ffmpeg_path=config.ffmpeg_path, ffprobe_path=config.ffprobe_path)
        if not profile["mediaTools"]["ffmpeg"]["available"] or not profile["mediaTools"]["ffprobe"]["available"]:
            raise ValueError("FFmpeg and FFprobe are required to generate preview assets.")
        return jobs.submit(proxy_job(store, str(params["assetId"]), profile))
    if method == "ping":
        return {"service": "localcut", "version": __version__, "protocolVersion": PROTOCOL_VERSION}
    if method == "health":
        return {
            "service": {"available": True, "detail": "Python service connected.", "version": __version__},
            "ffmpeg": check_ffmpeg(config),
            "storage": check_storage(config.data_dir),
        }
    if method == "capabilities":
        return collect_capabilities(config.data_dir, ffmpeg_path=config.ffmpeg_path, ffprobe_path=config.ffprobe_path)
    if method == "project_create":
        store = ProjectStore.create(Path(params["path"]), str(params["name"]), params.get("settings", {}))
        ProjectCatalog(config.data_dir).remember(store.root)
        return {"projectId": store.project_id, "path": str(store.root), "name": params["name"]}
    if method in {"project_open", "project_open_last"}:
        store = ProjectStore.open(Path(params["path"])) if method == "project_open" else ProjectCatalog(config.data_dir).reopen_last()
        if store is None:
            raise ProjectError("No valid recent project is available.")
        state = store.load_state()
        _annotate_source_availability(store, state)
        ProjectCatalog(config.data_dir).remember(store.root)
        return {"projectId": store.project_id, "path": str(store.root), "state": state, "recovery": store.recovery_report}
    if method == "project_save":
        store = ProjectStore.open(Path(params["path"]))
        revision = store.autosave(store.project_id, params.get("state", {}))
        ProjectCatalog(config.data_dir).remember(store.root)
        return {"projectId": store.project_id, "revision": revision}
    if method in {"revision_list", "revision_generate", "revision_propose", "revision_decide"}:
        required = {"path"}
        optional = set()
        if method == "revision_propose":
            required.add("text"); optional.update({"selectedSuggestionId", "selectedClipId"})
        elif method == "revision_decide": required.update({"suggestionId", "decision", "expectedRevision"})
        if not required <= set(params) or set(params) - required - optional:
            raise ValueError("Provide exactly the supported revision request fields.")
        if not isinstance(params["path"], str) or not params["path"]: raise ValueError("Provide the project path.")
        for key in ("selectedSuggestionId", "selectedClipId", "suggestionId"):
            if key in params and (not isinstance(params[key], str) or not params[key] or len(params[key]) > 200): raise ValueError("Select a valid project suggestion or clip ID.")
        if method == "revision_decide" and (params["decision"] not in ("accepted", "rejected", "undone") or type(params["expectedRevision"]) is not int or params["expectedRevision"] < 0):
            raise ValueError("Provide a supported decision and nonnegative reviewed project revision.")
        store = ProjectStore.open(Path(params["path"]))
        if method == "revision_list": return revision_list(store)
        if method == "revision_generate": return revision_generate(store)
        if method == "revision_propose": return revision_propose(store, params["text"], selected_suggestion_id=params.get("selectedSuggestionId"), selected_clip_id=params.get("selectedClipId"))
        return revision_decide(store, params["suggestionId"], params["decision"], params["expectedRevision"])
    if method in {"timeline_get", "timeline_command", "timeline_undo", "timeline_redo", "timeline_compile"}:
        required = {"path"} if method == "timeline_get" else {"path", "expectedRevision"}
        if method == "timeline_command": required.add("command")
        if method == "timeline_compile": required.update({"planId", "sectionIds", "expectedReviewVersion"})
        if set(params) != required:
            raise ValueError("Provide exactly the required timeline request fields.")
        if not isinstance(params["path"], str) or not params["path"]:
            raise ValueError("Provide the project path.")
        if method != "timeline_get" and (type(params["expectedRevision"]) is not int or params["expectedRevision"] < 0):
            raise ValueError("A timeline command requires the nonnegative project revision that was reviewed.")
        store = ProjectStore.open(Path(params["path"]))
        if method == "timeline_get": result = timeline_state(store)
        elif method == "timeline_command": result = timeline_command(store, params["command"], params["expectedRevision"])
        elif method == "timeline_undo": result = timeline_undo(store, params["expectedRevision"])
        elif method == "timeline_redo": result = timeline_redo(store, params["expectedRevision"])
        else:
            result = compile_plan(store, params["planId"], params["sectionIds"], params["expectedReviewVersion"], params["expectedRevision"], runtime=configured_runtime(config))
        return {"timeline": result}
    if method == "timeline_history":
        return ProjectStore.open(Path(params["path"])).timeline_history()
    if method == "project_cache_info":
        return cache_snapshot(ProjectStore.open(Path(params["path"])))
    if method == "project_cache_clear":
        store = ProjectStore.open(Path(params["path"]))
        return clear_generated_cache(store, str(params.get("confirmationToken", "")))
    if method == "media_import":
        store = ProjectStore.open(Path(params["path"]))
        probe = FFprobe(config.ffprobe_path)
        return import_media(store, params.get("inputs", []), probe=probe, storage_mode=str(params.get("storageMode", "linked")))
    if method == "media_relink":
        store = ProjectStore.open(Path(params["path"]))
        probe = FFprobe(config.ffprobe_path)
        return relink_media(store, str(params["assetId"]), str(params["candidatePath"]), probe=probe, allow_changed=bool(params.get("allowChanged", False)))
    if method.startswith("job_"):
        queue = jobs or JobQueue(config.data_dir, concurrency=config.job_concurrency, autostart=False)
        if method == "job_submit":
            raw = params.get("job")
            if not isinstance(raw, dict):
                raise ValueError("job must be an object")
            required = {"projectPath", "kind", "payload", "sourceFingerprint"}
            if not required.issubset(raw) or not isinstance(raw["payload"], dict):
                raise ValueError("job requires projectPath, kind, object payload, and sourceFingerprint")
            job = JobInput(project_path=str(raw["projectPath"]), kind=raw["kind"], payload=raw["payload"], source_fingerprint=str(raw["sourceFingerprint"]), pipeline_version=str(raw.get("pipelineVersion", "1")), asset_id=raw.get("assetId"), model_id=raw.get("modelId"), model_version=raw.get("modelVersion"), generator_id=str(raw.get("generatorId", "localcut")), generator_version=str(raw.get("generatorVersion", __version__)), config=raw.get("config", {}), dependencies=tuple(raw.get("dependencies", [])), max_attempts=int(raw.get("maxAttempts", 2)))
            return queue.submit(job)
        if method == "job_list":
            return {"jobs": queue.list_jobs(params.get("projectId"), int(params.get("limit", 100)))}
        if method == "job_cancel":
            return queue.cancel(str(params["jobId"]))
        if method == "job_retry":
            return queue.retry(str(params["jobId"]))
        raise KeyError(method)
    raise KeyError("unknown_method")


def serve(input_stream: Any = sys.stdin, output_stream: Any = sys.stdout, config: AppConfig | None = None) -> int:
    settings = config or load_config()
    logger = configure_logging(settings.data_dir / "logs", settings.log_level)
    jobs = JobQueue(settings.data_dir, concurrency=settings.job_concurrency, handlers=handlers(settings))
    preview = PreviewServer()
    logger.info("service_started", extra={"component": "service", "event": "service_started"})
    for line in input_stream:
        # Use a valid correlation ID even when a malformed request does not
        # provide one, so error envelopes still satisfy the protocol schema.
        request_id = 1
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("invalid_request")
            candidate_request_id = request.get("requestId")
            if not isinstance(candidate_request_id, int) or isinstance(candidate_request_id, bool) or candidate_request_id < 1:
                raise ValueError("invalid_request_id")
            request_id = candidate_request_id
            if request.get("protocolVersion") != PROTOCOL_VERSION:
                raise ValueError("protocol_version_mismatch")
            if not isinstance(request.get("method"), str) or not isinstance(request.get("params", {}), dict):
                raise ValueError("invalid_request")
            result = dispatch(request["method"], request.get("params", {}), settings, jobs, preview)
            response = {"protocolVersion": PROTOCOL_VERSION, "requestId": request_id, "ok": True, "result": result}
            logger.info("request_succeeded", extra={"component": "service", "event": request["method"], "request_id": request_id})
        except UnsupportedSchemaError as exc:
            response = {"protocolVersion": PROTOCOL_VERSION, "requestId": request_id, "ok": False, "error": {"code": "unsupported_schema", "message": str(exc)}}
        except ProjectError as exc:
            response = {"protocolVersion": PROTOCOL_VERSION, "requestId": request_id, "ok": False, "error": {"code": "project_error", "message": str(exc)}}
        except KeyError as exc:
            response = {"protocolVersion": PROTOCOL_VERSION, "requestId": request_id, "ok": False, "error": {"code": "unknown_method", "message": str(exc.args[0])}}
        except RuntimeFailure as exc:
            response = {"protocolVersion": PROTOCOL_VERSION, "requestId": request_id, "ok": False, "error": {"code": exc.code, "message": str(exc)}}
        except (ValueError, json.JSONDecodeError) as exc:
            response = {"protocolVersion": PROTOCOL_VERSION, "requestId": request_id, "ok": False, "error": {"code": "invalid_request", "message": str(exc)}}
        except Exception as exc:  # Keep malformed requests from killing the long-lived service.
            response = {"protocolVersion": PROTOCOL_VERSION, "requestId": request_id, "ok": False, "error": {"code": "internal_error", "message": exc.__class__.__name__}}
            logger.exception("request_failed", extra={"component": "service", "event": "request_failed", "request_id": request_id})
        output_stream.write(json.dumps(response, separators=(",", ":")) + "\n")
        output_stream.flush()
    logger.info("service_stopped", extra={"component": "service", "event": "service_stopped"})
    jobs.close()
    preview.close()
    return 0
