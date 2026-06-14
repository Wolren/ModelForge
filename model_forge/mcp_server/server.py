"""FastMCP server exposing Model Forge's compiler pipeline + QGIS context as tools."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import threading
import time
from typing import Any

from . import _HAS_MCP, _HAS_QGIS, SCHEMA_VERSION, __version__
from .errors import (
    AlgorithmNotFoundError,
    ConfigError,
    InvalidJSONError,
    LayerNotFoundError,
    LLMNotConfiguredError,
    PipelineFailedError,
    QGISNotAvailableError,
    UnknownExportFormatError,
    ValidationFailedError,
    error_response,
    error_response_json,
)
from .llm_config import (
    DEFAULT_CATALOG_LIMIT,
    PROVIDERS,
    LLMConfig,
    build_llm_config,
    normalize_provider,
)
from .llm_config import (
    config_path as _config_path,
)
from .llm_config import (
    load_config as _load_config,
)
from .llm_config import (
    save_config as _save_config,
)

log = logging.getLogger(__name__)

# Try importing uvicorn for SSE server control
_HAS_UVICORN = False
try:
    import uvicorn

    _HAS_UVICORN = True
except ImportError:
    pass

# --- Lazy QGIS imports --------------------------------------------------

if _HAS_QGIS:
    from qgis.core import (
        QgsApplication,
        QgsProject,
    )


# --- Lazy compiler imports ----------------------------------------------


def _import_compiler():
    from model_forge.compiler_core.core.compiler.algorithm_resolver import (
        AlgorithmResolver,
    )
    from model_forge.compiler_core.core.compiler.expression_validator import (
        ExpressionValidator,
    )
    from model_forge.compiler_core.core.compiler.intent_parser import IntentParser
    from model_forge.compiler_core.core.compiler.ir_validator import IRValidator
    from model_forge.compiler_core.core.compiler.link_repair import LinkRepairService
    from model_forge.compiler_core.core.compiler.model_emitter import ModelEmitter
    from model_forge.compiler_core.core.compiler.pipeline import CompilerPipeline
    from model_forge.compiler_core.core.compiler.semantic_planner import (
        SemanticPlanner,
    )
    from model_forge.compiler_core.core.context_collector import (
        ContextCollector as CompilerContextCollector,
    )
    from model_forge.compiler_core.core.llm.factory import (
        create_backend as create_compiler_backend,
    )
    from model_forge.compiler_core.core.mcp.client import DirectMCPClient
    from model_forge.compiler_core.core.mcp.tool_registry import build_server
    from model_forge.compiler_core.core.services.layout.graph_layout import (
        GraphLayoutService,
    )
    from model_forge.compiler_core.core.services.registry.registry_catalog import (
        RegistryCatalogService,
    )

    return {
        "AlgorithmResolver": AlgorithmResolver,
        "ExpressionValidator": ExpressionValidator,
        "IntentParser": IntentParser,
        "IRValidator": IRValidator,
        "LinkRepairService": LinkRepairService,
        "ModelEmitter": ModelEmitter,
        "CompilerPipeline": CompilerPipeline,
        "SemanticPlanner": SemanticPlanner,
        "CompilerContextCollector": CompilerContextCollector,
        "create_compiler_backend": create_compiler_backend,
        "DirectMCPClient": DirectMCPClient,
        "build_server": build_server,
        "GraphLayoutService": GraphLayoutService,
        "RegistryCatalogService": RegistryCatalogService,
    }


def _import_exporter():
    from model_forge.compiler_core.core.services.script_exporter import (
        export_to_processing_script,
    )

    return export_to_processing_script


def _import_mermaid():
    from model_forge.compiler_core.core.services.mermaid_renderer import to_mermaid

    return to_mermaid


# --- Server state --------------------------------------------------------


class ServerState:
    """Thread-safe container for server configuration and cached QGIS context."""

    def __init__(self, llm_config: dict[str, Any] | None = None):
        self._lock = threading.Lock()
        self.llm_config = llm_config or {}
        self._context_snapshot: dict[str, Any] | None = None
        self._catalog_snapshot: dict[str, Any] | None = None

    @property
    def context(self) -> dict[str, Any]:
        with self._lock:
            if self._context_snapshot is None:
                self._context_snapshot = self._capture_qgis_context()
            return self._context_snapshot

    @property
    def catalog(self) -> dict[str, Any]:
        with self._lock:
            if self._catalog_snapshot is None:
                self._catalog_snapshot = self._capture_catalog()
            return self._catalog_snapshot

    def refresh(self):
        with self._lock:
            self._context_snapshot = self._capture_qgis_context()
            self._catalog_snapshot = self._capture_catalog()

    @staticmethod
    def _capture_qgis_context() -> dict[str, Any]:
        if not _HAS_QGIS:
            return {"layers": [], "project_crs": None, "canvas_extent": None}
        ctx: dict[str, Any] = {"layers": [], "project_crs": None, "canvas_extent": None}
        try:
            project = QgsProject.instance()
            ctx["project_crs"] = project.crs().authid() if project.crs().isValid() else None
            layers = []
            for layer in project.mapLayers().values():
                info = {
                    "id": layer.id(),
                    "name": layer.name(),
                    "type": layer.type().name
                    if hasattr(layer.type(), "name")
                    else str(layer.type()),
                    "crs": layer.crs().authid() if layer.crs().isValid() else None,
                    "feature_count": layer.featureCount()
                    if hasattr(layer, "featureCount")
                    else None,
                    "fields": [f.name() for f in layer.fields()]
                    if hasattr(layer, "fields")
                    else [],
                }
                try:
                    wkb = layer.wkbType() if hasattr(layer, "wkbType") else None
                    if wkb is not None:
                        info["geometry_type"] = str(wkb)
                except Exception:
                    pass
                layers.append(info)
            ctx["layers"] = layers
            # Prefer iface.mapCanvas() (the active GUI canvas). Fall back to
            # the standalone QGIS map canvas for headless / no-iface
            # contexts. ``activeMessageBar()`` is the QGIS *message bar*
            # UI element, not a canvas - calling ``.extent()`` on it
            # always returns ``None`` or errors out. This was the wrong
            # API call up through v1.0.x.
            canvas = None
            try:
                from qgis.utils import iface  # type: ignore

                if iface is not None:
                    canvas = iface.mapCanvas()
            except Exception:
                canvas = None
            if canvas is None:
                try:
                    canvas = QgsApplication.instance().mapCanvas()
                except Exception:
                    canvas = None
            if canvas is not None:
                try:
                    extent = canvas.extent()
                    if extent is not None and not extent.isNull():
                        try:
                            ctx["canvas_extent"] = extent.asWktPolygon()
                        except Exception:
                            ctx["canvas_extent"] = None
                except Exception:
                    ctx["canvas_extent"] = None
        except Exception:
            log.warning("Failed to capture QGIS context", exc_info=True)
        return ctx

    @staticmethod
    def _capture_catalog() -> dict[str, Any]:
        if not _HAS_QGIS:
            return {}
        try:
            from model_forge.compiler_core.core.services.registry.registry_catalog import (
                RegistryCatalogService,
            )

            svc = RegistryCatalogService()
            cap = int(os.environ.get("MODELFORGE_MCP_CATALOG_LIMIT", DEFAULT_CATALOG_LIMIT))
            return svc.get_algorithm_catalog(
                include_native=True,
                include_gdal=True,
                max_algorithms=cap,
            )
        except Exception:
            log.warning("Failed to capture algorithm catalog", exc_info=True)
            return {}


# --- Global server reference --------------------------------------------

_server_instance: FastMCP | None = None
_server_thread: threading.Thread | None = None
_server_uvicorn: Any | None = None  # uvicorn.Server instance for SSE
_server_state: ServerState | None = None
_server_port: int = 9090
_server_shutdown_timeout: float = 15.0

if _HAS_MCP:
    from mcp.server.fastmcp import FastMCP


def _get_state() -> ServerState:
    global _server_state
    if _server_state is None:
        _server_state = ServerState()
    return _server_state


# --- Tool helpers -------------------------------------------------------


def _model_json_from_str(model_json_str: str) -> dict:
    if isinstance(model_json_str, str):
        return json.loads(model_json_str)
    return model_json_str


def _validate_model_json(model: dict) -> list[str]:
    errors = []
    if "inputs" not in model:
        errors.append("Missing 'inputs' list")
    if "algorithms" not in model:
        errors.append("Missing 'algorithms' list")
    if "model_name" not in model:
        errors.append("Missing 'model_name'")
    if errors:
        return errors
    seen_ids = set()
    for alg in model.get("algorithms", []):
        aid = alg.get("id", "")
        if not aid:
            errors.append("Algorithm missing 'id'")
        elif aid in seen_ids:
            errors.append(f"Duplicate algorithm id: {aid}")
        seen_ids.add(aid)
        if not alg.get("algorithm_id"):
            errors.append(f"Algorithm '{aid}' missing 'algorithm_id'")
        for pname, pval in alg.get("parameters", {}).items():
            if isinstance(pval, dict) and pval.get("type") == "child_output":
                ref = pval.get("child_id")
                if ref and ref not in seen_ids and ref != aid:
                    errors.append(
                        f"Algorithm '{aid}' param '{pname}' references unknown child '{ref}'"
                    )
    return errors


# --- Tool: Context ------------------------------------------------------


def _register_context_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        description=(
            "List all layers currently loaded in the QGIS project. "
            "Returns an array of layer objects, each containing: id (QGIS layer ID), "
            "name (human-readable), type (vector/raster), crs (EPSG code), "
            "feature_count (for vector layers), geometry_type (point/polygon/line), "
            "fields (list of field name+type pairs). "
            "Use this FIRST before generate_model to see which layers are available "
            "for use as model inputs."
        )
    )
    async def list_layers() -> str:
        state = _get_state()
        return json.dumps(state.context.get("layers", []), indent=2, ensure_ascii=False)

    @mcp.tool(
        description=(
            "Get detailed properties of a specific QGIS project layer by ID or name. "
            "Returns fields, geometry type, CRS, feature count, and sample attributes. "
            "Use this when you need to know exact field names/types for model parameter "
            "binding (e.g., which field to use in a dissolve group_by or a filter expression)."
        )
    )
    async def get_layer_info(layer_identifier: str) -> str:
        state = _get_state()
        for layer in state.context.get("layers", []):
            if layer["id"] == layer_identifier or layer["name"] == layer_identifier:
                return json.dumps(layer, indent=2, ensure_ascii=False)
        return error_response_json(
            LayerNotFoundError(
                f"Layer '{layer_identifier}' not found",
                details={"layer_identifier": layer_identifier},
            )
        )

    @mcp.tool(
        description=(
            "Get the current QGIS project overview: CRS (EPSG code), total layer count, "
            "and layer names list. Useful as a lightweight alternative to list_layers "
            "when you only need the project's spatial reference context."
        )
    )
    async def get_project_info() -> str:
        state = _get_state()
        ctx = state.context
        info = {
            "project_crs": ctx.get("project_crs"),
            "layer_count": len(ctx.get("layers", [])),
            "layer_names": [layer["name"] for layer in ctx.get("layers", [])],
        }
        return json.dumps(info, indent=2, ensure_ascii=False)

    @mcp.tool(
        description=(
            "Force-refresh the cached QGIS state (layers, project info, algorithm catalog). "
            "Call this after adding/removing layers in QGIS or changing project settings "
            "so that subsequent list_layers/get_layer_info calls return current data. "
            "Returns layer count and catalog size after refresh."
        )
    )
    async def refresh_qgis_context() -> str:
        _get_state().refresh()
        ctx = _get_state().context
        from .subscriptions import get_subscription_registry

        get_subscription_registry().mark_resources_dirty(
            {
                "model-forge://server-info",
                "model-forge://context/layers",
                "model-forge://algorithms",
            }
        )
        return json.dumps(
            {
                "status": "ok",
                "layers": len(ctx.get("layers", [])),
                "catalog_size": len(_get_state().catalog),
            },
            indent=2,
        )

    @mcp.tool(
        description=(
            "Configure a headless (non-QGIS) project context. Pass a "
            "``layers_json`` string (a JSON array of layer dicts, with "
            "fields ``id``, ``name``, ``type``, ``crs``, ``feature_count``, "
            "and ``fields``) and an optional ``project_crs`` EPSG string. "
            "Subsequent ``list_layers`` / ``get_layer_info`` / "
            "``get_project_info`` calls return this snapshot. Pass "
            "``reset=True`` to clear the headless context. "
            "Used for CI, code review, and design sessions where no live "
            "QGIS project is available."
        )
    )
    async def configure_headless_context(
        layers_json: str = "[]",
        project_crs: str | None = None,
        reset: bool = False,
    ) -> str:
        if reset:
            _get_state()._context_snapshot = None
            from .subscriptions import get_subscription_registry

            get_subscription_registry().mark_resource_dirty("model-forge://context/layers")
            return json.dumps(
                {"status": "ok", "reset": True, "layers": 0},
                indent=2,
                ensure_ascii=False,
            )
        try:
            layers = json.loads(layers_json) if layers_json else []
        except json.JSONDecodeError as e:
            return error_response_json(
                InvalidJSONError(
                    f"layers_json is not valid JSON: {e}",
                    details={"line": e.lineno, "column": e.colno},
                )
            )
        if not isinstance(layers, list):
            return error_response_json(
                ConfigError(
                    "layers_json must be a JSON array of layer objects.",
                    details={"value_type": type(layers).__name__},
                )
            )
        snapshot = {
            "layers": layers,
            "project_crs": project_crs,
            "canvas_extent": None,
        }
        _get_state()._context_snapshot = snapshot
        from .subscriptions import get_subscription_registry

        get_subscription_registry().mark_resource_dirty("model-forge://context/layers")
        return json.dumps(
            {"status": "ok", "layers": len(layers), "project_crs": project_crs},
            indent=2,
            ensure_ascii=False,
        )


# --- Tool: Algorithms ---------------------------------------------------


_FUZZY_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "by",
        "and",
        "or",
        "with",
        "from",
        "as",
        "is",
        "it",
        "be",
    }
)


def _fuzzy_score(query: str, alg_id: str, name: str) -> float:
    """Token-overlap similarity between ``query`` and an algorithm.

    Returns a float in ``[0.0, 1.0+]`` - values above 1.0 are
    possible when *every* query token matches and the candidate has
    no extra tokens (perfect overlap bonus).
    """
    q_tokens = {tok for tok in query.lower().split() if tok and tok not in _FUZZY_STOPWORDS}
    if not q_tokens:
        return 0.0
    cand_tokens = set()
    for source in (alg_id.lower(), name.lower()):
        for tok in source.replace(":", " ").replace("_", " ").split():
            if tok and tok not in _FUZZY_STOPWORDS:
                cand_tokens.add(tok)
    if not cand_tokens:
        return 0.0
    hits = len(q_tokens & cand_tokens)
    return hits / len(q_tokens)


def _register_algorithm_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        description=(
            "Search available QGIS Processing algorithms by name, ID, or provider prefix. "
            "Returns algorithm IDs, display names, groups, and parameter counts. "
            "Use with query='native:buffer' or query='buffer' or provider='native'. "
            "Essential before generate_model - provides the algorithm IDs the LLM planner "
            "needs to construct valid step definitions. Set max_results up to 200."
        )
    )
    async def list_algorithms(
        query: str | None = None,
        provider: str | None = None,
        max_results: int = 50,
        group: str | None = None,
        fuzzy: bool = False,
    ) -> str:
        """List algorithms in the catalog.

        ``query`` is matched against algorithm id and display name.
        When ``fuzzy=True``, results are ranked by token-overlap
        similarity (stopwords ignored); when ``fuzzy=False`` (the
        default), ``query`` does plain substring matching.

        ``provider`` and ``group`` are exact-match filters on the
        algorithm's provider prefix (e.g. ``native``) and group
        string (e.g. ``Vector geometry``). ``max_results`` caps the
        number of returned rows.
        """
        state = _get_state()
        catalog = state.catalog
        candidates: list[tuple[float, dict]] = []
        for alg_id, entry in catalog.items():
            if provider and not alg_id.startswith(provider + ":"):
                continue
            if group and entry.get("group", "") != group:
                continue
            score = 0.0
            if query:
                if fuzzy:
                    score = _fuzzy_score(query, alg_id, entry.get("name", ""))
                else:
                    q = query.lower()
                    if q in alg_id.lower() or q in (entry.get("name", "")).lower():
                        score = 1.0
                    else:
                        continue
            candidates.append(
                (
                    score,
                    {
                        "id": alg_id,
                        "name": entry.get("name", alg_id),
                        "group": entry.get("group", ""),
                        "param_count": len(entry.get("parameters", [])),
                    },
                )
            )
        if fuzzy and query:
            candidates.sort(key=lambda pair: pair[0], reverse=True)
        elif not query:
            # Without a query, keep catalog order (which mirrors the
            # underlying QGIS registry's enumeration).
            candidates.sort(key=lambda pair: pair[1]["id"])
        results = [item for _, item in candidates[:max_results]]
        return json.dumps(results, indent=2, ensure_ascii=False)

    @mcp.tool(
        description=(
            "Enumerate the algorithm providers present in the current "
            "catalog. Returns ``[{id, algorithm_count, groups}]`` "
            "where ``id`` is the provider prefix (e.g. ``native``, "
            "``gdal``, ``grass7``), ``algorithm_count`` is the number "
            "of algorithms from that provider, and ``groups`` is the "
            "distinct set of group names that provider exposes."
        )
    )
    async def list_providers() -> str:
        state = _get_state()
        catalog = state.catalog
        by_provider: dict[str, dict] = {}
        for alg_id, entry in catalog.items():
            provider_id = alg_id.split(":", 1)[0] if ":" in alg_id else "(no-provider)"
            bucket = by_provider.setdefault(
                provider_id,
                {"id": provider_id, "algorithm_count": 0, "groups": set()},
            )
            bucket["algorithm_count"] += 1
            grp = entry.get("group")
            if grp:
                bucket["groups"].add(grp)
        providers = [
            {
                "id": b["id"],
                "algorithm_count": b["algorithm_count"],
                "groups": sorted(b["groups"]),
            }
            for b in by_provider.values()
        ]
        providers.sort(key=lambda p: p["id"])
        return json.dumps(providers, indent=2, ensure_ascii=False)

    @mcp.tool(
        description=(
            "Enumerate the algorithm groups (categories) present in "
            "the catalog. Returns ``[{provider, group, algorithm_count}]`` "
            "sorted by (provider, group). Useful for navigating the "
            "catalog without knowing the exact provider prefix."
        )
    )
    async def list_algorithm_groups(provider: str | None = None) -> str:
        state = _get_state()
        catalog = state.catalog
        buckets: dict[tuple[str, str], int] = {}
        for alg_id, entry in catalog.items():
            pid = alg_id.split(":", 1)[0] if ":" in alg_id else "(no-provider)"
            if provider and pid != provider:
                continue
            grp = entry.get("group") or "(no-group)"
            buckets[(pid, grp)] = buckets.get((pid, grp), 0) + 1
        out = [
            {"provider": pid, "group": grp, "algorithm_count": n}
            for (pid, grp), n in sorted(buckets.items())
        ]
        return json.dumps(out, indent=2, ensure_ascii=False)

    @mcp.tool(
        description=(
            "Get full parameter and output documentation for a QGIS Processing algorithm. "
            "Returns parameter names, types, descriptions, optional flags, enum options, "
            "and output definitions. Use this when you need to construct correct parameter "
            "bindings in a model - e.g., to know that native:buffer has 'INPUT', 'DISTANCE', "
            "'SEGMENTS', 'END_CAP_STYLE', 'JOIN_STYLE', 'MITER_LIMIT', 'DISSOLVE', 'OUTPUT'."
        )
    )
    async def get_algorithm_info(algorithm_id: str) -> str:
        state = _get_state()
        catalog = state.catalog
        entry = catalog.get(algorithm_id)
        if not entry:
            for aid, e in catalog.items():
                if aid.split(":")[-1].lower() == algorithm_id.rsplit(":", maxsplit=1)[-1].lower():
                    entry = e
                    algorithm_id = aid
                    break
        if not entry:
            return error_response_json(
                AlgorithmNotFoundError(
                    f"Algorithm '{algorithm_id}' not found in catalog",
                    details={"algorithm_id": algorithm_id},
                )
            )
        return json.dumps(
            {
                "id": algorithm_id,
                "name": entry.get("name"),
                "group": entry.get("group"),
                "parameters": entry.get("parameters"),
                "outputs": entry.get("outputs"),
            },
            indent=2,
            ensure_ascii=False,
        )

    @mcp.tool(
        description=(
            "Load a pre-snapshotted algorithm catalog from a JSON file on "
            "disk. Useful for headless / non-QGIS mode where the live "
            "QGIS processing registry is not available. The file should "
            "contain a JSON object ``{algorithm_id: {name, group, "
            "parameters, outputs}}``. Subsequent ``list_algorithms`` and "
            "``get_algorithm_info`` calls read from this catalog until "
            "``refresh_qgis_context`` replaces it."
        )
    )
    async def load_catalog_from_file(path: str) -> str:
        import os

        if not os.path.isfile(path):
            return error_response_json(
                ConfigError(
                    f"Catalog file not found: {path}",
                    details={"path": path},
                )
            )
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            return error_response_json(
                InvalidJSONError(
                    f"Catalog file is not valid JSON: {e}",
                    details={"path": path, "line": e.lineno, "column": e.colno},
                )
            )
        except OSError as e:
            return error_response_json(
                ConfigError(
                    f"Failed to read catalog file: {e}",
                    details={"path": path},
                )
            )
        if not isinstance(data, dict):
            return error_response_json(
                ConfigError(
                    "Catalog file must be a JSON object mapping algorithm_id → entry.",
                    details={"value_type": type(data).__name__},
                )
            )
        state = _get_state()
        state._catalog_snapshot = data
        from .subscriptions import get_subscription_registry

        get_subscription_registry().mark_resources_dirty(
            {"model-forge://server-info", "model-forge://algorithms"}
        )
        return json.dumps(
            {"status": "ok", "loaded": len(data), "path": path},
            indent=2,
            ensure_ascii=False,
        )

    @mcp.tool(
        description=(
            "Export the current algorithm catalog (live or loaded) to a "
            "JSON file. The resulting file can be reloaded with "
            "``load_catalog_from_file`` for headless deployments. "
            "If ``path`` is omitted, the catalog is returned inline."
        )
    )
    async def export_catalog(path: str | None = None) -> str:
        state = _get_state()
        catalog = state.catalog
        if not catalog:
            return error_response_json(
                ConfigError(
                    "Catalog is empty. Refresh QGIS context or load_catalog_from_file first.",
                    details={},
                )
            )
        if path is None:
            return json.dumps(catalog, indent=2, ensure_ascii=False)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(catalog, f, indent=2, ensure_ascii=False, sort_keys=True)
        except OSError as e:
            return error_response_json(
                ConfigError(
                    f"Failed to write catalog to {path}: {e}",
                    details={"path": path},
                )
            )
        return json.dumps(
            {"status": "ok", "written": len(catalog), "path": path},
            indent=2,
            ensure_ascii=False,
        )


# --- Export-format helpers (Phase 7) -----------------------------------


def _model_output_schema(model: dict) -> dict[str, list[tuple[str, int]]]:
    """Best-effort: produce a ``{layer_name: [(field_name, ogr_type)]}``
    schema for a model's output steps.

    Layers are named after the *step id*; fields are read from the
    upstream input's field list (the LLM's emission of fields is
    unreliable, so we just snapshot the input schema).
    """
    schema: dict[str, list[tuple[str, int]]] = {}
    step_field_lists = {
        inp.get("name", ""): [
            (f.get("name", f"field_{i}"), 4)  # 4 = ogr.OFTString default
            for i, f in enumerate(inp.get("fields", []) or [])
        ]
        for inp in model.get("inputs", [])
    }
    for alg in model.get("algorithms", []):
        step_id = alg.get("id", "")
        if not step_id:
            continue
        # Heuristic: any input param that points to a model_input is the
        # schema source. We take the first one and use its fields.
        for pname, pval in (alg.get("parameters") or {}).items():
            if not isinstance(pval, dict):
                continue
            if pval.get("type") == "model_input":
                src = pval.get("input_name", "")
                fields = step_field_lists.get(src, [])
                if fields:
                    schema[step_id] = fields
                    break
    return schema


def _model_to_geojson_contract(model: dict) -> dict:
    """Build a placeholder GeoJSON FeatureCollection describing the
    model's output schema. No geometries - this is a contract artifact,
    not actual data.
    """
    schema = _model_output_schema(model)
    features = []
    for layer_name, fields in schema.items():
        properties: dict = {"_model_forge_layer": layer_name}
        for fld_name, _ in fields:
            properties[fld_name] = None
        features.append(
            {
                "type": "Feature",
                "geometry": None,
                "properties": properties,
            }
        )
    return {
        "type": "FeatureCollection",
        "features": features,
        "_model_forge_contract": {
            "model_name": model.get("model_name", ""),
            "model_group": model.get("model_group", ""),
            "layer_schemas": {
                layer: [{"name": fn, "type": "string"} for fn, _ in fields]
                for layer, fields in schema.items()
            },
        },
    }


def _wrap_script_as_runnable(model: dict, raw_script: str) -> str:
    """Wrap a processing.run() script in an ``argparse`` __main__ so
    the user can invoke it as ``python script.py --param value``.
    """
    inputs = model.get("inputs", [])
    arg_lines = []
    for inp in inputs:
        name = inp.get("name", "input")
        kind = inp.get("type", "string")
        flag = "--" + name.replace("_", "-")
        py_type = {
            "number": "float",
            "boolean": "bool",
            "field": "str",
            "expression": "str",
            "crs": "str",
            "extent": "str",
        }.get(kind, "str")
        arg_lines.append(f"    parser.add_argument({flag!r}, type={py_type}, required=True)")
    args_block = "\n".join(arg_lines) if arg_lines else "    pass"
    return (
        f'#!/usr/bin/env python\n"""\nAuto-generated by Model Forge MCP server.\n'
        f"Model: {model.get('model_name', 'workflow')}\nGroup: {model.get('model_group', 'ModelForge')}\n"
        f'"""\nimport argparse\n\n\ndef main():\n    parser = argparse.ArgumentParser()\n{args_block}\n'
        f'    args = parser.parse_args()\n\n\nif __name__ == "__main__":\n    main()\n\n\n'
        f"# ---- processing script body (exported by Model Forge) ----\n"
        f"{raw_script}\n"
    )


