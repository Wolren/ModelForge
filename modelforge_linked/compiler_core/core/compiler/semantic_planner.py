"""
Stage 2 - SemanticPlanner
Converts the plan_workflow MCP tool response into an ExecutablePlan.
"""
from __future__ import annotations
from typing import Dict, Any
from ..ir import (
    ExecutablePlan, ModelInput, ExecutableStep,
    ParamKind, StepStatus,
)

_KIND_MAP = {
    "vectorlayer":  ParamKind.VECTOR_LAYER,
    "rasterlayer":  ParamKind.RASTER_LAYER,
    "number":       ParamKind.NUMBER,
    "string":       ParamKind.STRING,
    "boolean":      ParamKind.BOOLEAN,
    "field":        ParamKind.FIELD,
    "expression":   ParamKind.EXPRESSION,
    "crs":          ParamKind.CRS,
    "extent":       ParamKind.EXTENT,
    "sink":         ParamKind.SINK,
    "featuresink":  ParamKind.FEATURE_SINK,
}


class SemanticPlanner:
    def build_plan(self, plan_dict: Dict[str, Any]) -> ExecutablePlan:
        plan = ExecutablePlan()

        for inp in plan_dict.get("model_inputs", []):
            kind = _KIND_MAP.get(str(inp.get("kind", "")).lower(), ParamKind.UNKNOWN)
            plan.inputs.append(ModelInput(
                name=inp.get("name", "input"),
                kind=kind,
                label=inp.get("label", inp.get("name", "Input")),
                description=inp.get("description", ""),
                optional=bool(inp.get("optional", False)),
            ))

        for step in plan_dict.get("steps", []):
            plan.steps.append(ExecutableStep(
                step_id=step.get("step_id", f"step_{len(plan.steps)}"),
                label=step.get("label", step.get("intent", "Step")),
                status=StepStatus.ASSUMED,
                confidence=0.0,
            ))

        plan.metadata["goal_summary"] = plan_dict.get("goal_summary", "")
        return plan
