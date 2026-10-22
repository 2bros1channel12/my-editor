"""Deterministic, offline brief parsing. A replaceable interface boundary, no network calls."""
from __future__ import annotations

from typing import Any, Protocol
import re
from .reference_traits import validate_traits
from .export_profiles import parse_output_request, validate_profile

PARSER_ID = "localcut-labeled-brief-parser"
PARSER_VERSION = "3"
PLATFORMS = {"unspecified", "youtube", "youtube-shorts", "tiktok", "instagram-reels", "course", "other"}
RECIPE_IDS = {"custom", "fast-educational", "calm-documentary", "conversational-commentary", "concise-product-review"}
FIELDS = {"audience", "objective", "tone", "targetDurationMinutes", "aspectRatio", "mustKeep", "mustRemove", "preferredOrder", "schemaVersion", "platform", "desiredOpening", "desiredEnding", "recipeId", "recipeVersion", "styleTraits", "styleReviewed", "outputProfiles", "subjectMode", "layout"}
EMPTY_TRAITS = {"pacing": "unknown", "averageShotLengthSeconds": None, "jumpCutFrequencyPerMinute": None, "captionDensityPerMinute": None, "punchInFrequencyPerMinute": None, "musicEnergy": "unknown", "humorLevel": "unknown", "narrativeStructure": "unknown"}


class BriefParser(Protocol):
    name: str
    version: str
    def parse(self, text: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]: ...