def _model_to_qgis_process_recipe(model: dict) -> dict:
    """Produce a JSON recipe runnable by ``qgis_process --json``.

    Each step becomes an entry with the algorithm_id and the
    parameter bindings as plain Python literals.
    """
    steps: list[dict] = []
    for alg in model.get("algorithms", []):
        step = {
            "algorithm_id": alg.get("algorithm_id", ""),
            "step_id": alg.get("id", ""),
            "parameters": {},
        }
        for pname, pval in (alg.get("parameters") or {}).items():
            if isinstance(pval, dict):
                btype = pval.get("type", "static")
                if btype == "static":
                    step["parameters"][pname] = pval.get("value")
                elif btype == "model_input":
                    step["parameters"][pname] = f"<model_input:{pval.get('input_name', '')}>"
                elif btype == "child_output":
                    step["parameters"][pname] = (
                        f"<child_output:{pval.get('child_id', '')}:"
                        f"{pval.get('output_name', 'OUTPUT')}>"
                    )
                else:
                    step["parameters"][pname] = pval.get("value")
            else:
                step["parameters"][pname] = pval
        steps.append(step)
    return {
        "model_name": model.get("model_name", "workflow"),
        "model_group": model.get("model_group", "ModelForge"),
        "inputs": [
            {
                "name": inp.get("name", ""),
                "type": inp.get("type", "string"),
                "label": inp.get("label", ""),
            }
            for inp in model.get("inputs", [])
        ],
        "algorithms": steps,
    }


