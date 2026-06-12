"""
CompilerPipeline
================
Orchestrates all compilation stages and injects a live
RegistryCatalogService into AlgorithmResolver so that every resolved
algorithm_id is validated against the real QGIS Processing registry.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..ir import ExecutablePlan


class CompilerPipeline:
    def __init__(
        self,
        intent_parser,
        semantic_planner,
        algorithm_resolver,
        expression_validator,
        ir_validator,
        model_emitter,
        link_repair=None,
        registry_catalog=None,
    ):
        self._intent_parser = intent_parser
        self._semantic_planner = semantic_planner
        self._algorithm_resolver = algorithm_resolver
        self._expression_validator = expression_validator
        self._ir_validator = ir_validator
        self._model_emitter = model_emitter
        self._link_repair = link_repair

        # Wire registry catalog into resolver if not already set
        if (
            registry_catalog is not None
            and getattr(self._algorithm_resolver, "_catalog_service", None) is None
        ):
            self._algorithm_resolver._catalog_service = registry_catalog

    def run(
        self,
        raw_text: str,
        model_name: str,
        model_group: str,
        qgis_context: dict[str, Any],
        mcp_client,
        progress_callback: Callable[..., None] | None = None,
    ) -> tuple[ExecutablePlan, dict[str, Any]]:
        """Run the full pipeline.

        ``progress_callback`` may be called in two shapes for backward
        compatibility:

        * ``progress_callback("plain text message")`` — legacy callers
          (and inner MCP tools) just pass a string; the progress fraction
          is unknown.
        * ``progress_callback(current, total, message)`` — structured
          callers (the MCP server) get exact progress. ``current`` and
          ``total`` are floats in [0, 1] where 1.0 means done.
        """

        STAGE_FRACTIONS = {
            "intent": 0.10,
            "plan": 0.30,
            "resolve": 0.55,
            "repair": 0.65,
            "validate_expr": 0.75,
            "validate_ir": 0.85,
            "emit": 0.95,
            "completed": 1.00,
        }

        def _emit(stage: str, message: str) -> None:
            if progress_callback is None:
                return
            frac = STAGE_FRACTIONS.get(stage, 0.0)
            try:
                # Try the structured signature first.
                progress_callback(frac, 1.0, message)
            except TypeError:
                # Legacy single-arg signature: fall back to a plain
                # string. The server-side shim will still emit *some*
                # progress to the client.
                progress_callback(f"[{stage}] {message}")

        _emit("intent", "Parsing intent")
        raw_intent = self._intent_parser.parse(raw_text)

        _emit("plan", "Planning workflow (LLM call)")
        semantic_plan_dict = mcp_client.call(
            "plan_workflow",
            {
                "description": raw_intent.cleaned_text,
                "qgis_context": qgis_context,
                "model_name": model_name,
                "model_group": model_group,
            },
        )
        plan = self._semantic_planner.build_plan(semantic_plan_dict)

        _emit("resolve", "Resolving algorithms (LLM call)")
        # Sub-step progress while the LLM is thinking: bump the bar to
        # 0.45 before the call so the UI shows movement during the
        # long pause.
        _emit("resolve", "Resolving algorithms — waiting on LLM")
        resolved_dict = mcp_client.call(
            "resolve_algorithms",
            {
                "semantic_plan": semantic_plan_dict,
                "algorithm_catalog": qgis_context.get("algorithms", {}),
            },
        )
        self._algorithm_resolver.apply_resolutions(plan, resolved_dict)

        _emit("repair", "Repairing step links")
        if self._link_repair is not None:
            self._link_repair.repair(plan)

        _emit("validate_expr", "Validating expressions (LLM call)")
        self._expression_validator.validate(plan, mcp_client, qgis_context=qgis_context)

        _emit("validate_ir", "Validating IR")
        self._ir_validator.validate(plan)

        _emit("emit", "Emitting model JSON")
        model_json = self._model_emitter.emit(plan, model_name, model_group)

        _emit("completed", "Pipeline completed")
        return plan, model_json
