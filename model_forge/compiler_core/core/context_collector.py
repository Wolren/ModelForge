"""
ContextCollector
================
Snapshots the live QGIS state (layers, CRS, selection, extent) and
algorithm registry into the qgis_context dict used by the compiler.
Works gracefully when QGIS is not importable (e.g. pure-Python tests).
"""

from __future__ import annotations

from typing import Any

try:
    from qgis.core import (
        QgsApplication,
        QgsProject,
        QgsRasterLayer,
        QgsVectorLayer,
        QgsWkbTypes,
    )

    _HAS_QGIS = True
except ImportError:
    _HAS_QGIS = False


class ContextCollector:
    _algorithm_cache: dict[str, dict[str, Any]] = {}

    @classmethod
    def _cache_key(
        cls,
        algorithm_ids: list | None,
        max_algorithms: int,
        provider_ids: tuple | None,
        algorithm_groups: tuple | None,
    ) -> str:
        parts = []
        if algorithm_ids:
            parts.append("ids=" + ",".join(sorted(algorithm_ids)))
        parts.append(f"max={max_algorithms}")
        if provider_ids:
            parts.append("prov=" + ",".join(sorted(provider_ids)))
        if algorithm_groups:
            parts.append("grp=" + ",".join(sorted(algorithm_groups)))
        return "|".join(parts)

    def collect(
        self,
        include_layers: bool = True,
        algorithm_ids: list | None = None,
        max_algorithms: int = 60,
        provider_ids: list[str] | None = None,
        algorithm_groups: list[str] | None = None,
    ) -> dict[str, Any]:
        ctx: dict[str, Any] = {
            "layers": [],
            "algorithms": {},
            "project_crs": None,
            "canvas_extent": None,
        }

        if not _HAS_QGIS:
            return ctx

        # ── Layers ────────────────────────────────────────────────────
        if include_layers:
            for layer_id, layer in QgsProject.instance().mapLayers().items():
                info: dict[str, Any] = {
                    "id": layer_id,
                    "name": layer.name(),
                    "type": "vector"
                    if isinstance(layer, QgsVectorLayer)
                    else ("raster" if isinstance(layer, QgsRasterLayer) else "other"),
                    "crs": layer.crs().authid() if layer.isValid() else None,
                }
                if isinstance(layer, QgsVectorLayer):
                    info["geometry_type"] = QgsWkbTypes.displayString(layer.wkbType())
                    info["feature_count"] = layer.featureCount()
                    info["fields"] = [
                        {"name": f.name(), "type": f.typeName()} for f in layer.fields()
                    ]
                ctx["layers"].append(info)

        # ── Project CRS ───────────────────────────────────────────────
        crs = QgsProject.instance().crs()
        if crs.isValid():
            ctx["project_crs"] = crs.authid()
        # ── Algorithm registry (limited subset) ───────────────────────
        registry = QgsApplication.processingRegistry()
        count = 0

        ck = self._cache_key(
            algorithm_ids,
            max_algorithms,
            tuple(sorted(provider_ids)) if provider_ids else None,
            tuple(sorted(algorithm_groups)) if algorithm_groups else None,
        )
        cached = self._algorithm_cache.get(ck)
        if cached is not None:
            ctx["algorithms"] = cached
            return ctx

        if algorithm_ids:
            for alg_id in algorithm_ids:
                alg = registry.algorithmById(alg_id)
                if alg:
                    ctx["algorithms"][alg_id] = {
                        "name": alg.displayName(),
                        "group": alg.group(),
                        "parameters": [
                            {"name": p.name(), "type": p.type()} for p in alg.parameterDefinitions()
                        ],
                    }
            self._algorithm_cache[ck] = ctx["algorithms"]
            return ctx

        provider_filter = provider_ids or ("native", "gdal")
        group_filter = set(algorithm_groups or [])

        for provider in registry.providers():
            pid = provider.id()
            if pid not in provider_filter:
                continue
            for alg in provider.algorithms():
                if count >= max_algorithms:
                    break
                if group_filter and alg.group() not in group_filter:
                    continue
                ctx["algorithms"][alg.id()] = {
                    "name": alg.displayName(),
                    "group": alg.group(),
                    "provider": pid,
                    "parameters": [
                        {"name": p.name(), "type": p.type()} for p in alg.parameterDefinitions()
                    ],
                }
                count += 1

        self._algorithm_cache[ck] = ctx["algorithms"]
        return ctx
