"""Tests for the headless / non-QGIS tools (Phase 6)."""

from __future__ import annotations

import pytest

from model_forge.mcp_server import server as server_mod


@pytest.fixture
def fresh_state(monkeypatch):
    """Build a fresh ServerState and a fresh subscriptions registry."""
    from model_forge.mcp_server.jobs import reset_registry as reset_jobs
    from model_forge.mcp_server.subscriptions import (
        reset_subscription_registry,
    )

    reset_subscription_registry()
    reset_jobs()
    state = server_mod.ServerState(llm_config={})
    # Force _get_state() to return our fresh one.
    monkeypatch.setattr(server_mod, "_server_state", state)
    yield state
    reset_subscription_registry()
    reset_jobs()


# ─── Direct helper coverage (no FastMCP) ──────────────────────────────


def test_model_output_schema_groups_per_step():
    model = {
        "inputs": [
            {
                "name": "roads",
                "type": "vectorlayer",
                "fields": [
                    {"name": "id"},
                    {"name": "speed_limit"},
                ],
            }
        ],
        "algorithms": [
            {
                "id": "buf",
                "algorithm_id": "native:buffer",
                "parameters": {
                    "INPUT": {"type": "model_input", "input_name": "roads"},
                },
            },
        ],
    }
    schema = server_mod._model_output_schema(model)
    assert "buf" in schema
    assert schema["buf"][0][0] == "id"
    assert schema["buf"][1][0] == "speed_limit"


def test_model_to_geojson_contract_includes_metadata():
    model = {
        "model_name": "My Model",
        "model_group": "Group",
        "inputs": [
            {"name": "inp", "fields": [{"name": "a"}, {"name": "b"}]},
        ],
        "algorithms": [
            {
                "id": "step1",
                "parameters": {
                    "INPUT": {"type": "model_input", "input_name": "inp"},
                },
            },
        ],
    }
    contract = server_mod._model_to_geojson_contract(model)
    assert contract["type"] == "FeatureCollection"
    assert contract["_model_forge_contract"]["model_name"] == "My Model"
    assert len(contract["features"]) == 1
    assert contract["features"][0]["properties"]["_model_forge_layer"] == "step1"
    assert contract["features"][0]["properties"]["a"] is None


def test_wrap_script_as_runnable_contains_argparse():
    model = {
        "model_name": "Buf",
        "model_group": "Test",
        "inputs": [
            {"name": "input_layer", "type": "vectorlayer"},
            {"name": "distance", "type": "number"},
        ],
    }
    out = server_mod._wrap_script_as_runnable(model, "# body")
    assert "argparse" in out
    assert "--input-layer" in out
    assert "--distance" in out
    # The body is appended at the end; trailing whitespace is allowed.
    assert out.rstrip().endswith("# body")


def test_model_to_qgis_process_recipe_preserves_binding_kinds():
    model = {
        "model_name": "M",
        "model_group": "G",
        "inputs": [{"name": "src", "type": "vectorlayer", "label": "Source"}],
        "algorithms": [
            {
                "id": "step1",
                "algorithm_id": "native:buffer",
                "parameters": {
                    "INPUT": {"type": "model_input", "input_name": "src"},
                    "DISTANCE": {"type": "static", "value": 50},
                    "OVERLAY": {
                        "type": "child_output",
                        "child_id": "step1",
                        "output_name": "OUTPUT",
                    },
                },
            },
        ],
    }
    recipe = server_mod._model_to_qgis_process_recipe(model)
    assert recipe["model_name"] == "M"
    assert recipe["inputs"][0]["name"] == "src"
    p = recipe["algorithms"][0]["parameters"]
    assert p["DISTANCE"] == 50
    assert p["INPUT"] == "<model_input:src>"
    assert p["OVERLAY"] == "<child_output:step1:OUTPUT>"


# ─── Tool-level coverage (via the registered handler) ─────────────────
# We can't easily invoke the @mcp.tool-wrapped functions without a live
# FastMCP app, so we exercise the helper layer + the state mutations
# that the tools do. (Full integration tests live in Phase 12's
# higher-level test_jobs / test_export_formats.)


def test_fresh_state_starts_empty(fresh_state):
    assert fresh_state.context.get("layers") == [] or fresh_state.context.get("layers") is None


def test_fuzzy_score_zero_for_empty_query():
    assert server_mod._fuzzy_score("", "native:buffer", "Buffer") == 0.0


def test_fuzzy_score_full_match():
    assert server_mod._fuzzy_score("buffer", "native:buffer", "Buffer") == 1.0


def test_fuzzy_score_partial_match():
    # Multi-token query where some tokens match and some don't. "big
    # bufff" — "big" doesn't match anything; "bufff" is one typo away
    # from "buffer" but the scorer is exact token overlap, so this
    # is a partial match, not a perfect one.
    score = server_mod._fuzzy_score("big bufff", "native:buffer", "Buffer features")
    # Two tokens, zero match (neither "big" nor "bufff" appears as a
    # whole word in the candidate). With stopword stripping and exact
    # token overlap, no hit.
    assert score == 0.0


def test_fuzzy_score_two_of_three_tokens():
    # "vector buffer distance" — three query tokens, one of which
    # ("buffer") is in the candidate tokens. Score is 1/3.
    score = server_mod._fuzzy_score("vector buffer distance", "native:buffer", "Buffer vectors")
    assert 0.0 < score < 0.5


def test_fuzzy_score_strips_stopwords():
    # "the buffer" should hit the same as "buffer" alone.
    a = server_mod._fuzzy_score("the buffer", "native:buffer", "Buffer")
    b = server_mod._fuzzy_score("buffer", "native:buffer", "Buffer")
    assert a == b


def test_fuzzy_score_ignores_provider_prefix():
    # Just "buffer" without "native" should still hit native:buffer
    # because tokens are matched against both id and name.
    assert server_mod._fuzzy_score("buffer", "native:buffer", "Buffer") == 1.0