def _guess_geometry_kind(alg: dict[str, Any]) -> str:
    """Best-effort guess of the geometry kind for a step.

    We read the algorithm_id - if it carries a known hint
    (e.g. ``native:buffer`` produces the same kind as its input;
    ``native:centroids`` produces points) we use that. Otherwise
    we look at the upstream ``model_input`` and the user's
    ``_mf_layer_geometry_kind`` hint. Falls back to ``polygon``
    because the LLM-emitted model JSON rarely carries geometry
    metadata and polygon is the conservative default.
    """
    alg_id = str(alg.get("algorithm_id", "") or "").lower()
    # Algorithm id hints.
    HINTS = {
        "native:centroids": "point",
        "native:pointstolines": "line",
        "native:linestopolygons": "polygon",
        "native:pointstopolygons": "polygon",
        "native:rasterize": "raster",
        "gdal:warpreproject": "raster",
        "gdal:translate": "raster",
    }
    if alg_id in HINTS:
        return HINTS[alg_id]
    # Suffix-based heuristic.
    for suffix, kind in (
        ("polygon", "polygon"),
        ("line", "line"),
        ("point", "point"),
        ("raster", "raster"),
    ):
        if alg_id.endswith(":" + suffix) or suffix in alg_id:
            return kind
    return "polygon"


# --- Tool: Generation --------------------------------------------------


def _make_progress_callback(base: Any | None, cancel_event: threading.Event | None):
    """Bridge the pipeline's progress callback to the job registry.

    The compiler pipeline now calls ``progress_callback`` with
    ``(current, total, message)`` (floats + string). Inner-MCP tool
    callbacks that still emit a bare string are accepted as a
    fallback. The bridge normalizes both shapes and forwards to
    ``base`` (which the job registry will hand to the MCP
    ``report_progress`` slot).

    The cancel event is checked *first* on every call so a user
    cancellation interrupts the long LLM round-trip promptly.
    """
    from .jobs import CancelledError

    def _wrapped(*args) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError(
                "Job cancelled during pipeline stage",
                details={"stage": str(args) if args else None},
            )
        if base is None:
            return
        try:
            if len(args) == 1 and isinstance(args[0], str):
                # Legacy single-string signature. We don't know the
                # current/total so emit (0, 1, message) which clients
                # render as a spinner with text.
                current, total, message = 0.0, 1.0, args[0]
            elif len(args) == 3:
                current, total, message = args
            else:
                return
            base(float(current), float(total), str(message))
        except CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.debug("progress forward failed", exc_info=True)

    return _wrapped


