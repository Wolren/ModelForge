"""
Auto-wire service - fills in missing parameter bindings in a model JSON.

Originally lived inside ``ModelBuilderBridge`` (UI-only) but the MCP
server also needs the capability: LLM planners frequently emit step
definitions where the ``INPUT`` / ``OVERLAY`` / ``OUTPUT`` parameters
are either missing, ``None``, or unbound to a model input / upstream
output. ``auto_wire_model_json`` applies a small, dependency-free
heuristic pass:

1. Renames step ids to safe, unique slugs (optional).
2. For each step, looks at the algorithm's expected input parameter
   names and, for each missing one, tries to bind it to:
   a. a model input whose normalized name matches;
   b. otherwise, the previous step's preferred layer-like output.
3. For each step that has a layer-like output (``OUTPUT``, ``RESULT``,
   ``OUTPUT_LAYER``, etc.) and no explicit binding, sets it to
   ``TEMPORARY_OUTPUT`` so the model can be run end-to-end.

The function is pure (it deep-copies the input dict) and does not
require QGIS; the algorithm's *expected* input names are looked up
either from a small built-in table or from the optional QGIS
processing registry if it's importable.
"""

from __future__ import annotations

import copy
import re
from typing import Any

# Algorithms that we know enough about to wire up without contacting
# the QGIS registry. The keys are the algorithm's base name (lowercased,
# with the ``provider:`` prefix stripped); the values are the parameter
# names we expect to find a binding for.
_BUILTIN_ALG_INPUTS: dict[str, list[str]] = {
    "extractbyexpression": ["INPUT", "EXPRESSION"],
    "intersection": ["INPUT", "OVERLAY", "OUTPUT"],
    "difference": ["INPUT", "OVERLAY", "OUTPUT"],
    "clipvectorbypolygon": ["INPUT", "OVERLAY", "OUTPUT"],
    "buffervectors": ["INPUT", "DISTANCE", "OUTPUT"],
    "multiparttosingleparts": ["INPUT", "OUTPUT"],
}

# Output name preference order for child-to-child linking.
_PREFERRED_OUTPUT_NAMES: tuple[str, ...] = (
    "OUTPUT",
    "RESULT",
    "OUTPUT_LAYER",
    "OUTPUT_TABLE",
    "OUTPUT_VECTOR",
)


