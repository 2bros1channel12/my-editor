from __future__ import annotations

import json

import pytest

from localcut_service.reference_traits import validate_traits
from localcut_service.style_recipes import propose_recipe, recipe_catalog


def test_catalog_contains_versioned_valid_neutral_recipes():
    catalog = recipe_catalog()
    assert [item["id"] for item in catalog] == [
        "fast-educational", "calm-documentary", "conversational-commentary", "concise-product-review",
    ]
    for item in catalog:
        assert item["version"] == "1"
        assert validate_traits(item["traits"]) == item["traits"]


def test_names_do_not_create_claims_or_survive_in_proposal():
    baseline = propose_recipe("fast-educational")
    for name in ("A private inspiration name", "MrBeast", "Unknown Creator 123"):
        result = propose_recipe("fast-educational", name)
        assert result == baseline
        assert name not in json.dumps(result)
    assert baseline["requiresReview"] is True
    assert "name alone cannot establish" in baseline["explanation"]
    assert baseline["generatorId"] == "localcut-neutral-style-recipe"
    assert baseline["generatorVersion"] == "1"


def test_returned_values_cannot_mutate_registry_or_later_proposals():
    catalog = recipe_catalog()
    catalog[0]["traits"]["pacing"] = "slow"
    catalog.clear()
    first = propose_recipe("fast-educational")
    first["traits"]["pacing"] = "unknown"
    assert recipe_catalog()[0]["traits"]["pacing"] == "fast"
    assert propose_recipe("fast-educational")["traits"]["pacing"] == "fast"


@pytest.mark.parametrize("recipe_id", [None, [], {}, 1, "", "private invalid recipe"])
def test_invalid_recipe_fails_without_echoing_input(recipe_id):
    with pytest.raises(ValueError, match="^Choose a supported style recipe[.]$"):
        propose_recipe(recipe_id)


@pytest.mark.parametrize("inspiration", [None, [], {}, 1, "x" * 201])
def test_invalid_inspiration_fails_without_echoing_input(inspiration):
    with pytest.raises(ValueError, match="^Inspiration must be text no longer than 200 characters[.]$"):
        propose_recipe("calm-documentary", inspiration)


def test_calm_recipe_preserves_zero_punch_ins_and_input_limit():
    result = propose_recipe("calm-documentary", "x" * 200)
    assert result["traits"]["punchInFrequencyPerMinute"] == 0
    assert result["traits"]["averageShotLengthSeconds"] == 30


def test_proposals_require_no_network(monkeypatch):
    import socket

    def reject_network(*args, **kwargs):
        raise AssertionError("Recipes must remain offline")

    monkeypatch.setattr(socket, "socket", reject_network)
    monkeypatch.setattr(socket, "create_connection", reject_network)
    for item in recipe_catalog():
        assert propose_recipe(item["id"], "Creator inspiration")["traits"] == item["traits"]
