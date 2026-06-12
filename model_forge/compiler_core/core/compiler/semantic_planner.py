"""
Stage 2 - SemanticPlanner
Converts the plan_workflow MCP tool response into an ExecutablePlan.
"""

from __future__ import annotations

from typing import Any

from ..ir import (
    ExecutablePlan,
    ExecutableStep,
    IssueLevel,
    ModelInput,
    ParamKind,
    PlanIssue,
    StepStatus,
)

_KIND_MAP = {
    "vectorlayer": ParamKind.VECTOR_LAYER,
    "rasterlayer": ParamKind.RASTER_LAYER,
    "number": ParamKind.NUMBER,
    "string": ParamKind.STRING,
    "boolean": ParamKind.BOOLEAN,
    "field": ParamKind.FIELD,
    "expression": ParamKind.EXPRESSION,
    "crs": ParamKind.CRS,
    "extent": ParamKind.EXTENT,
    "sink": ParamKind.SINK,
    "featuresink": ParamKind.FEATURE_SINK,
}


class SemanticPlanner:
    def build_plan(self, plan_dict: dict[str, Any]) -> ExecutablePlan:
        plan = ExecutablePlan()
        raw_steps = plan_dict.get("steps", [])

        for inp in plan_dict.get("model_inputs", []):
            kind = _KIND_MAP.get(str(inp.get("kind", "")).lower(), ParamKind.UNKNOWN)
            plan.inputs.append(
                ModelInput(
                    name=inp.get("name", "input"),
                    kind=kind,
                    label=inp.get("label", inp.get("name", "Input")),
                    description=inp.get("description", ""),
                    optional=bool(inp.get("optional", False)),
                )
            )

        step_ids = {str(step.get("step_id", "")) for step in raw_steps if step.get("step_id")}
        step_dependencies = {}

        for step in raw_steps:
            step_id = step.get("step_id", f"step_{len(plan.steps)}")
            # Promote ASSUMED to RESOLVED when the planner already
            # gave us an algorithm_id; the resolver will still
            # validate it against the registry and demote back to
            # ASSUMED/BLOCKED if it doesn't match.
            initial_status = StepStatus.RESOLVED if step.get("algorithm_id") else StepStatus.ASSUMED
            initial_confidence = 0.85 if step.get("algorithm_id") else 0.0
            executable = ExecutableStep(
                step_id=step_id,
                label=step.get("label", step.get("intent", "Step")),
                status=initial_status,
                confidence=initial_confidence,
            )
            if step.get("algorithm_id"):
                # Stash the planner's algorithm_id so the resolver
                # can use it as the *primary* candidate instead of
                # having to re-derive it from intent text.
                executable.metadata = {
                    "planner_algorithm_id": step["algorithm_id"],
                    "constraints": step.get("constraints", {}),
                    "needs_review": bool(step.get("needs_review", False)),
                }
            plan.steps.append(executable)

            deps = []
            for ref in step.get("inputs", []) or []:
                ref_id = str(ref or "")
                if ref_id and ref_id in step_ids and ref_id != step_id:
                    deps.append(ref_id)
            if deps:
                step_dependencies[step_id] = deps

        plan.metadata["goal_summary"] = plan_dict.get("goal_summary", "")
        plan.metadata["step_dependencies"] = step_dependencies

        # Surface open_questions + needs_review flags as plan-level
        # warnings so the user can see what the model was guessing.
        for question in plan_dict.get("open_questions", []) or []:
            if not question:
                continue
            plan.issues.append(
                PlanIssue(
                    level=IssueLevel.WARNING,
                    code="PLANNER_QUESTION",
                    message=str(question),
                )
            )
        for step in plan.steps:
            if step.metadata.get("needs_review"):
                plan.issues.append(
                    PlanIssue(
                        level=IssueLevel.WARNING,
                        code="PLANNER_NEEDS_REVIEW",
                        message=(
                            f"Step '{step.step_id}' was marked needs_review by the planner; "
                            f"verify parameters in QGIS Model Designer."
                        ),
                        step_id=step.step_id,
                    )
                )

        return plan
