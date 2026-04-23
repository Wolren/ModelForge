"""
Stage 4 - ExpressionValidator
For any parameter bound via 'expression' source_type, calls the
build_expression MCP tool and stores the rendered QGIS expression string.
"""
from __future__ import annotations
from ..ir import ExecutablePlan, ExpressionNode


class ExpressionValidator:
    def validate(self, plan: ExecutablePlan, mcp_client):
        for step in plan.steps:
            for pname, binding in step.parameters.items():
                if binding.source_type == "expression" and binding.expression is None:
                    # The constraint came as a string in the semantic plan
                    # and needs to be turned into an expression node.
                    pass  # No action needed if expression already absent
                if binding.source_type == "expression" and isinstance(binding.expression, str):
                    # LLM returned expression as a plain string; wrap it.
                    binding.expression = ExpressionNode(
                        node_type="literal",
                        rendered=binding.expression,
                    )
                if (
                    binding.source_type == "expression"
                    and binding.expression is not None
                    and binding.expression.rendered is None
                ):
                    try:
                        result = mcp_client.call("build_expression", {
                            "constraint_text": str(binding.expression.value or ""),
                            "layer_fields": [],
                        })
                        binding.expression.rendered = result.get("rendered", "")
                    except Exception:
                        binding.expression.rendered = ""
