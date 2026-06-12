"""
Stage 3 - AlgorithmResolver
============================
Applies resolve_algorithms MCP tool output to the ExecutablePlan, then
validates every resolved algorithm_id against the live QGIS Processing
registry via RegistryCatalogService.

Validation steps per resolved step:
  1. algorithm_id exists in registry  → ERROR if not
  2. every bound parameter name exists on the algorithm  → WARNING if not
  3. confidence < 0.6 on ASSUMED steps  → WARNING
  4. if algorithm_id is empty but status is "resolved"  → demote to ASSUMED

Planner hints:
  When the upstream ``plan_workflow`` LLM call already supplied an
  ``algorithm_id`` in ``step.metadata.planner_algorithm_id``, the
  resolver treats that as the primary candidate and only asks the
  second LLM call for parameter bindings (which is the part the second
  call actually has the catalog information to do well). The
  ``constraints`` dict in the planner's output is also merged in as
  static-value parameter bindings, so a planner that says
  ``DISTANCE=50`` ends up with that binding on the emitted step
  without needing the resolver LLM call to repeat the work.
"""

from __future__ import annotations

import logging
from typing import Any

from ..ir import (
    ExecutablePlan,
    IssueLevel,
    ParameterBinding,
    PlanIssue,
    ResolvedAlgorithm,
    StepStatus,
)

log = logging.getLogger(__name__)

_COMMON_NATIVE_ALGORITHMS = {
    "buffer",
    "clip",
    "dissolve",
    "intersection",
    "union",
    "difference",
    "mergevectorlayers",
    "reprojectlayer",
    "extractbyexpression",
    "extractbylocation",
    "fixgeometries",
    "multiparttosingleparts",
    "joinattributestable",
    "fieldcalculator",
    "centroids",
    "convexhull",
    "polygonize",
    "refactorfields",
}


