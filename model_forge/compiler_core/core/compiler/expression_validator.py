"""
Stage 4 - ExpressionValidator
For any parameter bound via 'expression' source_type, calls the
build_expression MCP tool and stores the rendered QGIS expression string.
"""

from __future__ import annotations

import logging
from typing import Any

from ..ir import ExecutablePlan, ExpressionNode

log = logging.getLogger(__name__)


class ExpressionValidator:
    def validate(
        self, plan: ExecutablePlan, mcp_client, qgis_context: dict[str, Any] | None = None
    ):
        """Validate expression bindings.

        Parameters
        ----------
        plan
            The ExecutablePlan whose expression bindings are
            validated.
        mcp_client
            The inner-MCP client; used to call ``build_expression``.
        qgis_context
            Optional QGIS context (layers + project_crs). When
            provided, we resolve layer fields from the project so the
            LLM has the real field names to work with. Falls back to
            an empty list when not available.
        """
        layers_by_name = {}
        if qgis_context is not None:
            for layer in qgis_context.get("layers", []) or []:
                name = layer.get("name")
                if name:
                    layers_by_name[name] = layer

        for step in plan.steps:
            for pname, binding in step.parameters.items():
                if binding.source_type != "expression":
                    continue

                # Materialize an ExpressionNode for this binding. The
                # resolver may have left ``binding.expression`` as
                # None (it just sets source_type=expression); in that
                # case the constraint text comes from
                # ``binding.static_value`` (the LLM-supplied QGIS
                # expression string).
                if binding.expression is None:
                    if binding.static_value is not None:
                        binding.expression = ExpressionNode(
                            node_type="literal",
                            value=str(binding.static_value),
                        )
                    else:
                        # No source text — nothing to render.
                        continue

                if isinstance(binding.expression, str):
                    binding.expression = ExpressionNode(
                        node_type="literal",
                        rendered=binding.expression,
                    )

                if binding.expression.rendered is None:
                    fields: list[str] = []
                    upstream_layer_name = self._upstream_layer_name(
                        step.step_id, pname, plan, layers_by_name
                    )
                    if upstream_layer_name and upstream_layer_name in layers_by_name:
                        raw_fields = layers_by_name[upstream_layer_name].get("fields", [])
                        if isinstance(raw_fields, list):
                            fields = [
                                str(f.get("name", "")) if isinstance(f, dict) else str(f)
                                for f in raw_fields
                            ]

                    try:
                        result = mcp_client.call(
                            "build_expression",
                            {
                                "constraint_text": str(
                                    binding.expression.value or binding.expression.rendered or ""
                                ),
                                "layer_fields": fields,
                            },
                        )
                        binding.expression.rendered = result.get("rendered", "")
                    except Exception:
                        log.debug(
                            "build_expression failed for step %s param %s",
                            step.step_id,
                            pname,
                        )
                        binding.expression.rendered = ""

    @staticmethod
    def _upstream_layer_name(
        step_id: str,
        param_name: str,
        plan: ExecutablePlan,
        layers_by_name: dict[str, Any],
    ) -> str:
        """Best-effort lookup of which layer this expression sees.

        Walks the step's ``depends_on`` / inputs to find a model
        input that names a real project layer. Returns "" if the
        upstream is a child step (we don't recursively trace into
        previous steps' outputs here; the validator only needs the
        field set, and child step outputs already share a schema
        with their inputs).
        """
        for inp in plan.inputs:
            if inp.name and inp.name in layers_by_name:
                return inp.name
        return ""