def _run_pipeline(
    description: str,
    model_name: str,
    model_group: str,
    qgis_context: dict[str, Any],
    llm_config: dict[str, Any],
    algorithm_ids: list[str] | None = None,
    algorithm_groups: list[str] | None = None,
    provider_ids: list[str] | None = None,
    max_algorithms: int = 100,
    progress_callback: Any | None = None,
) -> tuple[Any, dict]:
    comp = _import_compiler()
    llm = comp["create_compiler_backend"](llm_config)

    ctx = comp["CompilerContextCollector"]().collect(
        algorithm_ids=algorithm_ids,
        max_algorithms=max_algorithms,
        provider_ids=provider_ids or ["native", "gdal"],
        algorithm_groups=algorithm_groups,
    )

    if qgis_context.get("layers"):
        ctx["layers"] = qgis_context["layers"]
    if qgis_context.get("project_crs"):
        ctx["project_crs"] = qgis_context["project_crs"]

    server = comp["build_server"](llm)
    client = comp["DirectMCPClient"](server)

    pipeline = comp["CompilerPipeline"](
        intent_parser=comp["IntentParser"](),
        semantic_planner=comp["SemanticPlanner"](),
        algorithm_resolver=comp["AlgorithmResolver"](),
        expression_validator=comp["ExpressionValidator"](),
        ir_validator=comp["IRValidator"](),
        model_emitter=comp["ModelEmitter"](),
        link_repair=comp["LinkRepairService"](),
    )

    plan, model_json = pipeline.run(
        raw_text=description,
        model_name=model_name,
        model_group=model_group,
        qgis_context=ctx,
        mcp_client=client,
        progress_callback=progress_callback,
    )

    # Auto-wire missing parameter bindings *after* the LLM emits the model.
    # The compiler is allowed to leave inputs unbound; this pass plugs the
    # gaps with model_input / child_output references and ensures every
    # step has a destination parameter. GUI clients do this inside
    # ``ModelBuilderBridge.load_model_json``; the MCP server does it in
    # its own pure-Python service so headless callers get the same
    # guarantee.
    try:
        from model_forge.compiler_core.core.services.auto_wire import (
            auto_wire_model_json,
        )

        model_json = auto_wire_model_json(
            model_json,
            prefer_project_outputs=True,
            renaming_strategy="preserve",
            registry_lookup=_HAS_QGIS,
        )
    except Exception:
        log.warning("Auto-wire pass failed", exc_info=True)

    try:
        model_json = comp["GraphLayoutService"]().layout_model_json(model_json)
    except Exception:
        log.warning("Layout step failed", exc_info=True)

    return plan, model_json


