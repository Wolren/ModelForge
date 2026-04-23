"""
CompilerPipeline
================
Orchestrates all six compilation stages and injects a live
RegistryCatalogService into AlgorithmResolver so that every resolved
algorithm_id is validated against the real QGIS Processing registry.
"""
from __future__ import annotations
from typing import Any, Callable, Dict, Optional, Tuple
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
        registry_catalog=None,
    ):
        self._intent_parser        = intent_parser
        self._semantic_planner     = semantic_planner
        self._algorithm_resolver   = algorithm_resolver
        self._expression_validator = expression_validator
        self._ir_validator         = ir_validator
        self._model_emitter        = model_emitter

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
        qgis_context: Dict[str, Any],
        mcp_client,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Tuple[ExecutablePlan, Dict[str, Any]]:

        def _progress(msg: str) -> None:
            if progress_callback:
                progress_callback(msg)

        _progress("Stage 1/6: Parsing intent...")
        raw_intent = self._intent_parser.parse(raw_text)

        _progress("Stage 2/6: Planning workflow...")
        semantic_plan_dict = mcp_client.call("plan_workflow", {
            "description":  raw_intent.cleaned_text,
            "qgis_context": qgis_context,
            "model_name":   model_name,
            "model_group":  model_group,
        })
        plan = self._semantic_planner.build_plan(semantic_plan_dict)

        _progress("Stage 3/6: Resolving algorithms (+ registry validation)...")
        resolved_dict = mcp_client.call("resolve_algorithms", {
            "semantic_plan":     semantic_plan_dict,
            "algorithm_catalog": qgis_context.get("algorithms", {}),
        })
        self._algorithm_resolver.apply_resolutions(plan, resolved_dict)

        _progress("Stage 4/6: Validating expressions...")
        self._expression_validator.validate(plan, mcp_client)

        _progress("Stage 5/6: Validating IR...")
        self._ir_validator.validate(plan)

        _progress("Stage 6/6: Emitting model JSON...")
        model_json = self._model_emitter.emit(plan, model_name, model_group)

        return plan, model_json