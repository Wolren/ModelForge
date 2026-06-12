"""End-to-end pipeline test.

This is the test that should have existed from the start. It runs the
actual :class:`CompilerPipeline` with a deterministic fake LLM, feeds
it a "buffer then filter" goal, and asserts the emitted model JSON
is structurally valid (no duplicate ids, no dangling refs, all
parameter bindings are populated, the auto-wire pass filled the
INPUT bindings).

The fake LLM emits canned JSON for ``plan_workflow`` and
``resolve_algorithms``, and a trivial literal for
``build_expression``. The compiler's own validation (link_repair,
IR validator, resolver's registry check) runs against a tiny in-memory
catalog so the whole pipeline can be exercised in milliseconds.
"""

from __future__ import annotations

import json


from model_forge.compiler_core.core.compiler.algorithm_resolver import AlgorithmResolver
from model_forge.compiler_core.core.compiler.expression_validator import ExpressionValidator
from model_forge.compiler_core.core.compiler.intent_parser import IntentParser
from model_forge.compiler_core.core.compiler.ir_validator import IRValidator
from model_forge.compiler_core.core.compiler.link_repair import LinkRepairService
from model_forge.compiler_core.core.compiler.model_emitter import ModelEmitter
from model_forge.compiler_core.core.compiler.pipeline import CompilerPipeline
from model_forge.compiler_core.core.compiler.semantic_planner import SemanticPlanner


# ─── Fake inner MCP client ────────────────────────────────────────────


class _FakeMCPClient:
    """Replay a canned response for each inner-MCP tool call."""

    def __init__(self, responses: dict[str, dict]):
        self._responses = responses
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, args))
        if tool not in self._responses:
            raise AssertionError(f"Unexpected inner MCP tool call: {tool}")
        # Return a deep copy so the test can re-instantiate.
        return json.loads(json.dumps(self._responses[tool]))


# ─── Helpers ───────────────────────────────────────────────────────────


# Tiny in-memory catalog shaped like RegistryCatalogService output.
_CATALOG: dict[str, dict] = {
    "native:buffer": {
        "name": "Buffer",
        "group": "Vector geometry",
        "parameters": [
            {"name": "INPUT", "type": "source"},
            {"name": "DISTANCE", "type": "distance"},
            {"name": "SEGMENTS", "type": "number"},
            {"name": "OUTPUT", "type": "sink", "is_destination": True},
        ],
        "outputs": [{"name": "OUTPUT", "type": "sink"}],
    },
    "native:extractbyexpression": {
        "name": "Extract by expression",
        "group": "Vector selection",
        "parameters": [
            {"name": "INPUT", "type": "source"},
            {"name": "EXPRESSION", "type": "expression"},
            {"name": "OUTPUT", "type": "sink", "is_destination": True},
        ],
        "outputs": [{"name": "OUTPUT", "type": "sink"}],
    },
}


class _StubCatalogService:
    """In-memory replacement for RegistryCatalogService."""

    def get_algorithm_catalog(self, **kwargs):
        return _CATALOG


def _build_pipeline() -> CompilerPipeline:
    return CompilerPipeline(
        intent_parser=IntentParser(),
        semantic_planner=SemanticPlanner(),
        algorithm_resolver=AlgorithmResolver(registry_catalog=_StubCatalogService()),
        expression_validator=ExpressionValidator(),
        ir_validator=IRValidator(),
        model_emitter=ModelEmitter(),
        link_repair=LinkRepairService(registry_catalog=_StubCatalogService()),
        registry_catalog=_StubCatalogService(),
    )


# ─── Test ──────────────────────────────────────────────────────────────