def _register_generation_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        description=(
            "Generate a complete QGIS Processing model from a natural language description. "
            "Returns JSON with inputs list and algorithms list (each with parameters, "
            "algorithm_id, etc.) compatible with QGIS Processing model format. "
            "Optional ``progress_token`` (opaque string the client passes in) "
            "enables streaming progress notifications; ``timeout_seconds`` aborts "
            "the generation if it hasn't returned in time. Pair with "
            "``cancel_generation(job_id)`` for mid-flight cancellation."
        )
    )
    async def generate_model(
        description: str,
        model_name: str = "Model Forge Workflow",
        model_group: str = "ModelForge",
        layer_ids: list[str] | None = None,
        algorithm_ids: list[str] | None = None,
        algorithm_groups: list[str] | None = None,
        provider_ids: list[str] | None = None,
        max_algorithms: int = 100,
        progress_token: str | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        state = _get_state()
        try:
            llm = LLMConfig.from_dict(state.llm_config)
            if not llm.provider:
                raise LLMNotConfiguredError(
                    "LLM not configured. Set provider/model via set_llm_config tool."
                )
            llm.validate()
        except ConfigError as e:
            return error_response_json(e)
        except LLMNotConfiguredError as e:
            return error_response_json(e)

        ctx = state.context
        if layer_ids:
            wanted = set(layer_ids)
            ctx["layers"] = [layer for layer in ctx.get("layers", []) if layer["id"] in wanted]

        # Hook progress up to MCP's ``report_progress`` if we have a
        # progress token. ``mcp.get_context()`` is the FastMCP context
        # object; in stdio we always have it, in SSE we have it for the
        # duration of the request.
        def _on_progress(current: float, total: float, message: str) -> None:
            if not progress_token:
                return
            try:
                mcp_ctx = mcp.get_context()  # type: ignore[attr-defined]
                mcp_ctx.request_context.report_progress(progress_token, current, total, message)
            except Exception:  # noqa: BLE001
                # The client may not support progress; never let a
                # notification failure abort the generation.
                log.debug("report_progress failed", exc_info=True)

        # Submit the (potentially long) pipeline to the job registry
        # and await its result off the MCP event loop. The registry
        # owns the timeout / cancellation semantics; we pre-create a
        # job record so we can hand its ``job_id`` back to the client
        # (and so the client can later call ``cancel_generation``
        # using that id).
        from .jobs import (
            CancelledError as JobCancelled,
            MfTimeoutError,
            get_registry,
        )

        registry = get_registry()
        from .jobs import Job

        job = Job(job_id=__import__("uuid").uuid4().hex)
        with registry._lock:  # noqa: SLF001 - internal but stable enough
            registry._jobs[job.job_id] = job  # noqa: SLF001

        worker_args = dict(
            description=description,
            model_name=model_name,
            model_group=model_group,
            qgis_context=ctx,
            llm_config=llm.to_dict(),
            algorithm_ids=algorithm_ids,
            algorithm_groups=algorithm_groups,
            provider_ids=provider_ids,
            max_algorithms=max_algorithms,
        )

        cancel_event = job.cancel_event

        def _worker(progress_callback):
            # ``progress_callback`` is the registry's wrapper which calls
            # the user's ``on_progress`` with ``(current, total, message)``.
            # The compiler pipeline invokes it with a *string*; we
            # convert to a tuple here and also poll the cancel event
            # between stages.
            from .jobs import check_cancellation

            check_cancellation(cancel_event)

            # Bridge: pipeline → (str) → registry's (current, total, message).
            # We use the same stage mapping the server's
            # ``_make_progress_callback`` uses, so the (current, total)
            # we emit is identical to what a non-job-registry caller
            # would see.
            pipeline_cb = _make_progress_callback(progress_callback, cancel_event)

            result = _run_pipeline(progress_callback=pipeline_cb, **worker_args)
            check_cancellation(cancel_event)
            return result

        import asyncio

        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            None,
            lambda: registry.run(
                _worker,
                job_id=job.job_id,
                on_progress=_on_progress,
                timeout=timeout_seconds,
            ),
        )

        try:
            plan, model_json = await future
        except JobCancelled as e:
            e.details.setdefault("job_id", job.job_id)
            return error_response_json(e)
        except MfTimeoutError as e:
            e.details.setdefault("job_id", job.job_id)
            return error_response_json(e)
        except asyncio.CancelledError:
            # The MCP runtime cancelled the coroutine; set the cancel
            # event so the worker can short-circuit on its next poll.
            job.cancel_event.set()
            raise
        except Exception as e:  # noqa: BLE001
            log.error("Pipeline failed", exc_info=True)
            err = PipelineFailedError(str(e))
            err.details.setdefault("job_id", job.job_id)
            return error_response_json(err)

        result = {
            "model": model_json,
            "summary": {
                "steps": len(model_json.get("algorithms", [])),
                "inputs": len(model_json.get("inputs", [])),
                "issues": [
                    {"level": str(i.level), "message": i.message, "step": i.step_id}
                    for i in plan.issues
                ],
            },
            "job_id": job.job_id,
            "progress": {
                "current": 1.0,
                "total": 1.0,
                "message": "completed",
            },
        }
        return json.dumps(result, indent=2, ensure_ascii=False)

    @mcp.tool(
        description=(
            "Validate a model JSON for structural correctness. Checks for: missing required fields "
            "(inputs, algorithms, model_name), duplicate step IDs, missing algorithm_ids, "
            "and references to unknown child steps in parameter bindings. "
            "Returns {'valid': bool, 'issues': [error_strings]}."
        )
    )
    async def validate_model(model_json_str: str) -> str:
        try:
            model = _model_json_from_str(model_json_str)
        except json.JSONDecodeError as e:
            return json.dumps(
                {
                    "valid": False,
                    **error_response(
                        InvalidJSONError(
                            f"Invalid JSON: {e}",
                            details={"line": e.lineno, "column": e.colno},
                        )
                    ),
                },
                indent=2,
                ensure_ascii=False,
            )
        errors = _validate_model_json(model)
        if errors:
            return json.dumps(
                {
                    "valid": False,
                    **error_response(
                        ValidationFailedError(
                            "Model validation failed",
                            details={"issues": errors},
                        )
                    ),
                },
                indent=2,
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "valid": True,
                "issues": [],
            },
            indent=2,
            ensure_ascii=False,
        )

    @mcp.tool(
        description=(
            "Export a model JSON to QGIS .model3 file, Python Processing script, "
            "or Mermaid diagram chart. Returns JSON with 'path' and 'content' fields. "
            "Available formats: 'json' (pretty-printed JSON), 'mermaid' (flowchart diagram), "
            "'script' (standalone Python Processing script), 'model3' (QGIS model XML, requires QGIS)."
        )
    )
    async def export_model(
        model_json_str: str,
        export_format: str = "json",
        output_path: str | None = None,
    ) -> str:
        try:
            model = _model_json_from_str(model_json_str)
        except json.JSONDecodeError as e:
            return error_response_json(
                InvalidJSONError(
                    f"Invalid JSON: {e}",
                    details={"line": e.lineno, "column": e.colno},
                )
            )

        export_format = export_format.lower()

        if export_format == "json":
            # If output_path is provided, also write a pretty-printed copy.
            if output_path:
                try:
                    with open(output_path, "w", encoding="utf-8") as f:
                        json.dump(model, f, indent=2, ensure_ascii=False)
                except OSError as e:
                    return error_response_json(
                        ConfigError(
                            f"Failed to write JSON to {output_path}: {e}",
                            details={"output_path": output_path},
                        )
                    )
            return json.dumps(model, indent=2, ensure_ascii=False)

        if export_format == "mermaid":
            try:
                to_mermaid = _import_mermaid()
                return to_mermaid(model)
            except Exception as e:  # noqa: BLE001
                return error_response_json(
                    ConfigError(
                        f"Mermaid render failed: {e}",
                        details={"exception_type": type(e).__name__},
                    )
                )

        if export_format == "script":
            try:
                export_to_processing_script = _import_exporter()
                if output_path:
                    path = export_to_processing_script(model, output_path)
                    with open(path, encoding="utf-8") as f:
                        content = f.read()
                    return json.dumps(
                        {"path": path, "content": content},
                        indent=2,
                        ensure_ascii=False,
                    )
                # No output_path: emit content only, no tempfile.
                import tempfile

                with tempfile.NamedTemporaryFile(
                    suffix=".py", prefix="mf_script_", mode="w", delete=False, encoding="utf-8"
                ) as tmp:
                    tmp_path = tmp.name
                try:
                    path = export_to_processing_script(model, tmp_path)
                    with open(path, encoding="utf-8") as f:
                        content = f.read()
                finally:
                    import os as _os
                    import contextlib

                    with contextlib.suppress(OSError):
                        _os.unlink(tmp_path)
                return content
            except Exception as e:  # noqa: BLE001
                return error_response_json(
                    ConfigError(
                        f"Script export failed: {e}",
                        details={"exception_type": type(e).__name__},
                    )
                )

        if export_format == "model3":
            if not _HAS_QGIS:
                return error_response_json(
                    QGISNotAvailableError(
                        ".model3 export requires QGIS. Use 'script' or 'json' format."
                    )
                )
            if not output_path:
                import tempfile

                output_path = tempfile.mktemp(suffix=".model3", prefix="mf_model_")
            try:
                bridge_path = "model_forge.compiler_core.ui.model_builder_bridge.ModelBuilderBridge"
                from importlib import import_module

                mod_path, cls_name = bridge_path.rsplit(".", 1)
                bridge_cls = getattr(import_module(mod_path), cls_name)
                bridge = bridge_cls()
                qgs_model = bridge.load_model_json(model, open_designer=False)

                if qgs_model.toFile(output_path):
                    with open(output_path, encoding="utf-8") as f:
                        content = f.read()
                    return json.dumps(
                        {"path": output_path, "content": content},
                        indent=2,
                        ensure_ascii=False,
                    )
                return error_response_json(
                    ConfigError(
                        "toFile failed",
                        details={"output_path": output_path},
                    )
                )
            except Exception as e:  # noqa: BLE001
                return error_response_json(
                    ConfigError(
                        f".model3 export failed: {e}",
                        details={"exception_type": type(e).__name__},
                    )
                )

        if export_format == "geojson":
            # Emit a placeholder FeatureCollection that documents the
            # model's output layer schema (field names + types). Useful
            # as a contract artifact the user can hand to a consumer.
            feature_collection = _model_to_geojson_contract(model)
            if output_path:
                try:
                    with open(output_path, "w", encoding="utf-8") as f:
                        json.dump(feature_collection, f, indent=2, ensure_ascii=False)
                except OSError as e:
                    return error_response_json(
                        ConfigError(
                            f"Failed to write GeoJSON to {output_path}: {e}",
                            details={"output_path": output_path},
                        )
                    )
                return json.dumps(
                    {"path": output_path, "feature_count": len(feature_collection["features"])},
                    indent=2,
                    ensure_ascii=False,
                )
            return json.dumps(feature_collection, indent=2, ensure_ascii=False)

        if export_format == "gpkg":
            if not output_path:
                return error_response_json(
                    ConfigError(
                        "gpkg export requires output_path.",
                        details={"format": export_format},
                    )
                )
            try:
                from osgeo import ogr, osr  # type: ignore
            except ImportError:
                return error_response_json(
                    ConfigError(
                        "GeoPackage export requires the 'osgeo' (GDAL Python) bindings.",
                        details={"hint": "Install with: pip install GDAL==<your QGIS version>"},
                    )
                )
            try:
                schema = _model_output_schema(model)
                if not schema:
                    return error_response_json(
                        ConfigError(
                            "Model has no output schema; cannot write GeoPackage.",
                            details={"model_name": model.get("model_name")},
                        )
                    )
                driver = ogr.GetDriverByName("GPKG")
                if driver is None:
                    return error_response_json(
                        ConfigError(
                            "GPKG driver not available in this GDAL build.",
                            details={},
                        )
                    )
                import os as _os

                if _os.path.exists(output_path):
                    driver.DeleteDataSource(output_path)
                ds = driver.CreateDataSource(output_path)
                if ds is None:
                    return error_response_json(
                        ConfigError(
                            f"Failed to create GeoPackage at {output_path}.",
                            details={"output_path": output_path},
                        )
                    )
                for layer_name, fields in schema.items():
                    srs = osr.SpatialReference()
                    srs.ImportFromEPSG(4326)
                    lyr = ds.CreateLayer(layer_name, srs=srs, geom_type=ogr.wkbUnknown)
                    for fld_name, fld_type in fields:
                        fld_defn = ogr.FieldDefn(fld_name, fld_type)
                        lyr.CreateField(fld_defn)
                ds = None
                return json.dumps(
                    {"path": output_path, "layers": list(schema.keys())},
                    indent=2,
                    ensure_ascii=False,
                )
            except Exception as e:  # noqa: BLE001
                return error_response_json(
                    ConfigError(
                        f"GeoPackage export failed: {e}",
                        details={"exception_type": type(e).__name__},
                    )
                )

        if export_format == "runnable_script":
            try:
                export_to_processing_script = _import_exporter()
                import tempfile as _tempfile

                with _tempfile.NamedTemporaryFile(
                    suffix=".py", prefix="mf_raw_", mode="w", delete=False, encoding="utf-8"
                ) as tmp:
                    raw_path = tmp.name
                try:
                    export_to_processing_script(model, raw_path)
                    with open(raw_path, encoding="utf-8") as f:
                        raw_body = f.read()
                finally:
                    import os as _os
                    import contextlib

                    with contextlib.suppress(OSError):
                        _os.unlink(raw_path)
                wrapped = _wrap_script_as_runnable(model, raw_body)
                if output_path:
                    with open(output_path, "w", encoding="utf-8") as f:
                        f.write(wrapped)
                    return json.dumps(
                        {"path": output_path, "content": wrapped},
                        indent=2,
                        ensure_ascii=False,
                    )
                return wrapped
            except Exception as e:  # noqa: BLE001
                return error_response_json(
                    ConfigError(
                        f"runnable_script export failed: {e}",
                        details={"exception_type": type(e).__name__},
                    )
                )

        if export_format == "processing_runnable_json":
            recipe = _model_to_qgis_process_recipe(model)
            if output_path:
                try:
                    with open(output_path, "w", encoding="utf-8") as f:
                        json.dump(recipe, f, indent=2, ensure_ascii=False)
                except OSError as e:
                    return error_response_json(
                        ConfigError(
                            f"Failed to write recipe to {output_path}: {e}",
                            details={"output_path": output_path},
                        )
                    )
                return json.dumps(
                    {"path": output_path, "algorithms": len(recipe["algorithms"])},
                    indent=2,
                    ensure_ascii=False,
                )
            return json.dumps(recipe, indent=2, ensure_ascii=False)

        return error_response_json(
            UnknownExportFormatError(
                f"Unknown export format '{export_format}'. "
                f"Use one of: json, mermaid, script, model3, geojson, gpkg, "
                f"runnable_script, processing_runnable_json.",
                details={"format": export_format},
            )
        )

    @mcp.tool(
        description=(
            "Get a human-readable text summary of a model JSON: model name, group, "
            "number of inputs with types, number of steps with algorithm IDs, "
            "and any plan issues. Useful for quick model inspection without parsing raw JSON."
        )
    )
    async def summarize_model(model_json_str: str) -> str:
        try:
            model = _model_json_from_str(model_json_str)
        except json.JSONDecodeError as e:
            return error_response_json(
                InvalidJSONError(
                    f"Invalid JSON: {e}",
                    details={"line": e.lineno, "column": e.colno},
                )
            )

        lines = [f"Model: {model.get('model_name', 'unnamed')}"]
        lines.append(f"Group: {model.get('model_group', 'default')}")
        inputs = model.get("inputs", [])
        lines.append(f"Inputs ({len(inputs)}):")
        for inp in inputs:
            lines.append(f"  - {inp.get('name')} ({inp.get('type', '?')})")
        algos = model.get("algorithms", [])
        lines.append(f"Steps ({len(algos)}):")
        for alg in algos:
            aid = alg.get("algorithm_id", "?")
            aid_short = aid.rsplit(":", 1)[-1] if ":" in aid else aid
            lines.append(f"  - {alg.get('id', '?')}: {alg.get('description', aid_short)}")
        if model.get("_mf_plan_issues"):
            lines.append("Issues:")
            for issue in model["_mf_plan_issues"]:
                lines.append(f"  [{issue.get('level', '?')}] {issue.get('message', '')}")
        return "\n".join(lines)

    @mcp.tool(
        description=(
            "Cancel an in-flight ``generate_model`` job. The job_id is the "
            "``job_id`` field in the response of a previous ``generate_model`` "
            "call, or in the progress notifications emitted via "
            "``progress_token``. Cancellation is cooperative - the running "
            "worker checks the cancel event between stages and short-circuits. "
            "Returns ``{cancelled: true}`` if a job was signalled."
        )
    )
    async def cancel_generation(job_id: str) -> str:
        from .jobs import get_registry

        registry = get_registry()
        ok = registry.cancel(job_id)
        if not ok:
            return json.dumps(
                {
                    "cancelled": False,
                    "job_id": job_id,
                    "note": "No in-flight job with that id; it may have already finished.",
                },
                indent=2,
                ensure_ascii=False,
            )
        return json.dumps(
            {"cancelled": True, "job_id": job_id},
            indent=2,
            ensure_ascii=False,
        )

    @mcp.tool(
        description=(
            "Get the current status of a ``generate_model`` job: status "
            "(pending/running/completed/failed/cancelled/timed_out), "
            "progress (current/total/message/percent), elapsed seconds, "
            "and any error message. Pass the ``job_id`` returned by "
            "``generate_model``. Returns ``null`` if the job id is unknown."
        )
    )
    async def get_generation_status(job_id: str) -> str:
        from .jobs import get_registry

        registry = get_registry()
        status = registry.status(job_id)
        if status is None:
            return json.dumps(None, indent=2, ensure_ascii=False)
        return json.dumps(status, indent=2, ensure_ascii=False)


# --- Tool: Map building (print layout + symbology + execution) ---


