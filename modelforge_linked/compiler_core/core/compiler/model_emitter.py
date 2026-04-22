"""
Stage 6 - ModelEmitter
Converts a validated ExecutablePlan into the ModelForge model_json dict
compatible with ModelBuilder.build_model().
"""
from __future__ import annotations
from typing import Dict, Any
from ..ir import ExecutablePlan, StepStatus, ParamKind


_QGIS_PARAM_TYPE = {
    ParamKind.VECTOR_LAYER:  "source",
    ParamKind.RASTER_LAYER:  "raster",
    ParamKind.FIELD:         "field",
    ParamKind.EXPRESSION:    "expression",
    ParamKind.NUMBER:        "number",
    ParamKind.BOOLEAN:       "boolean",
    ParamKind.STRING:        "string",
    ParamKind.ENUM:          "enum",
    ParamKind.CRS:           "crs",
    ParamKind.EXTENT:        "extent",
    ParamKind.SINK:          "sink",
    ParamKind.FEATURE_SINK:  "sink",
    ParamKind.RASTER_DEST:   "rasterdestination",
    ParamKind.UNKNOWN:       "string",
}


class ModelEmitter:
    def emit(
        self,
        plan: ExecutablePlan,
        model_name: str,
        model_group: str,
    ) -> Dict[str, Any]:
        if not plan.is_valid:
            # Return partial JSON even on errors so the user can inspect it
            pass

        inputs = []
        for inp in plan.inputs:
            inputs.append({
                "name":         inp.name,
                "label":        inp.label,
                "type":         _QGIS_PARAM_TYPE.get(inp.kind, "string"),
                "description":  inp.description,
                "optional":     inp.optional,
                "default":      inp.default_value,
                "pos_x":        inp.pos_x,
                "pos_y":        inp.pos_y,
            })

        algorithms = []
        for step in plan.steps:
            if step.algorithm is None:
                continue

            params = {}
            for pname, binding in step.parameters.items():
                src = binding.source_type
                if src == "model_input":
                    params[pname] = {"type": "model_input", "input_name": binding.model_input}
                elif src == "child_output":
                    params[pname] = {
                        "type":        "child_output",
                        "child_id":    binding.child_id,
                        "output_name": binding.output_name,
                    }
                elif src == "expression":
                    rendered = (binding.expression.rendered
                                if binding.expression else "") or ""
                    params[pname] = {"type": "static", "value": rendered}
                elif src == "enum_index":
                    params[pname] = {"type": "static", "value": binding.enum_index}
                else:
                    params[pname] = {"type": "static", "value": binding.static_value}

            algorithms.append({
                "id":           step.step_id,
                "description":  step.label,
                "algorithm_id": step.algorithm.algorithm_id,
                "parameters":   params,
                "pos_x":        step.pos_x,
                "pos_y":        step.pos_y,
                "status":       step.status.value,
                "confidence":   step.confidence,
            })

        return {
            "model_name":  model_name,
            "model_group": model_group,
            "inputs":      inputs,
            "algorithms":  algorithms,
            "_mf_plan_issues": [
                {"level": i.level.value, "code": i.code, "message": i.message}
                for i in plan.issues
            ],
        }