def test_e2e_pipeline_emits_valid_model_for_buffer_then_filter():
    """The canonical case: 'buffer the roads layer by 50m, then keep
    only segments longer than 1 km'. Verify the pipeline walks all
    seven stages, surfaces the planner's algorithm_id without
    contradicting it, fills INPUT bindings from the planner's model
    inputs, and emits a structurally valid model JSON.
    """
    fake_llm = _FakeMCPClient(
        {
            "plan_workflow": {
                "goal_summary": "Buffer roads 50m and keep segments > 1km.",
                "steps": [
                    {
                        "step_id": "buffer_roads",
                        "label": "Buffer roads 50m",
                        "intent": "Buffer the roads layer by 50 meters",
                        "algorithm_id": "native:buffer",
                        "inputs": ["roads"],
                        "outputs": ["OUTPUT"],
                        "constraints": {"DISTANCE": 50, "SEGMENTS": 8},
                        "needs_review": False,
                    },
                    {
                        "step_id": "extract_long",
                        "label": "Keep segments > 1 km",
                        "intent": "Filter to features whose length exceeds 1000 metres",
                        "algorithm_id": "native:extractbyexpression",
                        "inputs": ["buffer_roads"],
                        "outputs": ["OUTPUT"],
                        "constraints": {
                            "EXPRESSION": {"field": "$length", "op": ">", "value": 1000}
                        },
                        "needs_review": False,
                    },
                ],
                "model_inputs": [
                    {
                        "name": "roads",
                        "kind": "vectorlayer",
                        "label": "Roads",
                        "description": "",
                        "optional": False,
                    }
                ],
                "open_questions": [],
            },
            "resolve_algorithms": {
                "resolved_steps": [
                    {
                        "step_id": "buffer_roads",
                        "algorithm_id": "native:buffer",
                        "confidence": 1.0,
                        "status": "resolved",
                        "parameters": {
                            "INPUT": {
                                "source_type": "model_input",
                                "model_input": "roads",
                                "child_id": None,
                                "output_name": None,
                                "static_value": None,
                                "enum_index": None,
                            },
                            "DISTANCE": {
                                "source_type": "static",
                                "model_input": None,
                                "child_id": None,
                                "output_name": None,
                                "static_value": 50,
                                "enum_index": None,
                            },
                        },
                    },
                    {
                        "step_id": "extract_long",
                        "algorithm_id": "native:extractbyexpression",
                        "confidence": 1.0,
                        "status": "resolved",
                        "parameters": {
                            "INPUT": {
                                "source_type": "child_output",
                                "model_input": None,
                                "child_id": "buffer_roads",
                                "output_name": "OUTPUT",
                                "static_value": None,
                                "enum_index": None,
                            },
                            "EXPRESSION": {
                                "source_type": "static",
                                "model_input": None,
                                "child_id": None,
                                "output_name": None,
                                "static_value": "$length > 1000",
                                "enum_index": None,
                            },
                        },
                    },
                ]
            },
        }
    )

    pipeline = _build_pipeline()
    qgis_context = {
        "layers": [
            {
                "id": "roads_xyz",
                "name": "roads",
                "type": "VectorLayer",
                "crs": "EPSG:4326",
                "feature_count": 1234,
                "fields": [{"name": "id"}, {"name": "name"}],
            }
        ],
        "algorithms": _CATALOG,
        "project_crs": "EPSG:4326",
    }

    plan, model_json = pipeline.run(
        raw_text="Buffer the roads layer by 50m, then keep only segments longer than 1 km.",
        model_name="Buffer and Filter",
        model_group="ModelForge",
        qgis_context=qgis_context,
        mcp_client=fake_llm,
    )

    # ─── 1. The plan has the right number of steps ─────────────────
    assert [s.step_id for s in plan.steps] == ["buffer_roads", "extract_long"]
    assert {inp.name for inp in plan.inputs} == {"roads"}

    # ─── 2. Both steps resolved to the planner's algorithm choice ─
    assert plan.steps[0].algorithm.algorithm_id == "native:buffer"
    assert plan.steps[1].algorithm.algorithm_id == "native:extractbyexpression"
    assert plan.steps[0].status.value == "resolved"
    assert plan.steps[1].status.value == "resolved"

    # ─── 3. Parameter bindings flowed through ──────────────────────
    buf_params = plan.steps[0].parameters
    assert buf_params["INPUT"].source_type == "model_input"
    assert buf_params["INPUT"].model_input == "roads"
    assert buf_params["DISTANCE"].source_type == "static"
    assert buf_params["DISTANCE"].static_value == 50
    assert buf_params["SEGMENTS"].source_type == "static"  # from planner constraints
    assert buf_params["SEGMENTS"].static_value == 8

    ext_params = plan.steps[1].parameters
    assert ext_params["INPUT"].source_type == "child_output"
    assert ext_params["INPUT"].child_id == "buffer_roads"
    assert ext_params["INPUT"].output_name == "OUTPUT"
    assert ext_params["EXPRESSION"].static_value == "$length > 1000"

    # ─── 4. Model JSON is structurally well-formed ────────────────
    assert model_json["model_name"] == "Buffer and Filter"
    assert model_json["model_group"] == "ModelForge"
    assert {inp["name"] for inp in model_json["inputs"]} == {"roads"}
    assert [a["id"] for a in model_json["algorithms"]] == [
        "buffer_roads",
        "extract_long",
    ]
    ids = [a["id"] for a in model_json["algorithms"]]
    assert len(ids) == len(set(ids)), "duplicate step ids"

    # Every algorithm step has algorithm_id set.
    for entry in model_json["algorithms"]:
        assert entry["algorithm_id"], f"empty algorithm_id for {entry['id']}"
        assert entry["parameters"], f"empty parameters for {entry['id']}"

    # No error-level issues were raised.
    errors = [i for i in plan.issues if i.level.value == "error"]
    assert not errors, f"unexpected errors: {[i.message for i in errors]}"


