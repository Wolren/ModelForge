"""
Builds the ModelForgeMCPServer with all six tools wired to the LLM backend.
"""
from __future__ import annotations
import json
import logging
from .server import ModelForgeMCPServer, MCPTool
from .tools import (
    plan_workflow, resolve_algorithms, build_expression,
    get_algorithm_docs, suggest_layout, generate_custom_step,
)

log = logging.getLogger(__name__)


def build_server(llm_backend) -> ModelForgeMCPServer:
    """
    llm_backend must implement:
        .chat(system_prompt: str, user_message: str) -> str
    """
    def _call(tool_mod, args: dict) -> dict:
        msg = tool_mod.build_user_message(args)
        raw = llm_backend.chat(tool_mod.SYSTEM_PROMPT, msg)
        raw = raw.strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            raw = "\n".join(lines).strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            log.error("JSON decode error for %s: %s\nRaw: %r", tool_mod.__name__, e, raw[:400])
            raise RuntimeError(
                f"LLM returned invalid JSON for {tool_mod.__name__}: {e}"
            ) from e

    tools = [
        MCPTool(
            name="plan_workflow",
            description="Decompose a geoprocessing goal into a typed SemanticPlan.",
            schema=plan_workflow.SCHEMA,
            handler=lambda args: _call(plan_workflow, args),
        ),
        MCPTool(
            name="resolve_algorithms",
            description="Resolve a semantic step intent to a QGIS algorithm + bindings.",
            schema=resolve_algorithms.SCHEMA,
            handler=lambda args: _call(resolve_algorithms, args),
        ),
        MCPTool(
            name="build_expression",
            description="Build a typed ExpressionNode from natural language.",
            schema=build_expression.SCHEMA,
            handler=lambda args: _call(build_expression, args),
        ),
        MCPTool(
            name="get_algorithm_docs",
            description="Return structured documentation for a QGIS algorithm.",
            schema=get_algorithm_docs.SCHEMA,
            handler=lambda args: _call(get_algorithm_docs, args),
        ),
        MCPTool(
            name="suggest_layout",
            description="Suggest groupings and annotations for graph layout.",
            schema=suggest_layout.SCHEMA,
            handler=lambda args: _call(suggest_layout, args),
        ),
        MCPTool(
            name="generate_custom_step",
            description="Generate a CustomStepSpec from a natural language description.",
            schema=generate_custom_step.SCHEMA,
            handler=lambda args: _call(generate_custom_step, args),
        ),
    ]
    return ModelForgeMCPServer(tools)
