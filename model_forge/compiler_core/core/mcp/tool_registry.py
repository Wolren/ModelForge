"""
Builds the ModelForgeMCPServer with the inner tools the compiler
pipeline actually calls. The pipeline invokes exactly three tools:

* ``plan_workflow``        — semantic decomposition
* ``resolve_algorithms``   — concrete algorithm + binding resolution
* ``build_expression``     — QGIS expression string rendering

The remaining tools that used to be registered here
(``get_algorithm_docs``, ``suggest_layout``, ``generate_custom_step``)
were not called by the pipeline and have been removed. If a future
pipeline stage needs them, re-add them here deliberately and wire
the corresponding call into ``CompilerPipeline.run``.
"""

from __future__ import annotations

import json
import logging

from ..llm.base import LLMRequestError, LLMResponseError, LLMTimeoutError
from .server import MCPTool, ModelForgeMCPServer
from .tools import (
    build_expression,
    plan_workflow,
    resolve_algorithms,
)

log = logging.getLogger(__name__)


def build_server(llm_backend) -> ModelForgeMCPServer:
    """
    llm_backend must implement:
        .chat(system_prompt: str, user_message: str) -> str
    """

    def _extract_json_payload(raw_text: str) -> str:
        raw = (raw_text or "").strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            raw = "\n".join(lines).strip()

        if raw.startswith("{") and raw.endswith("}"):
            return raw

        first = raw.find("{")
        last = raw.rfind("}")
        if first >= 0 and last > first:
            return raw[first : last + 1].strip()
        return raw

    def _call(tool_mod, args: dict) -> dict:
        base_message = tool_mod.build_user_message(args)
        retry_suffix = (
            "\n\nReturn only one valid JSON object. No prose, no markdown, no code fences."
        )
        attempts = 3

        for attempt in range(1, attempts + 1):
            message = base_message if attempt == 1 else (base_message + retry_suffix)
            try:
                raw = llm_backend.chat(tool_mod.SYSTEM_PROMPT, message)
                payload = _extract_json_payload(raw)
                return json.loads(payload)
            except json.JSONDecodeError as e:
                if attempt < attempts:
                    continue
                snippet = (_extract_json_payload(raw) if isinstance(raw, str) else "")[:400]
                log.error("JSON decode error for %s: %s\nRaw: %r", tool_mod.__name__, e, snippet)
                raise RuntimeError(
                    f"Tool '{tool_mod.__name__}' returned invalid JSON after {attempts} attempts."
                ) from e
            except LLMTimeoutError as e:
                if attempt < attempts:
                    continue
                raise RuntimeError(
                    f"Tool '{tool_mod.__name__}' timed out after {attempts} attempts."
                ) from e
            except (LLMRequestError, LLMResponseError) as e:
                raise RuntimeError(str(e)) from e

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
    ]
    return ModelForgeMCPServer(tools)