class AlgorithmResolver:
    def __init__(self, registry_catalog=None):
        """
        Parameters
        ----------
        registry_catalog : RegistryCatalogService | None
            If None the resolver skips live-registry validation (e.g. in
            unit-test environments without QGIS).  In production the
            CompilerPipeline injects a real RegistryCatalogService.
        """
        self._catalog_service = registry_catalog
        self._registry_cache: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def apply_resolutions(
        self,
        plan: ExecutablePlan,
        resolved: dict[str, Any],
    ) -> None:
        """
        Mutates *plan* in-place:
          • sets step.status, step.algorithm, step.parameters
          • appends PlanIssue entries for every problem found

        The resolver *trusts* the planner's ``algorithm_id`` when one
        is available in ``step.metadata.planner_algorithm_id``. The
        downstream LLM call (the one whose output is passed in as
        ``resolved``) is then only asked to fill in parameter
        bindings.
        """
        registry_cache = self._get_registry_cache()
        resolved_by_id = {res.get("step_id"): res for res in resolved.get("resolved_steps", [])}

        for step in plan.steps:
            res = resolved_by_id.get(step.step_id, {})
            planner_alg = (step.metadata.get("planner_algorithm_id") or "").strip()
            planner_constraints = step.metadata.get("constraints") or {}

            # ── Determine algorithm_id (resolver's or planner's) ───
            alg_id = (res.get("algorithm_id") or planner_alg or "").strip()
            confidence = float(res.get("confidence", step.confidence if step.confidence else 0.85))
            status_str = res.get("status", "resolved" if planner_alg else "assumed").lower()

            if status_str == "blocked":
                step.status = StepStatus.BLOCKED
                plan.issues.append(
                    PlanIssue(
                        level=IssueLevel.ERROR,
                        code="ALGORITHM_BLOCKED",
                        message=(
                            f"Step '{step.step_id}' could not be resolved to a "
                            f"QGIS algorithm: {res.get('reason', 'no reason given')}"
                        ),
                        step_id=step.step_id,
                    )
                )
                continue

            if not alg_id:
                step.status = StepStatus.ASSUMED
                plan.issues.append(
                    PlanIssue(
                        level=IssueLevel.WARNING,
                        code="EMPTY_ALGORITHM_ID",
                        message=f"Step '{step.step_id}' resolved with empty algorithm_id.",
                        step_id=step.step_id,
                    )
                )
            elif status_str == "resolved":
                step.status = StepStatus.RESOLVED
            else:
                step.status = StepStatus.ASSUMED

            # ── Live registry validation ──────────────────────────────
            known_params: list[str] = []
            if alg_id and registry_cache is not None:
                if alg_id not in registry_cache:
                    alg_id = self._fuzzy_match(alg_id, registry_cache)

                if alg_id and alg_id in registry_cache:
                    known_params = [p["name"] for p in registry_cache[alg_id].get("parameters", [])]
                    if step.status == StepStatus.ASSUMED:
                        step.status = StepStatus.RESOLVED
                else:
                    plan.issues.append(
                        PlanIssue(
                            level=IssueLevel.ERROR,
                            code="UNKNOWN_ALGORITHM",
                            message=(
                                f"Step '{step.step_id}': algorithm '{alg_id}' not found "
                                f"in the QGIS Processing registry."
                            ),
                            step_id=step.step_id,
                        )
                    )
                    step.status = StepStatus.BLOCKED
                    continue

            # ── Attach resolved algorithm ─────────────────────────────
            step.confidence = confidence
            step.algorithm = ResolvedAlgorithm(
                algorithm_id=alg_id,
                display_name=res.get("display_name", alg_id),
                provider_id=alg_id.split(":")[0] if ":" in alg_id else "unknown",
            )

            # ── Build parameter bindings ──────────────────────────────
            res_params = res.get("parameters") or {}
            merged_params: dict[str, dict] = dict(res_params)
            for cname, cval in planner_constraints.items():
                if cname not in merged_params:
                    merged_params[cname] = {
                        "source_type": "static",
                        "static_value": cval,
                    }

            for pname, pbind in merged_params.items():
                if known_params and pname not in known_params:
                    plan.issues.append(
                        PlanIssue(
                            level=IssueLevel.WARNING,
                            code="UNKNOWN_PARAMETER",
                            message=(
                                f"Step '{step.step_id}': parameter '{pname}' does not "
                                f"exist on algorithm '{alg_id}'. "
                                f"Known: {', '.join(known_params[:8])}"
                            ),
                            step_id=step.step_id,
                            param_name=pname,
                        )
                    )

                src = pbind.get("source_type", "static")
                step.parameters[pname] = ParameterBinding(
                    source_type=src,
                    model_input=pbind.get("model_input"),
                    child_id=pbind.get("child_id"),
                    output_name=pbind.get("output_name"),
                    static_value=pbind.get("static_value"),
                    enum_index=pbind.get("enum_index"),
                )

            # ── Confidence warning ────────────────────────────────────
            if step.status != StepStatus.BLOCKED and self._should_warn_low_confidence(
                alg_id, confidence
            ):
                plan.issues.append(
                    PlanIssue(
                        level=IssueLevel.WARNING,
                        code="LOW_CONFIDENCE_RESOLUTION",
                        message=(
                            f"Step '{step.step_id}' resolved with low confidence "
                            f"({confidence:.2f}). Manual review recommended."
                        ),
                        step_id=step.step_id,
                    )
                )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_registry_cache(self) -> dict[str, Any] | None:
        """Lazily fetch the algorithm catalog from the registry service."""
        if self._catalog_service is None:
            return None
        if self._registry_cache is None:
            try:
                self._registry_cache = self._catalog_service.get_algorithm_catalog(
                    include_native=True,
                    include_gdal=True,
                    include_grass=False,
                    include_saga=False,
                    include_all=False,
                    max_algorithms=2000,
                )
            except Exception:
                log.debug("Registry cache not ready — skipping validation")
                self._registry_cache = None
        return self._registry_cache

    _ALGORITHM_ALIASES: dict[str, str] = {
        "qgis:": "native:",
        "grass:": "grass7:",
        "native:fixgeometry": "native:fixgeometries",
        "native:multi2single": "native:multiparttosingleparts",
        "native:joinfield": "native:joinattributestable",
        "native:fieldcalc": "native:fieldcalculator",
        "native:singleparts": "native:multiparttosingleparts",
        "native:reproject": "native:reprojectlayer",
        "native:centroid": "native:centroids",
        "native:buffer": "native:buffer",
        "native:clip": "native:clip",
        "native:dissolve": "native:dissolve",
        "native:intersect": "native:intersection",
        "native:merge": "native:mergevectorlayers",
        "native:extractbyexpr": "native:extractbyexpression",
        "native:extractbyloc": "native:extractbylocation",
        "native:polygonize": "native:polygonize",
        "native:convexhull": "native:convexhull",
        "gdal:clipraster": "gdal:cliprasterbyextent",
    }

    @staticmethod
    def _fuzzy_match(alg_id: str, catalog: dict[str, Any]) -> str:
        lower = alg_id.lower()
        for key in catalog:
            if key.lower() == lower:
                return key

        if lower in AlgorithmResolver._ALGORITHM_ALIASES:
            alias_target = AlgorithmResolver._ALGORITHM_ALIASES[lower]
            if alias_target in catalog:
                return alias_target
        if lower in catalog:
            return lower

        suffix = alg_id.rsplit(":", maxsplit=1)[-1].lower() if ":" in alg_id else lower
        for key in catalog:
            if key.split(":")[-1].lower() == suffix:
                return key

        if ":" in alg_id:
            provider = alg_id.split(":", maxsplit=1)[0].lower()
            rest = alg_id.split(":", 1)[1].lower()
            if provider in ("qgis",):
                candidate = f"native:{rest}"
                for key in catalog:
                    if key.lower() == candidate:
                        return key
            for key in catalog:
                if key.split(":")[-1].lower() == rest:
                    return key

        return ""

    @staticmethod
    def _should_warn_low_confidence(alg_id: str, confidence: float) -> bool:
        if confidence >= 0.6:
            return False
        if not alg_id.startswith("native:"):
            return True

        suffix = alg_id.split(":", 1)[-1].lower()
        if suffix in _COMMON_NATIVE_ALGORITHMS:
            return confidence < 0.3
        return True