def test_e2e_pipeline_passes_layer_fields_to_expression_builder():
    """``build_expression`` must receive the actual QGIS field names
    for the layer that the expression is bound against, not an
    empty list. The fake LLM asserts on this.
    """
    received: list[dict] = []

    def _capture_call(tool: str, args: dict) -> dict:
        if tool == "build_expression":
            received.append(args)
        return _responses[tool]

    # planner produces an expression binding
    plan_workflow_response = {
        "goal_summary": "Filter",
        "steps": [
            {
                "step_id": "extract_long",
                "label": "Filter",
                "intent": "Filter",
                "algorithm_id": "native:extractbyexpression",
                "inputs": ["roads"],
                "outputs": ["OUTPUT"],
                "constraints": {
                    "EXPRESSION": {
                        "field": "speed_limit",
                        "op": ">",
                        "value": 50,
                    }
                },
                "needs_review": False,
            }
        ],
        "model_inputs": [
            {
                "name": "roads",
                "kind": "vectorlayer",
                "label": "R",
                "description": "",
                "optional": False,
            }
        ],
        "open_questions": [],
    }
    resolve_response = {
        "resolved_steps": [
            {
                "step_id": "extract_long",
                "algorithm_id": "native:extractbyexpression",
                "confidence": 0.9,
                "status": "resolved",
                "parameters": {
                    "INPUT": {
                        "source_type": "model_input",
                        "model_input": "roads",
                        "child_id": None,
                        "output_name": None,
                        "static_value": None,
                        "enum_index": None,
                    },
                    "EXPRESSION": {
                        "source_type": "expression",
                        "model_input": None,
                        "child_id": None,
                        "output_name": None,
                        "static_value": '"speed_limit" > 50',
                        "enum_index": None,
                    },
                },
            }
        ]
    }
    # The build_expression tool returns a rendered literal.
    build_expression_response = {
        "node_type": "comparison",
        "operator": ">",
        "rendered": '"speed_limit" > 50',
    }
    _responses = {
        "plan_workflow": plan_workflow_response,
        "resolve_algorithms": resolve_response,
        "build_expression": build_expression_response,
    }

    class _RecordingClient:
        def call(self, tool: str, args: dict) -> dict:
            return _capture_call(tool, args)

    pipeline = _build_pipeline()
    pipeline.run(
        raw_text="Filter",
        model_name="X",
        model_group="X",
        qgis_context={
            "layers": [
                {
                    "id": "r1",
                    "name": "roads",
                    "type": "VectorLayer",
                    "crs": "EPSG:4326",
                    "feature_count": 100,
                    "fields": [{"name": "id"}, {"name": "speed_limit"}, {"name": "name"}],
                }
            ],
            "algorithms": _CATALOG,
            "project_crs": "EPSG:4326",
        },
        mcp_client=_RecordingClient(),
    )

    assert received, "build_expression was not called"
    fields = received[0].get("layer_fields", [])
    assert "speed_limit" in fields
    assert "id" in fields
    assert "name" in fields


# ─── Defensive tests ──────────────────────────────────────────────────


