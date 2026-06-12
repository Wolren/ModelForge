"""Tests for the rewritten inner-MCP tool prompts and helpers."""

from __future__ import annotations



from model_forge.compiler_core.core.mcp.tools import (
    plan_workflow,
    resolve_algorithms,
)


# ─── plan_workflow ─────────────────────────────────────────────────────


def test_plan_workflow_prompt_allows_algorithm_ids():
    """The new prompt must explicitly say algorithm IDs are allowed."""
    assert "algorithm_id" in plan_workflow.SYSTEM_PROMPT
    # The legacy prohibition must be gone.
    assert "do not use algorithm IDs" not in plan_workflow.SYSTEM_PROMPT.lower()


def test_plan_workflow_prompt_includes_worked_example():
    """A worked example dramatically improves model output quality.
    The prompt must have one.
    """
    assert "Worked example" in plan_workflow.SYSTEM_PROMPT
    # The example uses native:buffer — a familiar algorithm.
    assert "native:buffer" in plan_workflow.SYSTEM_PROMPT


def test_plan_workflow_prompt_documents_constraint_value_types():
    """The constraints value-type rules must be explicit so the
    model doesn't emit placeholders like '<value>'."""
    assert "number" in plan_workflow.SYSTEM_PROMPT
    assert "string" in plan_workflow.SYSTEM_PROMPT
    assert "boolean" in plan_workflow.SYSTEM_PROMPT
    # The prompt must explicitly tell the model NOT to use
    # placeholders. Look for the rule sentence, not a substring
    # (the JSON example legitimately contains 'value': 1000).
    assert "Never use placeholders" in plan_workflow.SYSTEM_PROMPT


def test_plan_workflow_prompt_handles_ambiguity():
    """The prompt must tell the model to surface open_questions
    rather than fabricate steps when the goal is underspecified."""
    assert "open_questions" in plan_workflow.SYSTEM_PROMPT
    assert "underspecified" in plan_workflow.SYSTEM_PROMPT


def test_plan_workflow_user_message_lists_all_layers_verbatim():
    """Truncation was the main cause of layer-name hallucination.
    The new builder must not truncate the layers list."""
    args = {
        "description": "Buffer the roads layer by 50m",
        "qgis_context": {
            "layers": [
                {
                    "id": f"layer_{i}",
                    "name": f"layer_{i}",
                    "type": "VectorLayer",
                    "crs": "EPSG:4326",
                    "feature_count": 100,
                    "fields": [{"name": "id"}, {"name": "name"}],
                }
                for i in range(50)
            ],
            "project_crs": "EPSG:4326",
        },
    }
    msg = plan_workflow.build_user_message(args)
    # All 50 layer names must appear.
    for i in range(50):
        assert f"'layer_{i}'" in msg
    # No truncation marker.
    assert "more layer" not in msg


def test_plan_workflow_user_message_omits_legacy_algorithm_dump():
    """The old builder dumped up to 80 raw algorithm IDs into the
    user message. The new builder must not — the resolver gets a
    relevance-ranked catalog later.
    """
    args = {
        "description": "Buffer",
        "qgis_context": {
            "layers": [],
            "algorithms": {
                "native:buffer": {"name": "Buffer"},
                "native:clip": {"name": "Clip"},
            },
        },
    }
    msg = plan_workflow.build_user_message(args)
    assert "Available algorithm IDs" not in msg
    assert "native:buffer" not in msg
    assert "native:clip" not in msg


def test_plan_workflow_user_message_handles_empty_layers():
    msg = plan_workflow.build_user_message(
        {"description": "X", "qgis_context": {"layers": [], "project_crs": None}}
    )
    assert "- none" in msg
    assert "unknown" in msg  # project_crs is unknown


# ─── resolve_algorithms helpers ───────────────────────────────────────


def test_resolve_tokenize_strips_stopwords():
    tokens = resolve_algorithms._tokenize("Buffer the input layer by 50 meters")
    assert "buffer" in tokens
    assert "layer" in tokens
    assert "meters" in tokens
    # Stopwords are gone.
    for sw in ("the", "by", "a", "of"):
        assert sw not in tokens


def test_resolve_relevance_score_is_zero_for_empty_intent():
    score = resolve_algorithms._relevance_score("native:buffer", {"name": "Buffer"}, set())
    assert score == 0.0


