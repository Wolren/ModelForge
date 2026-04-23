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
        raw_steps = plan_dict.get("steps", [])

        for inp in plan_dict.get("model_inputs", []):
            kind = _KIND_MAP.get(str(inp.get("kind", "")).lower(), ParamKind.UNKNOWN)
            plan.inputs.append(ModelInput(
                name=inp.get("name", "input"),
                kind=kind,
                label=inp.get("label", inp.get("name", "Input")),
                description=inp.get("description", ""),
                optional=bool(inp.get("optional", False)),
            ))

        step_ids = {str(step.get("step_id", "")) for step in raw_steps if step.get("step_id")}
        step_dependencies = {}

        for step in raw_steps:
            step_id = step.get("step_id", f"step_{len(plan.steps)}")
            plan.steps.append(ExecutableStep(
                step_id=step_id,
                label=step.get("label", step.get("intent", "Step")),
                status=StepStatus.ASSUMED,
                confidence=0.0,
            ))
            deps = []
            for ref in step.get("inputs", []) or []:
                ref_id = str(ref or "")
                if ref_id and ref_id in step_ids and ref_id != step_id:
                    deps.append(ref_id)
            if deps:
                step_dependencies[step_id] = deps

        plan.metadata["goal_summary"] = plan_dict.get("goal_summary", "")
        plan.metadata["step_dependencies"] = step_dependencies
        return plan