def _register_map_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        description=(
            "Generate a QGIS print layout template (.qpt) for the given model JSON. "
            "Choose a template: 'default', 'scientific', 'presentation', or 'minimal'. "
            "The LLM writes the title / subtitle text; the verifier checks structural "
            "correctness (margins, overlaps, required items) before returning. "
            "If ``verify=True`` (default), the layout is checked against the ruleset "
            "and a list of violations is included in the response. Pass "
            "``output_layer_ids`` to control what shows in the legend."
        )
    )
    async def generate_print_layout(
        model_json_str: str,
        output_path: str,
        template: str = "default",
        title: str = "",
        subtitle: str = "",
        crs: str = "",
        author: str = "",
        output_layer_ids: list[str] | None = None,
        verify: bool = True,
    ) -> str:
        try:
            from model_forge.compiler_core.core.services.map_builder.qpt_builder import build_qpt
            from model_forge.compiler_core.core.services.map_builder.layout_verifier import (
                verify_qpt,
            )
        except ImportError as e:
            return error_response_json(
                ConfigError(
                    "Map-building modules are not available in this environment.",
                    details={"exception": str(e)},
                )
            )

        try:
            model = _model_json_from_str(model_json_str)
        except json.JSONDecodeError as e:
            return error_response_json(
                InvalidJSONError(
                    f"Invalid model JSON: {e}",
                    details={"line": e.lineno, "column": e.colno},
                )
            )

        try:
            qpt_xml = build_qpt(
                template=template,
                title=title,
                subtitle=subtitle,
                crs=crs,
                author=author,
                output_layer_ids=output_layer_ids,
            )
        except Exception as e:  # noqa: BLE001
            return error_response_json(
                ConfigError(
                    f"qpt_builder failed: {e}",
                    details={"template": template, "exception_type": type(e).__name__},
                )
            )

        try:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(qpt_xml)
        except OSError as e:
            return error_response_json(
                ConfigError(
                    f"Failed to write .qpt to {output_path}: {e}",
                    details={"output_path": output_path},
                )
            )

        result: dict = {
            "status": "ok",
            "path": output_path,
            "template": template,
            "bytes_written": len(qpt_xml.encode("utf-8")),
        }
        if verify:
            try:
                report = verify_qpt(qpt_xml)
                result["verification"] = report.to_dict()
            except Exception as e:  # noqa: BLE001
                result["verification"] = {
                    "passed": False,
                    "error": f"verifier raised: {e}",
                }
        return json.dumps(result, indent=2, ensure_ascii=False)

    @mcp.tool(
        description=(
            "Verify a .qpt print layout document against the layout ruleset. "
            "Returns a list of violations plus a pass/fail verdict. Use this in "
            "a re-try loop: if the LLM emits a layout that fails verification, "
            "include the violation messages as constraints for the next emission."
        )
    )
    async def verify_layout(qpt_xml_or_path: str) -> str:
        try:
            from model_forge.compiler_core.core.services.map_builder.layout_verifier import (
                verify_qpt,
            )
        except ImportError as e:
            return error_response_json(
                ConfigError(
                    "layout_verifier is not available.",
                    details={"exception": str(e)},
                )
            )
        # Accept either inline XML or a path to a .qpt file.
        qpt_xml = qpt_xml_or_path
        if "\n" not in qpt_xml and "<Layout" not in qpt_xml:
            try:
                with open(qpt_xml, encoding="utf-8") as f:
                    qpt_xml = f.read()
            except OSError as e:
                return error_response_json(
                    ConfigError(
                        f"Failed to read {qpt_xml}: {e}",
                        details={"path": qpt_xml},
                    )
                )
        report = verify_qpt(qpt_xml)
        return json.dumps(report.to_dict(), indent=2, ensure_ascii=False)

    @mcp.tool(
        description=(
            "Render a .qpt print layout to PDF, PNG, or SVG using QGIS. "
            "QGIS is required (the tool returns E_QGIS_NOT_AVAILABLE in headless "
            "mode unless a custom QgisProject is supplied). For PDFs the "
            "rendering is vector (crisp at any zoom); for PNGs the resolution "
            "is controlled by the ``dpi`` parameter."
        )
    )
    async def export_layout(
        qpt_path: str,
        output_path: str,
        format: str = "pdf",
        dpi: int = 300,
    ) -> str:
        if not _HAS_QGIS:
            return error_response_json(
                QGISNotAvailableError(
                    "export_layout requires QGIS. Use 'qpt' format and open the file in QGIS."
                )
            )
        try:
            from qgis.core import (
                QgsApplication,
                QgsLayoutExporter,
                QgsPrintLayout,
                QgsProject,
            )
            from qgis.PyQt.QtCore import QRectF
        except ImportError as e:
            return error_response_json(
                QGISNotAvailableError(
                    "QGIS Python bindings are incomplete.",
                    details={"exception": str(e)},
                )
            )
        project = QgsProject.instance()
        layout = QgsPrintLayout(project)
        # ``loadFromTemplate`` reads the .qpt XML; if the file is
        # not on disk, the caller must have passed inline content.
        if not layout.loadFromTemplate(qpt_path):
            return error_response_json(
                ConfigError(
                    f"Failed to load .qpt from {qpt_path}",
                    details={"path": qpt_path},
                )
            )
        fmt = format.lower()
        if fmt == "pdf":
            exporter = QgsLayoutExporter(layout)
            settings = QgsLayoutExporter.PdfExportSettings()
            settings.dpi = dpi
            result = exporter.exportToPdf(output_path, settings)
            ok = result == QgsLayoutExporter.ExportResult.Success
        elif fmt == "png":
            exporter = QgsLayoutExporter(layout)
            settings = QgsLayoutExporter.ImageExportSettings()
            settings.dpi = dpi
            page_rect = QRectF(
                0,
                0,
                layout.pageCollection().page(0).pageSize().width(),
                layout.pageCollection().page(0).pageSize().height(),
            )
            result = exporter.exportToImage(output_path, settings, page_rect)
            ok = result == QgsLayoutExporter.ExportResult.Success
        elif fmt == "svg":
            exporter = QgsLayoutExporter(layout)
            settings = QgsLayoutExporter.SvgExportSettings()
            settings.dpi = dpi
            result = exporter.exportToSvg(output_path, settings)
            ok = result == QgsLayoutExporter.ExportResult.Success
        else:
            return error_response_json(
                ConfigError(
                    f"Unknown export format {format!r}. Use pdf, png, or svg.",
                    details={"format": format},
                )
            )
        if not ok:
            return error_response_json(
                ConfigError(
                    f"Layout export failed with code {result}.",
                    details={"path": output_path, "format": fmt, "code": int(result)},
                )
            )
        return json.dumps(
            {"status": "ok", "path": output_path, "format": fmt, "dpi": dpi},
            indent=2,
            ensure_ascii=False,
        )

    @mcp.tool(
        description=(
            "Generate per-layer-type default symbology (QML XML) for the "
            "model's output layers. The LLM picks a renderer per layer "
            "(single_symbol / categorized / graduated / rule_based); the "
            "tool emits one .qml file per layer. Outputs are written to "
            "``output_dir`` and named ``<step_id>.qml``. Returns a list of "
            "{step_id, geometry_kind, renderer, path} entries."
        )
    )
    async def generate_symbology(
        model_json_str: str,
        output_dir: str,
        *,
        default_renderer: str = "single_symbol",
        classification_field: str | None = None,
        classification_method: str = "equal",
    ) -> str:
        try:
            from model_forge.compiler_core.core.services.map_builder.qml_builder import (
                build_qml,
            )
        except ImportError as e:
            return error_response_json(
                ConfigError(
                    "qml_builder is not available.",
                    details={"exception": str(e)},
                )
            )
        try:
            model = _model_json_from_str(model_json_str)
        except json.JSONDecodeError as e:
            return error_response_json(
                InvalidJSONError(
                    f"Invalid model JSON: {e}",
                    details={"line": e.lineno, "column": e.colno},
                )
            )

        import os

        os.makedirs(output_dir, exist_ok=True)

        # Determine geometry kind per step from the upstream
        # model_input fields, or default to polygon.
        layer_results: list[dict[str, Any]] = []
        for alg in model.get("algorithms", []):
            step_id = str(alg.get("id", "") or "")
            if not step_id:
                continue
            kind = _guess_geometry_kind(alg)
            try:
                qml = build_qml(
                    geometry_kind=kind,
                    layer_name=step_id,
                    renderer=default_renderer,
                    field_name=classification_field,
                    classes=5,
                    classification_mode=classification_method,
                )
            except Exception as e:  # noqa: BLE001
                layer_results.append({"step_id": step_id, "error": f"qml_builder failed: {e}"})
                continue
            out_path = os.path.join(output_dir, f"{step_id}.qml")
            try:
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(qml)
            except OSError as e:
                layer_results.append({"step_id": step_id, "error": f"write failed: {e}"})
                continue
            layer_results.append(
                {
                    "step_id": step_id,
                    "geometry_kind": kind,
                    "renderer": default_renderer,
                    "path": out_path,
                    "bytes_written": len(qml.encode("utf-8")),
                }
            )
        return json.dumps(
            {
                "status": "ok",
                "output_dir": output_dir,
                "layers": layer_results,
            },
            indent=2,
            ensure_ascii=False,
        )

    @mcp.tool(
        description=(
            "Run a model JSON in a QGIS environment. Requires QGIS Python "
            "(no GUI needed) - the runner calls ``processing.run()`` for "
            "each step in topological order and threads outputs to inputs. "
            "``fail_fast=True`` (default) aborts on the first failure; "
            "``fail_fast=False`` continues and reports per-step status. "
            "``max_retries`` retries each step on failure with exponential "
            "backoff. Returns a per-step execution report with timings, "
            "status, inputs, and outputs."
        )
    )
    async def execute_model(
        model_json_str: str,
        fail_fast: bool = True,
        max_retries: int = 0,
    ) -> str:
        try:
            from model_forge.compiler_core.core.services.model_runner import (
                run_model,
            )
        except ImportError as e:
            return error_response_json(
                ConfigError(
                    "model_runner is not available.",
                    details={"exception": str(e)},
                )
            )
        try:
            model = _model_json_from_str(model_json_str)
        except json.JSONDecodeError as e:
            return error_response_json(
                InvalidJSONError(
                    f"Invalid model JSON: {e}",
                    details={"line": e.lineno, "column": e.colno},
                )
            )
        try:
            report = run_model(
                model,
                fail_fast=fail_fast,
                max_retries=max_retries,
            )
        except Exception as e:  # noqa: BLE001
            return error_response_json(
                ConfigError(
                    f"Model run failed: {e}",
                    details={"exception_type": type(e).__name__},
                )
            )
        return json.dumps(report.to_dict(), indent=2, ensure_ascii=False)