def test_e2e_pipeline_blocks_unknown_algorithm():
    """Planner-supplied algorithm_id that doesn't exist in the
    registry: the resolver must mark the step BLOCKED and emit an
    UNKNOWN_ALGORITHM issue, *not* silently pass the fake id
    through to the model JSON.
    """
    fake_llm = _FakeMCPClient(
        {
            "plan_workflow": {
                "goal_summary": "Do a thing.",
                "steps": [
                    {
                        "step_id": "bogus",
                        "label": "Do a thing",
                        "intent": "Do a thing",
                        "algorithm_id": "native:does_not_exist",
                        "inputs": ["in"],
                        "outputs": ["OUTPUT"],
                        "constraints": {},
                        "needs_review": False,
                    }
                ],
                "model_inputs": [
                    {
                        "name": "in",
                        "kind": "vectorlayer",
                        "label": "In",
                        "description": "",
                        "optional": False,
                    }
                ],
                "open_questions": [],
            },
            "resolve_algorithms": {"resolved_steps": []},
        }
    )

    pipeline = _build_pipeline()
    plan, model_json = pipeline.run(
        raw_text="Do a thing.",
        model_name="X",
        model_group="X",
        qgis_context={"layers": [], "algorithms": _CATALOG, "project_crs": "EPSG:4326"},
        mcp_client=fake_llm,
    )

    assert all(a["id"] != "bogus" for a in model_json["algorithms"])
    codes = {i.code for i in plan.issues}
    assert "UNKNOWN_ALGORITHM" in codes


def test_e2e_resolver_merges_planner_constraints_into_params():
    """The planner's ``constraints`` dict is the primary source of
    static parameter values. The resolver must merge them into
    the step's parameter bindings even when the resolver's own
    response omits those parameters (so the second LLM call only
    has to do the *new* work, not repeat the planner's).
    """
    fake_llm = _FakeMCPClient(
        {
            "plan_workflow": {
                "goal_summary": "Buffer",
                "steps": [
                    {
                        "step_id": "buf",
                        "label": "Buffer 50m",
                        "intent": "Buffer by 50m with 8 segments",
                        "algorithm_id": "native:buffer",
                        "inputs": ["in"],
                        "outputs": ["OUTPUT"],
                        "constraints": {"DISTANCE": 50, "SEGMENTS": 8, "DISSOLVE": False},
                        "needs_review": False,
                    }
                ],
                "model_inputs": [
                    {
                        "name": "in",
                        "kind": "vectorlayer",
                        "label": "In",
                        "description": "",
                        "optional": False,
                    }
                ],
                "open_questions": [],
            },
            # Resolver only sets INPUT; relies on the planner's
            # constraints for DISTANCE / SEGMENTS / DISSOLVE.
            "resolve_algorithms": {
                "resolved_steps": [
                    {
                        "step_id": "buf",
                        "algorithm_id": "native:buffer",
                        "confidence": 0.9,
                        "status": "resolved",
                        "parameters": {
                            "INPUT": {
                                "source_type": "model_input",
                                "model_input": "in",
                                "child_id": None,
                                "output_name": None,
                                "static_value": None,
                                "enum_index": None,
                            }
                        },
                    }
                ]
            },
        }
    )

    pipeline = _build_pipeline()
    plan, _ = pipeline.run(
        raw_text="Buffer by 50m.",
        model_name="X",
        model_group="X",
        qgis_context={"layers": [], "algorithms": _CATALOG, "project_crs": "EPSG:4326"},
        mcp_client=fake_llm,
    )

    params = plan.steps[0].parameters
    # All three planner constraints are present as static bindings.
    assert params["DISTANCE"].source_type == "static"
    assert params["DISTANCE"].static_value == 50
    assert params["SEGMENTS"].source_type == "static"
    assert params["SEGMENTS"].static_value == 8
    assert params["DISSOLVE"].source_type == "static"
    assert params["DISSOLVE"].static_value is False
    # The resolver's INPUT binding is preserved.
    assert params["INPUT"].source_type == "model_input"
    assert params["INPUT"].model_input == "in"


def test_e2e_pipeline_surfaces_open_questions_as_issues():
    """When the planner asks the user for clarification, those
    questions must surface as plan-level warnings so the user can
    see what the model was guessing.
    """
    fake_llm = _FakeMCPClient(
        {
            "plan_workflow": {
                "goal_summary": "Clean my data.",
                "steps": [],
                "model_inputs": [],
                "open_questions": [
                    "Which field should the buffer use to compute distance?",
                    "What is the target CRS for reprojection?",
                ],
            },
            "resolve_algorithms": {"resolved_steps": []},
        }
    )

    pipeline = _build_pipeline()
    plan, _ = pipeline.run(
        raw_text="Clean my data.",
        model_name="X",
        model_group="X",
        qgis_context={"layers": [], "algorithms": _CATALOG, "project_crs": "EPSG:4326"},
        mcp_client=fake_llm,
    )

    planner_qs = [i for i in plan.issues if i.code == "PLANNER_QUESTION"]
    assert len(planner_qs) == 2
    messages = " | ".join(i.message for i in planner_qs)
    assert "field" in messages
    assert "CRS" in messages
