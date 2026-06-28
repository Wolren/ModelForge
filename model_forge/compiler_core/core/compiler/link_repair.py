"""
Stage 4 - LinkRepairService
============================
Post-resolution link repair for step-to-step connections.

Repairs common LLM failures:
  1. Wrong output port names (LLM guesses "OUTPUT" when real name differs)
  2. Missing required parameter bindings that upstream steps can satisfy
  3. Circular dependencies that would crash QGIS at runtime
  4. Source type confusion (model_input vs child_output swapped)
  5. Static value type coercion (string booleans, string numbers)
  6. Enum index resolution (label vs index confusion)
  7. Parameter name fuzzy matching (misspelled param names)
  8. Destination parameter hardening (TEMPORARY_OUTPUT for intermediate steps)
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..ir import (
    ExecutablePlan,
    IssueLevel,
    ParameterBinding,
    PlanIssue,
)

log = logging.getLogger(__name__)

_QGIS_OUTPUT_TYPE_MAP: dict[int, str] = {
    0: "vector",
    1: "raster",
    2: "file",
    3: "number",
    4: "string",
    5: "html",
    6: "folder",
    7: "layer",
    8: "boolean",
    9: "pointcloud",
    10: "mesh",
    11: "table",
}

_QGIS_PARAM_TYPE_TO_OUTPUT: dict[str, list[str]] = {
    "source": ["vector", "layer", "table", "pointcloud", "mesh"],
    "vector": ["vector", "layer"],
    "raster": ["raster", "layer"],
    "number": ["number"],
    "string": ["string"],
    "boolean": ["boolean"],
    "field": ["string", "number"],
    "layer": ["vector", "raster", "layer", "pointcloud", "mesh", "table"],
    "mesh": ["mesh", "layer"],
    "pointcloud": ["pointcloud", "layer"],
    "table": ["table"],
    "file": ["file"],
    "folder": ["folder"],
    "extent": ["string", "number"],
    "crs": ["string", "number"],
}

_PREFERRED_OUTPUT_NAMES = ("OUTPUT", "RESULT", "OUTPUT_LAYER", "OUTPUT_TABLE", "OUTPUT_VECTOR")


class LinkRepairService:
    """Repair step connections after LLM resolution, before final validation."""

    def __init__(self, registry_catalog=None):
        self._catalog_service = registry_catalog
        self._output_cache: dict[str, list[dict[str, Any]]] = {}
        self._param_cache: dict[str, list[dict[str, Any]]] = {}
        self._enum_cache: dict[str, dict[str, list[str]]] = {}

    def repair(self, plan: ExecutablePlan) -> None:
        step_ids = {s.step_id for s in plan.steps}
        model_input_names = {inp.name for inp in plan.inputs}

        catalog = self._get_catalog()
        self._build_caches(plan, catalog)
        self._repair_source_types(plan, step_ids, model_input_names)
        self._repair_parameter_names(plan)
        self._coerce_static_values(plan)
        self._repair_output_names(plan)
        self._fill_missing_bindings(plan, catalog)
        self._harden_destination_params(plan)
        self._detect_cycles(plan)

    # -----------------------------------------------------------------
    # Phase 0: cache algorithm definitions from the live registry
    # -----------------------------------------------------------------

    def _get_catalog(self) -> dict[str, Any]:
        if self._catalog_service is None:
            return {}
        try:
            catalog = self._catalog_service.get_algorithm_catalog(
                include_native=True,
                include_gdal=True,
                include_grass=False,
                include_saga=False,
                include_all=False,
                max_algorithms=2000,
            )
            self._validate_catalog(catalog)
            return catalog
        except Exception:
            log.warning("_get_registry_catalog() failed, using empty catalog")
            return {}

    @staticmethod
    def _validate_catalog(catalog: dict[str, Any]) -> None:
        for alg_id, entry in catalog.items():
            if not isinstance(entry, dict):
                log.warning("Catalog entry %r is not a dict", alg_id)
                continue
            if not isinstance(entry.get("parameters"), list):
                log.warning("Catalog entry %r has no parameters list", alg_id)
            if not isinstance(entry.get("outputs"), list):
                log.warning("Catalog entry %r has no outputs list", alg_id)

    def _build_caches(self, plan: ExecutablePlan, catalog: dict[str, Any]) -> None:
        for step in plan.steps:
            if step.algorithm is None:
                continue
            alg_id = step.algorithm.algorithm_id
            entry = catalog.get(alg_id)
            if entry is None:
                entry = self._entry_from_algorithm(step.algorithm)
            outputs = entry.get("outputs", []) if entry else []
            # Fallback: query live QGIS registry when catalog/IR have no outputs
            if not outputs and alg_id:
                try:
                    from qgis.core import QgsApplication

                    qgs_alg = QgsApplication.processingRegistry().algorithmById(alg_id)
                    if qgs_alg:
                        outputs = [
                            {"name": odef.name(), "type": odef.type()}
                            for odef in qgs_alg.outputDefinitions()
                        ]
                except Exception:
                    pass
            self._output_cache[step.step_id] = outputs
            params = entry.get("parameters", []) if entry else []
            self._param_cache[step.step_id] = params
            enum_opts = {}
            for p in params:
                opts = p.get("enum_options")
                if opts:
                    enum_opts[p["name"]] = opts
            if enum_opts:
                self._enum_cache[step.step_id] = enum_opts

    @staticmethod
    def _entry_from_algorithm(alg) -> dict[str, Any]:
        if not hasattr(alg, "outputs") and not hasattr(alg, "parameters"):
            return {}
        entry: dict[str, Any] = {}
        if hasattr(alg, "outputs") and alg.outputs:
            entry["outputs"] = [
                {"name": o.name, "type": o.kind.value if hasattr(o.kind, "value") else str(o.kind)}
                for o in alg.outputs
            ]
        if hasattr(alg, "parameters") and alg.parameters:
            entry["parameters"] = [
                {
                    "name": p.name,
                    "type": p.kind.value if hasattr(p.kind, "value") else str(p.kind),
                    "optional": p.optional,
                    "default": p.default_value,
                    "enum_options": p.enum_options if hasattr(p, "enum_options") else [],
                }
                for p in alg.parameters
            ]
        return entry

    # -----------------------------------------------------------------
    # Phase 1: repair source type confusion (model_input ↔ child_output)
    # -----------------------------------------------------------------

    def _repair_source_types(
        self,
        plan: ExecutablePlan,
        step_ids: set[str],
        model_input_names: set[str],
    ) -> None:
        for step in plan.steps:
            for pname, binding in list(step.parameters.items()):
                if (
                    binding.source_type == "child_output"
                    and binding.child_id
                    and binding.child_id not in step_ids
                    and binding.child_id in model_input_names
                ):
                    plan.issues.append(
                        PlanIssue(
                            level=IssueLevel.WARNING,
                            code="REPAIRED_SOURCE_TYPE",
                            message=(
                                f"Step '{step.step_id}' param '{pname}': "
                                f"source_type 'child_output' with child_id "
                                f"'{binding.child_id}' matches a model input. "
                                f"Swapped to 'model_input'."
                            ),
                            step_id=step.step_id,
                            param_name=pname,
                        )
                    )
                    binding.source_type = "model_input"
                    binding.model_input = binding.child_id
                    binding.child_id = None
                    binding.output_name = None

                elif (
                    binding.source_type == "model_input"
                    and binding.model_input
                    and binding.model_input not in model_input_names
                    and binding.model_input in step_ids
                ):
                    plan.issues.append(
                        PlanIssue(
                            level=IssueLevel.WARNING,
                            code="REPAIRED_SOURCE_TYPE",
                            message=(
                                f"Step '{step.step_id}' param '{pname}': "
                                f"source_type 'model_input' with name "
                                f"'{binding.model_input}' matches a step ID. "
                                f"Swapped to 'child_output'."
                            ),
                            step_id=step.step_id,
                            param_name=pname,
                        )
                    )
                    binding.source_type = "child_output"
                    binding.child_id = binding.model_input
                    binding.output_name = "OUTPUT"
                    binding.model_input = None

                # Catch unknown source_type values
                valid_types = {"model_input", "child_output", "static", "expression", "enum_index"}
                if binding.source_type not in valid_types:
                    plan.issues.append(
                        PlanIssue(
                            level=IssueLevel.WARNING,
                            code="UNKNOWN_SOURCE_TYPE",
                            message=(
                                f"Step '{step.step_id}' param '{pname}': "
                                f"unknown source_type '{binding.source_type}'. "
                                f"Defaulted to 'static'."
                            ),
                            step_id=step.step_id,
                            param_name=pname,
                        )
                    )
                    binding.source_type = "static"
                    binding.static_value = None

    # -----------------------------------------------------------------
    # Phase 2: fuzzy-match parameter names against algorithm defs
    # -----------------------------------------------------------------

    def _repair_parameter_names(self, plan: ExecutablePlan) -> None:
        for step in plan.steps:
            alg_params = self._param_cache.get(step.step_id, [])
            if not alg_params:
                continue
            known = {p["name"]: p for p in alg_params}
            to_rename: list[tuple[str, str]] = []

            for pname in list(step.parameters.keys()):
                if pname in known:
                    continue
                match = self._fuzzy_param_name(pname, list(known.keys()))
                if match:
                    to_rename.append((pname, match))

            for old_name, new_name in to_rename:
                step.parameters[new_name] = step.parameters.pop(old_name)
                plan.issues.append(
                    PlanIssue(
                        level=IssueLevel.WARNING,
                        code="REPAIRED_PARAM_NAME",
                        message=(
                            f"Step '{step.step_id}': parameter '{old_name}' "
                            f"renamed to '{new_name}' to match algorithm definition."
                        ),
                        step_id=step.step_id,
                        param_name=old_name,
                    )
                )

    @staticmethod
    def _fuzzy_param_name(candidate: str, known: list[str]) -> str | None:
        """Find best matching known param name for a candidate."""
        clower = candidate.lower()
        # Exact case-insensitive
        for k in known:
            if k.lower() == clower:
                return k

        # Strip common suffixes like _LAYER, _SOURCE
        c_stripped = re.sub(
            r"_(?:layer|source|field|table|file|name|value)$", "", clower, flags=re.I
        )
        for k in known:
            k_stripped = re.sub(
                r"_(?:layer|source|field|table|file|name|value)$", "", k.lower(), flags=re.I
            )
            if c_stripped == k_stripped:
                return k

        # Substring match (one contains the other)
        for k in known:
            klower = k.lower()
            if clower in klower or klower in clower:
                return k

        return None

    # -----------------------------------------------------------------
    # Phase 3: coerce static values to correct types
    # -----------------------------------------------------------------

    def _coerce_static_values(self, plan: ExecutablePlan) -> None:
        for step in plan.steps:
            alg_params = self._param_cache.get(step.step_id, [])
            if not alg_params:
                continue
            param_map = {p["name"]: p for p in alg_params}

            for pname, binding in list(step.parameters.items()):
                if binding.source_type not in ("static", "enum_index"):
                    continue

                pdef = param_map.get(pname)
                if pdef is None:
                    continue

                ptype = str(pdef.get("type", "") or "")
                value = (
                    binding.static_value if binding.source_type == "static" else binding.enum_index
                )

                # Boolean coercion
                if "bool" in ptype.lower():
                    if isinstance(value, str):
                        coerced = value.strip().lower() in ("true", "1", "yes", "t")
                        if coerced != (
                            value.strip().lower()
                            in ("true", "1", "yes", "t", "false", "0", "no", "f")
                        ):
                            binding.static_value = coerced
                            plan.issues.append(
                                PlanIssue(
                                    level=IssueLevel.INFO,
                                    code="COERCED_VALUE",
                                    message=(
                                        f"Step '{step.step_id}' param '{pname}': "
                                        f"coerced string '{value}' to boolean."
                                    ),
                                    step_id=step.step_id,
                                    param_name=pname,
                                )
                            )

                # Number coercion
                elif (
                    "number" in ptype.lower()
                    or "range" in ptype.lower()
                    or "distance" in ptype.lower()
                ):
                    if isinstance(value, str) and value.strip():
                        try:
                            coerced = float(value)
                            binding.static_value = coerced
                        except ValueError:
                            pass

                # Enum index resolution
                elif "enum" in ptype.lower():
                    enum_options = self._enum_cache.get(step.step_id, {}).get(pname, [])
                    if not enum_options:
                        continue
                    if isinstance(value, str) and value.strip():
                        # Try to resolve option label → index
                        value_lower = value.strip().lower()
                        for idx, opt in enumerate(enum_options):
                            if opt.lower() == value_lower:
                                binding.source_type = "enum_index"
                                binding.enum_index = idx
                                binding.static_value = None
                                plan.issues.append(
                                    PlanIssue(
                                        level=IssueLevel.INFO,
                                        code="RESOLVED_ENUM_INDEX",
                                        message=(
                                            f"Step '{step.step_id}' param '{pname}': "
                                            f"resolved enum label '{value}' to index {idx}."
                                        ),
                                        step_id=step.step_id,
                                        param_name=pname,
                                    )
                                )
                                break

                    # Validate enum index is in range
                    if (
                        binding.source_type == "enum_index"
                        and binding.enum_index is not None
                        and not (0 <= int(binding.enum_index) < len(enum_options))
                    ):
                        plan.issues.append(
                            PlanIssue(
                                level=IssueLevel.WARNING,
                                code="ENUM_INDEX_OUT_OF_RANGE",
                                message=(
                                    f"Step '{step.step_id}' param '{pname}': "
                                    f"enum index {binding.enum_index} out of range "
                                    f"[0, {len(enum_options) - 1}]. Defaulted to 0."
                                ),
                                step_id=step.step_id,
                                param_name=pname,
                            )
                        )
                        binding.enum_index = 0

    # -----------------------------------------------------------------
    # Phase 4: harden destination parameters
    # -----------------------------------------------------------------

    def _harden_destination_params(self, plan: ExecutablePlan) -> None:
        last_step_ids = set()
        if plan.steps:
            last_step_ids.add(plan.steps[-1].step_id)

        for step in plan.steps:
            alg_params = self._param_cache.get(step.step_id, [])
            if not alg_params:
                continue

            is_last = step.step_id in last_step_ids

            for pdef in alg_params:
                if not pdef.get("is_destination", False):
                    continue
                pname = pdef["name"]
                binding = step.parameters.get(pname)
                if binding is None:
                    if not is_last:
                        step.parameters[pname] = ParameterBinding(
                            source_type="static",
                            static_value="TEMPORARY_OUTPUT",
                        )
                    continue

                if binding.source_type == "static":
                    current = str(binding.static_value or "").strip().lower()
                    if not is_last and current not in ("", "temporary_output"):
                        plan.issues.append(
                            PlanIssue(
                                level=IssueLevel.WARNING,
                                code="HARDENED_DESTINATION",
                                message=(
                                    f"Step '{step.step_id}' param '{pname}': "
                                    f"value '{binding.static_value}' replaced with "
                                    f"'TEMPORARY_OUTPUT' (intermediate step)."
                                ),
                                step_id=step.step_id,
                                param_name=pname,
                            )
                        )
                        binding.static_value = "TEMPORARY_OUTPUT"
                    elif not is_last and (current == "" or binding.static_value is None):
                        binding.static_value = "TEMPORARY_OUTPUT"

    # -----------------------------------------------------------------
    # Phase 5: repair wrong output port names on child_output bindings
    # -----------------------------------------------------------------

    def _repair_output_names(self, plan: ExecutablePlan) -> None:
        for step in plan.steps:
            for pname, binding in list(step.parameters.items()):
                if binding.source_type != "child_output":
                    continue
                if not binding.child_id:
                    continue

                actual = self._output_cache.get(binding.child_id, [])
                if not actual:
                    log.warning(
                        "No outputs found for child step %r (needed by %s param %s)",
                        binding.child_id,
                        step.step_id,
                        pname,
                    )
                    continue

                out_names = {o["name"] for o in actual}
                if binding.output_name in out_names:
                    continue

                repaired = self._find_best_output_name(
                    actual, pname, step.algorithm.algorithm_id if step.algorithm else ""
                )
                if repaired:
                    plan.issues.append(
                        PlanIssue(
                            level=IssueLevel.WARNING,
                            code="REPAIRED_OUTPUT_NAME",
                            message=(
                                f"Step '{step.step_id}' param '{pname}': "
                                f"output name '{binding.output_name}' not found on "
                                f"'{binding.child_id}', repaired to '{repaired}'."
                            ),
                            step_id=step.step_id,
                            param_name=pname,
                        )
                    )
                    binding.output_name = repaired

    @staticmethod
    def _find_best_output_name(
        outputs: list[dict[str, Any]],
        param_name: str,
        _alg_id: str = "",
    ) -> str | None:
        if not outputs:
            return None

        exact_name = outputs[0]["name"]
        if len(outputs) == 1:
            return exact_name

        out_by_name = {o["name"]: o for o in outputs}

        if param_name.upper() in out_by_name:
            return out_by_name[param_name.upper()]["name"]

        for pref in _PREFERRED_OUTPUT_NAMES:
            if pref in out_by_name:
                return out_by_name[pref]["name"]

        return exact_name

    # -----------------------------------------------------------------
    # Phase 2: fill missing required-parameter bindings from upstream
    # -----------------------------------------------------------------

    def _fill_missing_bindings(self, plan: ExecutablePlan, catalog: dict[str, Any]) -> None:
        step_ids = {s.step_id: s for s in plan.steps}

        for step in plan.steps:
            if step.algorithm is None:
                continue

            alg_params = self._param_cache.get(step.step_id, [])
            for pdef in alg_params:
                pname = pdef["name"]
                if pname in step.parameters:
                    continue
                if pdef.get("optional", False):
                    continue
                if pdef.get("is_destination", False):
                    continue

                upstream = self._find_upstream_match(step, pname, pdef, step_ids, catalog)
                if upstream:
                    child_id, output_name = upstream
                    step.parameters[pname] = ParameterBinding(
                        source_type="child_output",
                        child_id=child_id,
                        output_name=output_name,
                    )
                    plan.issues.append(
                        PlanIssue(
                            level=IssueLevel.INFO,
                            code="AUTO_LINKED",
                            message=(
                                f"Step '{step.step_id}' param '{pname}' "
                                f"auto-linked from '{child_id}' ({output_name})."
                            ),
                            step_id=step.step_id,
                            param_name=pname,
                        )
                    )

    def _find_upstream_match(
        self,
        step: Any,
        pname: str,
        pdef: dict[str, Any],
        step_ids: dict[str, Any],
        catalog: dict[str, Any],
    ) -> tuple[str, str] | None:
        param_type = pdef.get("type", "")
        compatible_outputs = _QGIS_PARAM_TYPE_TO_OUTPUT.get(param_type, [])

        upstream_steps = []
        for s in step_ids.values():
            if s.step_id == step.step_id:
                break
            upstream_steps.append(s)

        # Try exact parameter-name match first (e.g. "INPUT" -> upstream "OUTPUT")
        for us in reversed(upstream_steps):
            outputs = self._output_cache.get(us.step_id, [])
            for o in outputs:
                if o["name"].upper() in ("OUTPUT", "RESULT") and not compatible_outputs:
                    return us.step_id, o["name"]

        # Type-compatible match
        candidates = []
        for us in reversed(upstream_steps):
            outputs = self._output_cache.get(us.step_id, [])
            for o in outputs:
                out_type_str = _QGIS_OUTPUT_TYPE_MAP.get(o["type"], "")
                if (
                    out_type_str in compatible_outputs
                    or not compatible_outputs
                    or out_type_str == "layer"
                ):
                    candidates.append((us.step_id, o["name"], o))

        if not candidates:
            return None

        # Prefer OUTPUT among candidates
        for c in candidates:
            if c[1].upper() == "OUTPUT":
                return c[0], c[1]
        return candidates[0][0], candidates[0][1]

    # -----------------------------------------------------------------
    # Phase 3: detect circular dependencies
    # -----------------------------------------------------------------

    def _detect_cycles(self, plan: ExecutablePlan) -> None:
        adj: dict[str, list[str]] = {}
        for step in plan.steps:
            deps = [
                b.child_id
                for b in step.parameters.values()
                if b.source_type == "child_output" and b.child_id
            ]
            adj[step.step_id] = deps

        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {s.step_id: WHITE for s in plan.steps}

        def dfs(node: str, path: list[str]) -> None:
            color[node] = GRAY
            for dep in adj.get(node, []):
                if dep not in color:
                    continue
                if color[dep] == GRAY:
                    cycle = path + [dep]
                    plan.issues.append(
                        PlanIssue(
                            level=IssueLevel.ERROR,
                            code="CIRCULAR_DEPENDENCY",
                            message=f"Circular dependency: {' → '.join(cycle)}.",
                            step_id=node,
                        )
                    )
                    return
                if color[dep] == WHITE:
                    dfs(dep, path + [dep])
            color[node] = BLACK

        for step in plan.steps:
            if color[step.step_id] == WHITE:
                dfs(step.step_id, [step.step_id])