# --- Tool: Server Management --------------------------------------------


def _register_management_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        description=(
            "Check the subscription dirty-set for resources and the "
            "tools/list_changed flag. Returns a list of resource URIs that "
            "changed since the last consume, a boolean for tools/list_changed, "
            "and a monotonically increasing version number. "
            "Pass consume=True to atomically clear the dirty set (clients "
            "typically call this after re-reading the listed resources). "
            "Use ``subscribe_resource`` / ``unsubscribe_resource`` to "
            "register interest in a specific URI."
        )
    )
    async def subscription_status(consume: bool = False) -> str:
        from .subscriptions import get_subscription_registry

        reg = get_subscription_registry()
        payload = reg.consume_dirty() if consume else reg.status()
        return json.dumps(payload, indent=2, ensure_ascii=False)

    @mcp.tool(
        description=(
            "Subscribe to changes on a given model-forge:// resource. "
            "Returns ``{subscribed: true}`` on success. The URI must be "
            "one of the subscribable_uris reported in model-forge://server-info. "
            "After subscribing, poll ``subscription_status`` to learn when "
            "the resource is dirty."
        )
    )
    async def subscribe_resource(uri: str) -> str:
        from .subscriptions import get_subscription_registry

        reg = get_subscription_registry()
        ok = reg.subscribe(uri)
        if not ok:
            return error_response_json(
                ConfigError(
                    f"URI {uri!r} is not subscribable.",
                    details={"uri": uri},
                )
            )
        return json.dumps({"subscribed": True, "uri": uri}, indent=2, ensure_ascii=False)

    @mcp.tool(
        description=(
            "Unsubscribe from a previously-subscribed resource URI. Returns "
            "{unsubscribed: true} on success, {unsubscribed: false} if the "
            "client was not subscribed to that URI."
        )
    )
    async def unsubscribe_resource(uri: str) -> str:
        from .subscriptions import get_subscription_registry

        reg = get_subscription_registry()
        ok = reg.unsubscribe(uri)
        return json.dumps(
            {"unsubscribed": ok, "uri": uri},
            indent=2,
            ensure_ascii=False,
        )

    @mcp.tool(
        description=(
            "Health check / ping. Returns server status, version, schema version, "
            "transport mode (sse/stdio), QGIS availability flag, layer count, and "
            "algorithm catalog size. Call this first to verify the server is "
            "responsive before using other tools."
        )
    )
    async def ping() -> str:
        running = is_running()
        return json.dumps(
            {
                "status": "ok" if running else "stopped",
                "version": __version__,
                "schema_version": SCHEMA_VERSION,
                "qgis_available": _HAS_QGIS,
                "transport": "sse" if _server_thread else "stdio",
                "layer_count": len(_get_state().context.get("layers", [])),
                "catalog_size": len(_get_state().catalog),
            },
            indent=2,
            ensure_ascii=False,
        )

    @mcp.tool(
        description=(
            "Configure the LLM backend for model generation. Required before calling "
            "generate_model. Supports 'ollama' (default, http://localhost:11434), "
            "'openai' (OpenAI Chat Completions), 'openai_compat' (any OpenAI-compatible "
            "endpoint such as vLLM, LM Studio, OpenRouter - set base_url), "
            "'azure_openai' (Azure-hosted OpenAI deployments - model field is the "
            "deployment name, base_url is the resource endpoint), 'anthropic' "
            "(Anthropic Messages API, e.g. claude-3-5-sonnet), and 'gemini' "
            "(Google Gemini generateContent API). "
            "default_headers and extra_body are JSON strings forwarded to the provider. "
            "Set persist=True to save the config to mcp.json so subsequent server starts "
            "reuse it. Returns the effective config (without API key) on success."
        )
    )
    async def set_llm_config(
        provider: str = "ollama",
        model: str = "qwen2.5-coder:7b",
        base_url: str = "http://localhost:11434",
        api_key: str = "",
        temperature: float = 0.2,
        timeout: float = 120.0,
        max_retries: int = 2,
        default_headers: str = "{}",
        extra_body: str = "{}",
        persist: bool = True,
    ) -> str:
        state = _get_state()
        try:
            provider_normalized = normalize_provider(provider)
            headers = json.loads(default_headers) if default_headers else {}
            extra = json.loads(extra_body) if extra_body else {}
            new_cfg = LLMConfig(
                provider=provider_normalized,
                model=model,
                base_url=base_url,
                api_key=api_key,
                temperature=temperature,
                timeout=timeout,
                max_retries=max_retries,
                default_headers={str(k): str(v) for k, v in headers.items()},
                extra_body=dict(extra),
            )
            new_cfg.validate()
        except (ConfigError, json.JSONDecodeError) as e:
            return error_response_json(
                e
                if isinstance(e, ConfigError)
                else ConfigError(
                    f"Invalid default_headers / extra_body: {e}",
                )
            )

        state.llm_config = new_cfg.to_dict()
        if persist:
            try:
                _save_config({"llm": state.llm_config})
            except Exception as e:  # noqa: BLE001
                return error_response_json(
                    ConfigError(
                        f"Failed to persist config to {_config_path()}: {e}",
                        details={"config_path": _config_path()},
                    )
                )
        # Tell subscribers the server-info resource changed.
        from .subscriptions import get_subscription_registry

        get_subscription_registry().mark_resource_dirty("model-forge://server-info")
        safe = {k: v for k, v in state.llm_config.items() if k != "api_key"}
        return json.dumps(
            {
                "status": "ok",
                "persisted": persist,
                "config": safe,
            },
            indent=2,
            ensure_ascii=False,
        )

    @mcp.tool(
        description=(
            "Get the current server configuration: version, schema version, QGIS "
            "availability, LLM provider/model (without API key), cached layer count, "
            "and catalog size. Use this to verify LLM config was set correctly by "
            "set_llm_config."
        )
    )
    async def get_server_info() -> str:
        state = _get_state()
        config_safe = (
            {k: v for k, v in state.llm_config.items() if k != "api_key"}
            if state.llm_config
            else {}
        )
        return json.dumps(
            {
                "version": __version__,
                "schema_version": SCHEMA_VERSION,
                "qgis_available": _HAS_QGIS,
                "supported_providers": list(PROVIDERS),
                "llm_config": config_safe,
                "context_layers": len(state.context.get("layers", [])),
                "catalog_size": len(state.catalog),
            },
            indent=2,
            ensure_ascii=False,
        )


# --- Server creation ----------------------------------------------------


# Single source of truth for the tool list. The ``server-info`` resource
# reads from this so the hardcoded list can never drift from reality.
TOOL_REGISTRY: tuple[str, ...] = (
    "ping",
    "list_layers",
    "get_layer_info",
    "get_project_info",
    "refresh_qgis_context",
    "configure_headless_context",
    "list_algorithms",
    "get_algorithm_info",
    "list_providers",
    "list_algorithm_groups",
    "load_catalog_from_file",
    "export_catalog",
    "generate_model",
    "validate_model",
    "export_model",
    "summarize_model",
    "generate_print_layout",
    "verify_layout",
    "export_layout",
    "generate_symbology",
    "execute_model",
    "set_llm_config",
    "get_server_info",
    "cancel_generation",
    "get_generation_status",
    "subscription_status",
    "subscribe_resource",
    "unsubscribe_resource",
)


def create_server(state: ServerState | None = None) -> FastMCP:
    """Create a FastMCP server instance with all tools registered."""
    if not _HAS_MCP:
        raise RuntimeError("mcp package not installed. Run: pip install mcp")

    global _server_state
    if state is not None:
        _server_state = state

    mcp = FastMCP(
        "Model Forge",
        instructions=(
            "QGIS Processing model generation via the Model Forge "
            "compiler pipeline. Tools are chained: list_layers → "
            "list_algorithms → get_algorithm_info → generate_model → "
            "export_model, and for layout: generate_symbology → "
            "generate_print_layout → verify_layout → export_layout."
        ),
    )

    _register_context_tools(mcp)
    _register_algorithm_tools(mcp)
    _register_generation_tools(mcp)
    _register_map_tools(mcp)
    _register_management_tools(mcp)

    try:
        from .prompts import register_prompts

        register_prompts(mcp)
    except Exception:  # noqa: BLE001
        log.debug("Prompt registration failed", exc_info=True)

    @mcp.resource("model-forge://server-info")
    def resource_server_info() -> str:
        state = _get_state()
        from .subscriptions import SUBSCRIBE_CAPABILITIES, SUBSCRIBABLE_URIS

        return json.dumps(
            {
                "version": __version__,
                "schema_version": SCHEMA_VERSION,
                "qgis_available": _HAS_QGIS,
                "transport": "sse" if _server_thread else "stdio",
                "llm_configured": bool(state.llm_config.get("provider")),
                "layers": len(state.context.get("layers", [])),
                "catalog_size": len(state.catalog),
                "supported_providers": list(PROVIDERS),
                "tools": list(TOOL_REGISTRY),
                "prompts": list(
                    getattr(
                        __import__("model_forge.mcp_server.prompts", fromlist=["PROMPT_REGISTRY"]),
                        "PROMPT_REGISTRY",
                        (),
                    )
                ),
                "subscription_capabilities": dict(SUBSCRIBE_CAPABILITIES),
                "subscribable_resources": sorted(SUBSCRIBABLE_URIS),
            },
            indent=2,
            ensure_ascii=False,
        )

    @mcp.resource("model-forge://context/layers")
    def resource_layers() -> str:
        return json.dumps(_get_state().context.get("layers", []), indent=2, ensure_ascii=False)

    @mcp.resource("model-forge://algorithms")
    def resource_algorithms() -> str:
        return json.dumps(
            [
                {"id": k, "name": v.get("name", k), "group": v.get("group", "")}
                for k, v in _get_state().catalog.items()
            ],
            indent=2,
            ensure_ascii=False,
        )

    return mcp


# --- Start/stop ---------------------------------------------------------


