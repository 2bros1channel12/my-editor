"""Offline, neutral editing recipes; inspiration names never determine traits."""
from __future__ import annotations

from copy import deepcopy

RECIPE_VERSION = "1"
GENERATOR_ID = "localcut-neutral-style-recipe"
GENERATOR_VERSION = "1"


def _traits(pacing, shot_length, cuts, captions, punch_ins, music, humor, structure):
    return {
        "pacing": pacing,
        "averageShotLengthSeconds": shot_length,
        "jumpCutFrequencyPerMinute": cuts,
        "captionDensityPerMinute": captions,
        "punchInFrequencyPerMinute": punch_ins,
        "musicEnergy": music,
        "humorLevel": humor,
        "narrativeStructure": structure,
    }


_RECIPES = [
    {
        "id": "fast-educational", "name": "Fast educational",
        "description": "A clear opening, brisk explanations, frequent captions, and a short recap.",
        "version": RECIPE_VERSION,
        "traits": _traits("fast", 7.5, 8, 12, 2, "moderate", "low", "hook-explanation-summary"),
    },
    {
        "id": "calm-documentary", "name": "Calm documentary",
        "description": "Longer passages, restrained emphasis, and a chronological explanation.",
        "version": RECIPE_VERSION,
        "traits": _traits("slow", 30, 2, 3, 0, "low", "low", "chronological"),
    },
    {
        "id": "conversational-commentary", "name": "Conversational commentary",
        "description": "A conversational question-and-answer structure with space for humor.",
        "version": RECIPE_VERSION,
        "traits": _traits("moderate", 15, 4, 6, 1, "low", "moderate", "question-answer"),
    },
    {
        "id": "concise-product-review", "name": "Concise product review",
        "description": "A focused problem-and-solution structure with concise demonstrations.",
        "version": RECIPE_VERSION,
        "traits": _traits("moderate", 10, 6, 6, 1, "low", "low", "problem-solution"),
    },
]


def recipe_catalog() -> list[dict]:
    """Return independent values so callers cannot mutate recipe defaults."""
    return deepcopy(_RECIPES)


def propose_recipe(recipe_id: str, inspiration: str = "") -> dict:
    """Produce a reviewable recipe without retaining or interpreting a name."""
    if not isinstance(recipe_id, str) or recipe_id not in {recipe["id"] for recipe in _RECIPES}:
        raise ValueError("Choose a supported style recipe.")
    if not isinstance(inspiration, str) or len(inspiration) > 200:
        raise ValueError("Inspiration must be text no longer than 200 characters.")
    recipe = next(recipe for recipe in _RECIPES if recipe["id"] == recipe_id)
    return {
        "recipeId": recipe_id,
        "recipeVersion": RECIPE_VERSION,
        "traits": deepcopy(recipe["traits"]),
        "requiresReview": True,
        "explanation": (
            f"These are editable starting suggestions from the {recipe['name']} recipe you chose. "
            "A creator name alone cannot establish editing traits; no claims about a creator's style are inferred. "
            "Review and edit these neutral traits before saving. They do not request identity, voice, likeness, "
            "branding, signature graphics or phrases, or copyrighted music copying. "
            "Music energy describes a preference only; it does not acquire music."
        ),
        "generatorId": GENERATOR_ID,
        "generatorVersion": GENERATOR_VERSION,
    }