class DeterministicBriefParser:
    name = PARSER_ID
    version = PARSER_VERSION
    labels = {
        "audience": "audience", "objective": "objective", "goal": "objective", "tone": "tone",
        "target duration": "targetDurationMinutes", "duration": "targetDurationMinutes",
        "aspect ratio": "aspectRatio", "must keep": "mustKeep", "keep": "mustKeep",
        "must remove": "mustRemove", "remove": "mustRemove",
        "preferred order": "preferredOrder", "order": "preferredOrder", "passage order": "preferredOrder",
        "video purpose": "objective", "purpose": "objective", "intended audience": "audience",
        "platform": "platform", "opening": "desiredOpening", "desired opening": "desiredOpening",
        "ending": "desiredEnding", "desired ending": "desiredEnding",
        "required topics": "mustKeep", "required moments": "mustKeep", "material to avoid": "mustRemove",
        "pacing": "style:pacing", "caption density": "style:captionDensityPerMinute",
        "cut frequency": "style:jumpCutFrequencyPerMinute", "zoom frequency": "style:punchInFrequencyPerMinute",
        "punch-in frequency": "style:punchInFrequencyPerMinute", "music energy": "style:musicEnergy",
        "humor level": "style:humorLevel", "narrative structure": "style:narrativeStructure",
    }

    def parse(self, text: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(text, str) or len(text) > 20_000:
            raise ValueError("Creative brief must be text no longer than 20,000 characters.")
        values: dict[str, Any] = {"audience": "", "objective": "", "tone": "", "targetDurationMinutes": None, "aspectRatio": "source", "mustKeep": [], "mustRemove": [], "preferredOrder": [], "schemaVersion": 2, "platform": "unspecified", "desiredOpening": "", "desiredEnding": "", "recipeId": "custom", "recipeVersion": "1"}
        for line in text.splitlines():
            match = re.match(r"^\s*([A-Za-z -]{2,30})\s*:\s*(.*?)\s*$", line)
            key = self.labels.get(match.group(1).casefold().strip()) if match else None
            if key and match:
                if key.startswith("style:"):
                    trait_key = key.partition(":")[2]
                    trait_value: Any = match.group(2).casefold()
                    if trait_key.endswith("PerMinute"):
                        try: trait_value = float(trait_value) if trait_value else None
                        except ValueError as exc: raise ValueError("Style frequencies must be numbers per minute.") from exc
                    values["styleTraits"] = self._coerce("styleTraits", {**values.get("styleTraits", EMPTY_TRAITS), trait_key: trait_value})
                else:
                    values[key] = self._coerce(key, match.group(2))
        # Unlabeled prose remains an objective draft for user review.
        if not any(line.partition(":")[1] for line in text.splitlines()) and text.strip():
            values["objective"] = text.strip()
        if re.search(r'\b(vertical|horizontal|square|tiktok|reels|shorts|youtube|both speakers|gameplay above)\b',text,re.I):
            outputs=parse_output_request(text[:16000])
            values.update(outputProfiles=outputs['profiles'],subjectMode=outputs['subjectMode'],layout=outputs['layout'])
            first=outputs['profiles'][0]
            if values['targetDurationMinutes'] is None: values['targetDurationMinutes']=first['targetDurationSeconds']/60
            else:
                for profile in values['outputProfiles']:
                    profile['targetDurationSeconds']=values['targetDurationMinutes']*60
                    profile['maxDurationSeconds']=max(profile['maxDurationSeconds'],profile['targetDurationSeconds'])
            if values['aspectRatio']=='source': values['aspectRatio']={'horizontal':'16:9','vertical':'9:16','square':'1:1'}.get(first['aspect'],'source')
        if overrides is not None:
            if not isinstance(overrides, dict) or set(overrides) - FIELDS:
                raise ValueError("Brief constraints contain unsupported fields.")
            for key, value in overrides.items():
                values[key] = self._coerce(key, value)
        return values

    @staticmethod
    def _coerce(key: str, value: Any) -> Any:
        if key == 'outputProfiles':
            if not isinstance(value,list) or not 1<=len(value)<=10: raise ValueError('Choose between one and ten output profiles.')
            result=[validate_profile(p) for p in value]
            if len({p['id'] for p in result})!=len(result): raise ValueError('Output profile IDs must be unique.')
            return result
        if key in ('subjectMode','layout'):
            if not isinstance(value,str) or value not in ({'primary','keep_all'} if key=='subjectMode' else {'single','stacked'}): raise ValueError('Invalid output framing constraint.')
            return value
        if key == "schemaVersion":
            if type(value) is not int or value != 2: raise ValueError("Unsupported creative brief schema version.")
            return 2
        if key == "styleReviewed":
            if type(value) is not bool: raise ValueError("Style review must be a boolean.")
            return value
        if key == "styleTraits":
            if not isinstance(value, dict): raise ValueError("Style traits must be an object.")
            return validate_traits({**EMPTY_TRAITS, **value})
        if key == "platform":
            if not isinstance(value, str): raise ValueError("Choose a supported platform.")
            value = value.strip().casefold().replace(" ", "-")
            if value not in PLATFORMS: raise ValueError("Choose a supported platform.")
            return value
        if key == "recipeId":
            if not isinstance(value, str) or value not in RECIPE_IDS: raise ValueError("Choose a supported style recipe or custom.")
            return value
        if key == "recipeVersion":
            if value != "1": raise ValueError("Unsupported style recipe version.")
            return "1"
        if key in {"mustKeep", "mustRemove", "preferredOrder"}:
            parts = value if isinstance(value, list) else re.split(r"[,;\n]", str(value))
            cleaned = []
            for part in parts:
                phrase = str(part).strip()
                if phrase:
                    if len(phrase) > 200: raise ValueError(f"{key} phrases must be at most 200 characters.")
                    if phrase.casefold() not in {item.casefold() for item in cleaned}: cleaned.append(phrase)
            if len(cleaned) > 25: raise ValueError(f"{key} can contain at most 25 phrases.")
            return cleaned
        if key == "targetDurationMinutes":
            if isinstance(value, bool): raise ValueError("Target duration must be a number of minutes.")
            if value in (None, "", 0, "0"): return None
            try: number = float(value)
            except (TypeError, ValueError) as exc: raise ValueError("Target duration must be a number of minutes.") from exc
            if not 0.25 <= number <= 180: raise ValueError("Target duration must be between 0.25 and 180 minutes.")
            return number
        if key == "aspectRatio":
            value = str(value or "source").strip()
            if value not in {"source", "16:9", "9:16", "1:1", "4:3"}: raise ValueError("Choose source, 16:9, 9:16, 1:1, or 4:3 aspect ratio.")
            return value
        result = str(value or "").strip()
        if len(result) > 2000: raise ValueError(f"{key} must be at most 2,000 characters.")
        return result
