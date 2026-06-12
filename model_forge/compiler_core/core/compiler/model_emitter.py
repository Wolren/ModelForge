"""
Stage 6 - ModelEmitter
Converts a validated ExecutablePlan into the ModelForge model_json dict
compatible with ModelBuilder.build_model().
"""

from __future__ import annotations

import logging
from typing import Any

from ..ir import ExecutablePlan, ParamKind

log = logging.getLogger(__name__)

_PREFERRED_OUTPUT_NAMES = ("OUTPUT", "RESULT", "OUTPUT_LAYER", "OUTPUT_TABLE", "OUTPUT_VECTOR")

_QGIS_PARAM_TYPE = {
    ParamKind.VECTOR_LAYER: "source",
    ParamKind.RASTER_LAYER: "raster",
    ParamKind.FIELD: "field",
    ParamKind.EXPRESSION: "expression",
    ParamKind.NUMBER: "number",
    ParamKind.BOOLEAN: "boolean",
    ParamKind.STRING: "string",
    ParamKind.ENUM: "enum",
    ParamKind.CRS: "crs",
    ParamKind.EXTENT: "extent",
    ParamKind.SINK: "sink",
    ParamKind.FEATURE_SINK: "sink",
    ParamKind.RASTER_DEST: "rasterdestination",
    ParamKind.UNKNOWN: "string",
}


class ModelEmitter:
    def emit(
        self,
        plan: ExecutablePlan,
        model_name: str,
        model_group: str,
    ) -> dict[str, Any]:
        if not plan.is_valid:
            # Return partial JSON even on errors so the user can inspect it
            pass

        inputs = []
        for inp in plan.inputs:
            inputs.append(
                {
                    "name": inp.name,
                    "label": inp.label,
                    "type": _QGIS_PARAM_TYPE.get(inp.kind, "string"),
                    "description": inp.description,
                    "optional": inp.optional,
                    "default": inp.default_value,
                    "pos_x": inp.pos_x,
                    "pos_y": inp.pos_y,
                }
            )

        algorithms = []
        step_dependencies = plan.metadata.get("step_dependencies", {}) or {}
        for step in plan.steps:
            if step.algorithm is None:
                continue

            params = {}
            for pname, binding in step.parameters.items():
                src = binding.source_type
                if src == "model_input":
                    params[pname] = {
                        "type": "model_input",
                        "input_name": binding.model_input or "",
                    }
                elif src == "child_output":
                    out_name = self._resolve_output_name(
                        plan, binding.child_id, binding.output_name
                    )
                    params[pname] = {
                        "type": "child_output",
                        "child_id": binding.child_id or "",
                        "output_name": out_name,
                    }
                elif src == "expression":
                    rendered = (binding.expression.rendered if binding.expression else "") or ""
                    params[pname] = {"type": "static", "value": rendered or ""}
                elif src == "enum_index":
                    idx = binding.enum_index
                    params[pname] = {"type": "static", "value": 0 if idx is None else int(idx)}
                else:
                    val = binding.static_value
                    if val is None:
                        val = ""
                    params[pname] = {"type": "static", "value": val}

            alg_entry = {
                "id": step.step_id,
                "description": step.label,
                "algorithm_id": step.algorithm.algorithm_id,
                "parameters": params,
                "pos_x": step.pos_x,
                "pos_y": step.pos_y,
                "status": step.status.value,
                "confidence": step.confidence,
            }
            deps = step_dependencies.get(step.step_id, [])
            if deps:
                alg_entry["depends_on"] = [d for d in deps if d]
            algorithms.append(alg_entry)

        return {
            "model_name": model_name,
            "model_group": model_group,
            "inputs": inputs,
            "algorithms": algorithms,
            "_mf_plan_issues": [
                {"level": i.level.value, "code": i.code, "message": i.message} for i in plan.issues
            ],
        }

    @staticmethod
    def _resolve_output_name(
        plan: ExecutablePlan, child_id: str | None, current_name: str | None
    ) -> str:
        if current_name:
            return current_name
        if not child_id:
            return "OUTPUT"
        child_step = next((s for s in plan.steps if s.step_id == child_id), None)
        if child_step is None or child_step.algorithm is None:
            return "OUTPUT"
        try:
            from qgis.core import QgsApplication

            qgs_alg = QgsApplication.processingRegistry().algorithmById(
                child_step.algorithm.algorithm_id
            )
            if qgs_alg is None:
                return "OUTPUT"
            outputs = list(qgs_alg.outputDefinitions())
            if not outputs:
                return "OUTPUT"
            out_names = {odef.name() for odef in outputs if odef.name()}
            for pref in _PREFERRED_OUTPUT_NAMES:
                if pref in out_names:
                    return pref
            return outputs[0].name() or "OUTPUT"
        except Exception:
            log.warning("Failed to resolve output name for %s", child_id)
            return "OUTPUT"