def start_server(
    host: str = "127.0.0.1",
    port: int = 9090,
    llm_config: dict[str, Any] | None = None,
    transport: str = "sse",
    *,
    auth_token: str | None = None,
    ssl_certfile: str | None = None,
    ssl_keyfile: str | None = None,
    shutdown_timeout: float = 15.0,
) -> FastMCP:
    """Start the MCP server in a background thread (SSE mode) or return it for stdio.

    Parameters
    ----------
    host, port
        Bind address for the SSE transport.
    llm_config
        Optional caller-supplied LLM configuration; merged with
        on-disk + env values via ``build_llm_config``.
    transport
        ``"sse"`` (default) or ``"stdio"``.
    auth_token
        If set, requires every non-health HTTP request to carry
        ``Authorization: Bearer <token>`` or ``X-Model-Forge-Token: <token>``.
        Disabled for stdio and for ``127.0.0.1``/``localhost`` unless
        the caller explicitly sets this.
    ssl_certfile, ssl_keyfile
        If both are set, the SSE transport serves HTTPS.
    shutdown_timeout
        How long ``stop_server`` will wait for in-flight SSE
        connections to drain before forcing thread teardown.
    """
    global _server_instance, _server_thread, _server_state, _server_uvicorn, _server_port
    global _server_shutdown_timeout

    if is_running():
        log.warning("MCP server already running on port %s", _server_port)
        return _server_instance

    # Normalize the caller-supplied llm_config (if any) by running it
    # through ``build_llm_config`` so the same merge order (CLI > file
    # > env > auto) applies.
    if llm_config:
        try:
            merged = build_llm_config(cli=llm_config, file_cfg=_load_config())
            llm_config = merged.to_dict()
        except ConfigError as e:
            log.warning("LLM config invalid: %s", e.message)
            llm_config = {"provider": "", "model": ""}
    else:
        llm_config = build_llm_config(file_cfg=_load_config()).to_dict()

    state = ServerState(llm_config)
    _server_state = state
    _server_port = port
    _server_shutdown_timeout = max(0.0, float(shutdown_timeout))

    mcp = create_server(state)
    mcp.settings.host = host
    mcp.settings.port = port

    if transport == "stdio":
        _server_instance = mcp
        return mcp

    if not _HAS_UVICORN:
        raise RuntimeError("uvicorn is required for SSE mode. Run: pip install uvicorn")

    def _run_sse():
        nonlocal mcp
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            app = mcp.sse_app()
            if auth_token:
                app = _wrap_with_token_auth(app, auth_token)
            config_kwargs: dict[str, Any] = {
                "app": app,
                "host": host,
                "port": port,
                "log_level": "warning",
            }
            if ssl_certfile and ssl_keyfile:
                config_kwargs["ssl_certfile"] = ssl_certfile
                config_kwargs["ssl_keyfile"] = ssl_keyfile
            config = uvicorn.Config(**config_kwargs)
            server = uvicorn.Server(config)
            _server_uvicorn = server
            server.run()
        except Exception:
            log.warning("MCP SSE server stopped", exc_info=True)
        finally:
            import contextlib

            with contextlib.suppress(Exception):
                loop.close()

    _server_thread = threading.Thread(target=_run_sse, daemon=True, name="mcp-server")
    _server_thread.start()
    _server_instance = mcp

    for _ in range(20):
        if _server_uvicorn is not None:
            break
        time.sleep(0.1)

    scheme = "https" if (ssl_certfile and ssl_keyfile) else "http"
    log.info("MCP server started on %s://%s:%s/sse", scheme, host, port)
    return mcp


def stop_server():
    """Gracefully stop the MCP server background thread.

    We use uvicorn's proper shutdown path (``server.shutdown()``)
    which awaits the ASGI lifespan exit, then wait up to
    ``shutdown_timeout`` seconds (default 15) for the worker
    thread. Old behavior used ``should_exit = True; sleep(0.3);
    thread.join(5)`` which could drop in-flight SSE messages.
    """
    global _server_instance, _server_thread, _server_uvicorn

    if _server_uvicorn is not None:
        try:
            _server_uvicorn.shutdown()
        except Exception:  # noqa: BLE001
            log.debug("uvicorn.shutdown() raised", exc_info=True)
        import contextlib

        with contextlib.suppress(Exception):
            _server_uvicorn.should_exit = True

    if _server_thread and _server_thread.is_alive():
        _server_thread.join(timeout=_server_shutdown_timeout or 15.0)

    try:
        from .jobs import reset_registry

        reset_registry()
    except Exception:  # noqa: BLE001
        log.debug("Job registry shutdown failed", exc_info=True)

    _server_instance = None
    _server_thread = None
    _server_uvicorn = None
    log.info("MCP server stopped")


def _wrap_with_token_auth(app: Any, expected_token: str) -> Any:
    """Wrap a Starlette/ASGI app with bearer-token auth.

    All requests must carry either ``Authorization: Bearer <token>``
    or ``X-Model-Forge-Token: <token>``. The path ``/healthz`` is
    always allowed (used by orchestrators for liveness probes).
    Falls back to a no-op wrap if Starlette isn't importable - the
    SSE transport still works, just without auth.
    """
    try:
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import JSONResponse
    except ImportError:
        log.warning("starlette not available - token auth not enforced")
        return app

    expected = str(expected_token)

    class _TokenAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):  # type: ignore[no-untyped-def]
            path = request.url.path
            if path in ("/healthz", "/health"):
                return await call_next(request)
            auth = request.headers.get("authorization", "")
            custom = request.headers.get("x-model-forge-token", "")
            token = ""
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
            elif custom:
                token = custom.strip()
            if token != expected:
                return JSONResponse(
                    {"error": "unauthorized", "code": "E_AUTH"},
                    status_code=401,
                )
            return await call_next(request)

    return _TokenAuthMiddleware(app)


def is_running() -> bool:
    if _server_thread and _server_thread.is_alive():
        return True
    if _server_uvicorn is not None and not getattr(_server_uvicorn, "should_exit", True):
        return True
    return _server_instance is not None


# --- CLI entry point ----------------------------------------------------


def main():
    """CLI entry point: `python -m model_forge.mcp_server`"""

    parser = argparse.ArgumentParser(description="Model Forge MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport mode (default: stdio for Claude Desktop)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="SSE host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=9090, help="SSE port (default: 9090)")
    parser.add_argument(
        "--llm-provider",
        default="",
        help=(
            "LLM provider. One of: ollama, openai, openai_compat, anthropic. "
            "Empty means auto-detect."
        ),
    )
    parser.add_argument("--llm-model", default="", help="LLM model (overrides auto-detect)")
    parser.add_argument("--llm-base-url", default="", help="LLM base URL")
    parser.add_argument(
        "--llm-api-key",
        default=os.environ.get("MODELFORGE_API_KEY", ""),
        help="LLM API key (or MODELFORGE_API_KEY env var)",
    )
    parser.add_argument(
        "--llm-temperature", type=float, default=None, help="LLM temperature (default: 0.2)"
    )
    parser.add_argument(
        "--llm-timeout", type=float, default=None, help="LLM request timeout in seconds"
    )
    parser.add_argument(
        "--llm-default-headers",
        default="",
        help='JSON object of custom HTTP headers (e.g. \'{"X-Org": "acme"}\')',
    )
    parser.add_argument(
        "--llm-extra-body",
        default="",
        help="JSON object merged into the LLM request body (provider-specific fields)",
    )
    parser.add_argument(
        "--auth-token",
        default=os.environ.get("MODELFORGE_MCP_TOKEN", ""),
        help=(
            "If set, requires the SSE transport to authenticate every "
            "request with this bearer token (header: Authorization: Bearer "
            "<token>, or X-Model-Forge-Token: <token>). The /healthz path "
            "is exempt. Also settable via MODELFORGE_MCP_TOKEN."
        ),
    )
    parser.add_argument(
        "--tls-cert",
        default=os.environ.get("MODELFORGE_MCP_TLS_CERT", ""),
        help="Path to TLS certificate (PEM). Pair with --tls-key for HTTPS.",
    )
    parser.add_argument(
        "--tls-key",
        default=os.environ.get("MODELFORGE_MCP_TLS_KEY", ""),
        help="Path to TLS private key (PEM). Pair with --tls-cert for HTTPS.",
    )
    parser.add_argument(
        "--shutdown-timeout",
        type=float,
        default=15.0,
        help="Seconds to wait for in-flight SSE connections to drain on stop_server.",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    cli_overrides: dict[str, Any] = {}
    if args.llm_provider:
        cli_overrides["provider"] = normalize_provider(args.llm_provider)
    if args.llm_model:
        cli_overrides["model"] = args.llm_model
    if args.llm_base_url:
        cli_overrides["base_url"] = args.llm_base_url
    if args.llm_api_key:
        cli_overrides["api_key"] = args.llm_api_key
    if args.llm_temperature is not None:
        cli_overrides["temperature"] = args.llm_temperature
    if args.llm_timeout is not None:
        cli_overrides["timeout"] = args.llm_timeout
    if args.llm_default_headers:
        try:
            cli_overrides["default_headers"] = json.loads(args.llm_default_headers)
        except json.JSONDecodeError as e:
            log.error("--llm-default-headers must be a JSON object: %s", e)
            raise SystemExit(2) from e
    if args.llm_extra_body:
        try:
            cli_overrides["extra_body"] = json.loads(args.llm_extra_body)
        except json.JSONDecodeError as e:
            log.error("--llm-extra-body must be a JSON object: %s", e)
            raise SystemExit(2) from e

    try:
        cfg = build_llm_config(cli=cli_overrides, file_cfg=_load_config())
    except ConfigError as e:
        log.error("LLM config error: %s", e.message)
        raise SystemExit(2) from e
    llm_config = cfg.to_dict()

    if not _HAS_QGIS:
        log.warning("QGIS not available - context tools will return empty results.")

    if args.transport == "stdio":
        mcp = start_server(transport="stdio", llm_config=llm_config)
        mcp.run(transport="stdio")
    else:
        start_server(
            host=args.host,
            port=args.port,
            transport="sse",
            llm_config=llm_config,
            auth_token=args.auth_token or None,
            ssl_certfile=args.tls_cert or None,
            ssl_keyfile=args.tls_key or None,
            shutdown_timeout=args.shutdown_timeout,
        )
        scheme = "https" if (args.tls_cert and args.tls_key) else "http"
        log.info("MCP SSE server running on %s://%s:%s/sse", scheme, args.host, args.port)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            stop_server()


if __name__ == "__main__":
    main()
