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
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional

from ..ir import (
    ExecutablePlan, ResolvedAlgorithm, ParameterBinding,
    StepStatus, IssueLevel, PlanIssue,
)


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
        self._registry_cache: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def apply_resolutions(
        self,
        plan: ExecutablePlan,
        resolved: Dict[str, Any],
    ) -> None:
        """
        Mutates *plan* in-place:
          • sets step.status, step.algorithm, step.parameters
          • appends PlanIssue entries for every problem found
        """
        registry_cache = self._get_registry_cache()
        by_id = {s.step_id: s for s in plan.steps}

        for res in resolved.get("resolved_steps", []):
            step = by_id.get(res.get("step_id"))
            if step is None:
                continue

            alg_id     = (res.get("algorithm_id") or "").strip()
            confidence = float(res.get("confidence", 0.0))
            status_str = res.get("status", "assumed").lower()

            # ── Determine step status ─────────────────────────────────
            if status_str == "blocked":
                step.status = StepStatus.BLOCKED
                plan.issues.append(PlanIssue(
                    level=IssueLevel.ERROR,
                    code="ALGORITHM_BLOCKED",
                    message=(
                        f"Step '{step.step_id}' could not be resolved to a "
                        f"QGIS algorithm: {res.get('reason', 'no reason given')}"
                    ),
                    step_id=step.step_id,
                ))
                continue

            if not alg_id:
                # LLM returned resolved but with no id — treat as assumed
                step.status = StepStatus.ASSUMED
                plan.issues.append(PlanIssue(
                    level=IssueLevel.WARNING,
                    code="EMPTY_ALGORITHM_ID",
                    message=f"Step '{step.step_id}' resolved with empty algorithm_id.",
                    step_id=step.step_id,
                ))
            elif status_str == "resolved":
                step.status = StepStatus.RESOLVED
            else:
                step.status = StepStatus.ASSUMED

            # ── Live registry validation ──────────────────────────────
            known_params: List[str] = []
            if alg_id and registry_cache is not None:
                if alg_id not in registry_cache:
                    # Algorithm id not found — try case-insensitive fallback
                    alg_id = self._fuzzy_match(alg_id, registry_cache)

                if alg_id and alg_id in registry_cache:
                    known_params = [
                        p["name"]
                        for p in registry_cache[alg_id].get("parameters", [])
                    ]
                    # Upgrade ASSUMED to RESOLVED if registry confirms it
                    if step.status == StepStatus.ASSUMED:
                        step.status = StepStatus.RESOLVED
                else:
                    plan.issues.append(PlanIssue(
                        level=IssueLevel.ERROR,
                        code="UNKNOWN_ALGORITHM",
                        message=(
                            f"Step '{step.step_id}': algorithm '{alg_id}' not found "
                            f"in the QGIS Processing registry."
                        ),
                        step_id=step.step_id,
                    ))
                    step.status = StepStatus.BLOCKED
                    continue

            # ── Attach resolved algorithm ─────────────────────────────
            step.confidence = confidence
            step.algorithm  = ResolvedAlgorithm(
                algorithm_id  = alg_id,
                display_name  = res.get("display_name", alg_id),
                provider_id   = alg_id.split(":")[0] if ":" in alg_id else "unknown",
            )

            # ── Build parameter bindings ──────────────────────────────
            for pname, pbind in res.get("parameters", {}).items():
                # Warn if parameter name not in registry
                if known_params and pname not in known_params:
                    plan.issues.append(PlanIssue(
                        level=IssueLevel.WARNING,
                        code="UNKNOWN_PARAMETER",
                        message=(
                            f"Step '{step.step_id}': parameter '{pname}' does not "
                            f"exist on algorithm '{alg_id}'. "
                            f"Known: {', '.join(known_params[:8])}"
                        ),
                        step_id=step.step_id,
                    ))

                src = pbind.get("source_type", "static")
                step.parameters[pname] = ParameterBinding(
                    source_type  = src,
                    model_input  = pbind.get("model_input"),
                    child_id     = pbind.get("child_id"),
                    output_name  = pbind.get("output_name"),
                    static_value = pbind.get("static_value"),
                    enum_index   = pbind.get("enum_index"),
                )

            # ── Confidence warning ────────────────────────────────────
            if confidence < 0.6 and step.status != StepStatus.BLOCKED:
                plan.issues.append(PlanIssue(
                    level=IssueLevel.WARNING,
                    code="LOW_CONFIDENCE_RESOLUTION",
                    message=(
                        f"Step '{step.step_id}' resolved with low confidence "
                        f"({confidence:.2f}). Manual review recommended."
                    ),
                    step_id=step.step_id,
                ))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_registry_cache(self) -> Optional[Dict[str, Any]]:
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
                    max_algorithms=2000,   # fetch everything useful
                )
            except Exception:
                # Registry not ready (e.g. during plugin load) — skip validation
                self._registry_cache = None
        return self._registry_cache

    @staticmethod
    def _fuzzy_match(alg_id: str, catalog: Dict[str, Any]) -> str:
        """
        Try case-insensitive and provider-prefix-stripped matching.
        Returns the corrected id if found, else empty string.
        """
        lower = alg_id.lower()
        for key in catalog:
            if key.lower() == lower:
                return key
        # Strip provider prefix and try suffix match
        suffix = alg_id.split(":")[-1].lower() if ":" in alg_id else lower
        for key in catalog:
            if key.split(":")[-1].lower() == suffix:
                return key
        return ""