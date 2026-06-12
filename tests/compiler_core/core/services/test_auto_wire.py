"""Tests for the auto-wire service."""

from __future__ import annotations

from model_forge.compiler_core.core.services import auto_wire
from model_forge.compiler_core.core.services.auto_wire import auto_wire_model_json


def _empty_model(algorithms, inputs=None):
    return {
        "model_name": "Test",
        "model_group": "TestGroup",
        "inputs": list(inputs or []),
        "algorithms": algorithms,
    }


def test_does_not_mutate_input():
    src = _empty_model(
        algorithms=[
            {"id": "buf", "algorithm_id": "native:buffer", "parameters": {}},
        ],
        inputs=[{"name": "Input", "type": "vectorlayer"}],
    )
    snapshot = {"id": src["algorithms"][0]["id"]}
    auto_wire_model_json(src)
    assert src["algorithms"][0]["id"] == snapshot["id"]


def test_wires_input_to_model_input():
    model = _empty_model(
        algorithms=[
            {"id": "buf", "algorithm_id": "native:buffer", "parameters": {}},
        ],
        inputs=[{"name": "Input", "type": "vectorlayer"}],
    )
    out = auto_wire_model_json(model, registry_lookup=False)
    # ``native:buffer`` is in the built-in table; INPUT is wired to the
    # model input named "Input" (case-insensitive normalization).
    params = out["algorithms"][0]["parameters"]
    assert params["INPUT"]["type"] == "model_input"
    assert params["INPUT"]["input_name"] == "Input"
    # And the output parameter gets a TEMPORARY_OUTPUT binding.
    assert params["OUTPUT"]["value"] == "TEMPORARY_OUTPUT"


def test_wires_upstream_to_child_output():
    model = _empty_model(
        algorithms=[
            {
                "id": "src",
                "algorithm_id": "native:buffer",
                "parameters": {"INPUT": "x", "OUTPUT": "memory:"},
            },
            {"id": "intersect", "algorithm_id": "native:intersection", "parameters": {}},
        ],
        inputs=[{"name": "Input", "type": "vectorlayer"}],
    )
    out = auto_wire_model_json(model, registry_lookup=False)
    params = out["algorithms"][1]["parameters"]
    # INPUT and OVERLAY should be wired; OVERLAY points to the
    # previous step's OUTPUT.
    assert params["INPUT"]["type"] == "model_input"
    assert params["OVERLAY"]["type"] == "child_output"
    assert params["OVERLAY"]["child_id"] == "src"


def test_step_renaming_preserves_existing_ids():
    model = _empty_model(
        algorithms=[
            {"id": "buffer1", "algorithm_id": "native:buffer", "parameters": {}},
            {"id": "buffer1", "algorithm_id": "native:buffer", "parameters": {}},
        ],
    )
    out = auto_wire_model_json(model, registry_lookup=False, renaming_strategy="preserve")
    ids = [a["id"] for a in out["algorithms"]]
    # The first is kept verbatim; the second gets a disambiguating
    # suffix so QGIS won't refuse to load the model.
    assert ids[0] == "buffer1"
    assert ids[1] != ids[0]


def test_step_renaming_label_slug():
    model = _empty_model(
        algorithms=[
            {"id": "", "algorithm_id": "native:buffer", "description": "Buffer 50m"},
            {"id": "", "algorithm_id": "native:buffer", "description": "Buffer 50m"},
        ],
    )
    out = auto_wire_model_json(model, registry_lookup=False, renaming_strategy="label_slug")
    ids = [a["id"] for a in out["algorithms"]]
    assert all(i for i in ids)
    assert len(set(ids)) == 2  # disambiguated


def test_slug_and_normalize_helpers():
    assert auto_wire.slug("Buffer Stage 1") == "buffer_stage_1"
    assert auto_wire.slug("___") == ""
    assert auto_wire.normalize_token("Buffer Stage 1") == "bufferstage1"
    assert auto_wire.normalize_token("ÉÀ") == ""  # non-ascii stripped


def test_does_not_overwrite_existing_binding():
    model = _empty_model(
        algorithms=[
            {
                "id": "buf",
                "algorithm_id": "native:buffer",
                "parameters": {"INPUT": {"type": "static", "value": "already_set"}},
            },
        ]
    )
    out = auto_wire_model_json(model, registry_lookup=False)
    assert out["algorithms"][0]["parameters"]["INPUT"]["value"] == "already_set"


def test_prefer_project_outputs_false_omits_temporary_output():
    model = _empty_model(
        algorithms=[{"id": "b", "algorithm_id": "native:buffer", "parameters": {}}],
        inputs=[{"name": "Input", "type": "vectorlayer"}],
    )
    out = auto_wire_model_json(model, registry_lookup=False, prefer_project_outputs=False)
    params = out["algorithms"][0]["parameters"]
    assert "OUTPUT" not in params