def test_resolve_relevance_score_matches_id_and_name():
    score = resolve_algorithms._relevance_score(
        "native:buffer", {"name": "Buffer", "group": "Vector geometry"}, {"buffer"}
    )
    assert score == 1.0


def test_resolve_user_message_ranks_planner_choice_first():
    """The resolver must see the planner's algorithm choice in its
    catalog, even if relevance scoring would rank it lower."""
    args = {
        "semantic_plan": {
            "steps": [
                {
                    "step_id": "x",
                    "intent": "Compute area for each polygon",
                    "algorithm_id": "qgis:fieldcalculator",
                    "constraints": {"FIELD_NAME": "area_m2"},
                }
            ],
            "model_inputs": [{"name": "poly"}],
        },
        "algorithm_catalog": {
            "qgis:fieldcalculator": {"name": "Field calculator", "parameters": [], "outputs": []},
            "native:buffer": {"name": "Buffer", "parameters": [], "outputs": []},
        },
    }
    msg = resolve_algorithms.build_user_message(args)
    # The planner's choice is in the catalog, even though "buffer"
    # wouldn't be a top relevance match for "compute area".
    assert "qgis:fieldcalculator" in msg
    assert "planner_algorithm_id" in msg


def test_resolve_user_message_is_capped_to_max_algs():
    """A huge catalog should not blow up the user message."""
    args = {
        "semantic_plan": {
            "steps": [
                {
                    "step_id": "x",
                    "intent": "do thing",
                    "algorithm_id": None,
                }
            ],
        },
        "algorithm_catalog": {
            f"native:alg_{i:04d}": {"name": f"Alg {i}", "parameters": [], "outputs": []}
            for i in range(500)
        },
    }
    msg = resolve_algorithms.build_user_message(args)
    # Hard cap is 40 by default; allow some slack for the planner
    # choice being double-counted.
    assert msg.count("native:alg_") <= 60


# ─── Semantic planner surfaces planner hints ─────────────────────────


def test_semantic_planner_promotes_resolved_when_planner_chose_algorithm():
    from model_forge.compiler_core.core.compiler.semantic_planner import SemanticPlanner
    from model_forge.compiler_core.core.ir import StepStatus

    sp = SemanticPlanner()
    plan = sp.build_plan(
        {
            "steps": [
                {
                    "step_id": "x",
                    "label": "Buffer",
                    "intent": "Buffer",
                    "algorithm_id": "native:buffer",
                    "inputs": [],
                    "outputs": ["OUTPUT"],
                    "constraints": {"DISTANCE": 50},
                    "needs_review": False,
                }
            ],
            "model_inputs": [],
            "open_questions": [],
        }
    )
    assert plan.steps[0].status == StepStatus.RESOLVED
    assert plan.steps[0].confidence == 0.85
    assert plan.steps[0].metadata.get("planner_algorithm_id") == "native:buffer"
    assert plan.steps[0].metadata.get("constraints") == {"DISTANCE": 50}
    assert plan.steps[0].metadata.get("needs_review") is False


def test_semantic_planner_assumed_when_no_planner_algorithm():
    from model_forge.compiler_core.core.compiler.semantic_planner import SemanticPlanner
    from model_forge.compiler_core.core.ir import StepStatus

    sp = SemanticPlanner()
    plan = sp.build_plan(
        {
            "steps": [
                {
                    "step_id": "x",
                    "label": "X",
                    "intent": "X",
                    "algorithm_id": None,
                    "inputs": [],
                    "outputs": ["OUTPUT"],
                    "constraints": {},
                    "needs_review": False,
                }
            ],
            "model_inputs": [],
            "open_questions": [],
        }
    )
    assert plan.steps[0].status == StepStatus.ASSUMED
    assert plan.steps[0].confidence == 0.0


def test_semantic_planner_surfaces_open_questions_and_needs_review():
    from model_forge.compiler_core.core.compiler.semantic_planner import SemanticPlanner

    sp = SemanticPlanner()
    plan = sp.build_plan(
        {
            "steps": [
                {
                    "step_id": "x",
                    "label": "X",
                    "intent": "X",
                    "algorithm_id": "native:buffer",
                    "inputs": [],
                    "outputs": ["OUTPUT"],
                    "constraints": {},
                    "needs_review": True,
                }
            ],
            "model_inputs": [],
            "open_questions": ["What CRS?"],
        }
    )
    codes = [i.code for i in plan.issues]
    assert "PLANNER_QUESTION" in codes
    assert "PLANNER_NEEDS_REVIEW" in codes