def normalize_token(value: str) -> str:
    """Lowercase + drop non-alphanumerics. Used for fuzzy input matching."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def slug(value: str) -> str:
    """Lowercase + replace non-alphanumerics with ``_``."""
    value = str(value or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", value).strip("_")


def _unique_id(base: str, used: set[str]) -> str:
    if base not in used:
        used.add(base)
        return base
    i = 2
    while f"{base}_{i}" in used:
        i += 1
    result = f"{base}_{i}"
    used.add(result)
    return result


def _compute_step_id(alg: dict[str, Any], idx: int, strategy: str, used: set[str]) -> str:
    raw_id = str(alg.get("id", "") or "")
    label = str(alg.get("description", "") or "")
    alg_id = str(alg.get("algorithm_id", "") or "")

    if strategy == "suffix_counter":
        base = slug(raw_id) or "step"
        return _unique_id(f"{base}_{idx}", used)

    if strategy == "label_slug":
        base = slug(label) or slug(alg_id.rsplit(":", maxsplit=1)[-1]) or "step"
        return _unique_id(f"{base}_{idx}", used)

    base = slug(raw_id) or "step"
    candidate = raw_id if raw_id and raw_id not in used else base
    return _unique_id(slug(candidate) or "step", used)


def _apply_step_renaming(model_json: dict[str, Any], strategy: str = "preserve") -> None:
    algorithms = model_json.get("algorithms", [])
    if not isinstance(algorithms, list):
        return
    strategy = (strategy or "preserve").lower()
    used: set[str] = set()
    id_map: dict[str, str] = {}

    for idx, alg in enumerate(algorithms, start=1):
        old_id = str(alg.get("id", "") or "")
        new_id = _compute_step_id(alg, idx, strategy, used)
        alg["id"] = new_id
        if old_id:
            id_map[old_id] = new_id

    for alg in algorithms:
        params = alg.get("parameters", {})
        if not isinstance(params, dict):
            continue
        for pbind in params.values():
            if isinstance(pbind, dict) and pbind.get("type") == "child_output":
                child_id = str(pbind.get("child_id", "") or "")
                if child_id in id_map:
                    pbind["child_id"] = id_map[child_id]


def _expected_inputs(algorithm_id: str, qgs_alg: Any | None = None) -> list[str]:
    """Return the parameter names this algorithm expects as inputs.

    If ``qgs_alg`` is provided (a ``QgsProcessingAlgorithm`` instance),
    we ask QGIS directly. Otherwise we fall back to the small built-in
    table, then to a generic ``['INPUT', 'OUTPUT']`` heuristic.
    """
    if qgs_alg is not None:
        try:
            names: list[str] = []
            for pdef in qgs_alg.parameterDefinitions():
                cname = pdef.__class__.__name__.lower()
                if any(h in cname for h in ("destination", "sink", "output")):
                    continue
                names.append(str(pdef.name()))
            return names
        except Exception:
            pass

    id_lower = (algorithm_id or "").lower()
    base = id_lower.split(":")[-1] if ":" in id_lower else id_lower
    if base in _BUILTIN_ALG_INPUTS:
        return list(_BUILTIN_ALG_INPUTS[base])
    return ["INPUT", "OUTPUT"]


def _preferred_output_name(qgs_alg: Any | None) -> str:
    """Pick the best output name to link to from a downstream step."""
    if qgs_alg is not None:
        try:
            outputs = list(qgs_alg.outputDefinitions())
        except Exception:
            outputs = []
        if outputs:
            by_name = {str(odef.name() or "").upper(): str(odef.name() or "") for odef in outputs}
            for key in _PREFERRED_OUTPUT_NAMES:
                if key in by_name and by_name[key]:
                    return by_name[key]
            return str(outputs[0].name() or "OUTPUT")
    return "OUTPUT"


def _resolve_qgs_algorithm(algorithm_id: str) -> Any | None:
    """Try to look up the algorithm in QGIS. Returns ``None`` on failure."""
    if not algorithm_id:
        return None
    try:
        from qgis.core import QgsApplication  # type: ignore

        registry = QgsApplication.processingRegistry()
        qgs_alg = registry.algorithmById(algorithm_id)
        if qgs_alg:
            return qgs_alg
    except Exception:
        return None
    return None


def auto_wire_model_json(
    model_json: dict[str, Any],
    *,
    prefer_project_outputs: bool = True,
    renaming_strategy: str = "preserve",
    registry_lookup: bool = True,
) -> dict[str, Any]:
    """Fill in missing parameter bindings.

    Parameters
    ----------
    model_json
        The model definition to be augmented. Will be deep-copied; the
        input is not mutated.
    prefer_project_outputs
        When True, missing layer-like output parameters are bound to
        ``TEMPORARY_OUTPUT`` (QGIS will materialize them on run).
    renaming_strategy
        One of ``"preserve"``, ``"suffix_counter"``, ``"label_slug"``.
    registry_lookup
        If True and QGIS is importable, ask the processing registry
        for the algorithm's input/output definitions. If False, only
        the built-in table is used (useful for headless / non-QGIS
        contexts).
    """
    result = copy.deepcopy(model_json or {})
    algorithms = result.get("algorithms", [])
    inputs = result.get("inputs", [])
    _apply_step_renaming(result, strategy=renaming_strategy)

    input_names = {
        str(inp.get("name", "")): str(inp.get("name", "")) for inp in inputs if inp.get("name")
    }
    normalized_input_map = {normalize_token(name): name for name in input_names}

    producers: list[str] = []

    for alg in algorithms:
        step_id = str(alg.get("id", "") or "")
        params = alg.setdefault("parameters", {})

        alg_id = str(alg.get("algorithm_id", "") or "")
        qgs_alg = _resolve_qgs_algorithm(alg_id) if registry_lookup else None
        expected = _expected_inputs(alg_id, qgs_alg=qgs_alg)

        for inp_name in expected:
            if inp_name in params:
                continue
            matched = normalized_input_map.get(normalize_token(inp_name))
            if matched:
                params[inp_name] = {"type": "model_input", "input_name": matched}
                continue
            if producers:
                prev_step_id = producers[-1]
                prev_alg = next(
                    (a for a in algorithms if a.get("id") == prev_step_id),
                    None,
                )
                output_name = "OUTPUT"
                if prev_alg:
                    prev_alg_id = str(prev_alg.get("algorithm_id", "") or "")
                    prev_qgs_alg = _resolve_qgs_algorithm(prev_alg_id) if registry_lookup else None
                    output_name = _preferred_output_name(prev_qgs_alg)
                params[inp_name] = {
                    "type": "child_output",
                    "child_id": prev_step_id,
                    "output_name": output_name,
                }

        if prefer_project_outputs:
            # Output parameter is conventionally the last entry in ``expected``
            # (heuristic) and named ``OUTPUT``/``RESULT``/etc.
            for candidate in ("OUTPUT", "RESULT", "OUTPUT_LAYER", "OUTPUT_VECTOR"):
                if candidate in params:
                    break
            else:
                candidate = expected[-1] if expected else "OUTPUT"
                if candidate not in params:
                    params[candidate] = {
                        "type": "static",
                        "value": "TEMPORARY_OUTPUT",
                    }

        if step_id:
            producers.append(step_id)

    return result
