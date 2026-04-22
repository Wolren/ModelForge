"""
ContextCollector
================
Snapshots the live QGIS state (layers, CRS, selection, extent) and
algorithm registry into the qgis_context dict used by the compiler.
Works gracefully when QGIS is not importable (e.g. pure-Python tests).
"""
from __future__ import annotations
from typing import Any, Dict

try:
    from qgis.core import (
        QgsProject, QgsVectorLayer, QgsRasterLayer,
        QgsApplication, QgsWkbTypes,
    )
    _HAS_QGIS = True
except ImportError:
    _HAS_QGIS = False


class ContextCollector:

    def collect(
        self,
        include_layers: bool = True,
        algorithm_ids: list | None = None,
        max_algorithms: int = 60,
    ) -> Dict[str, Any]:
        ctx: Dict[str, Any] = {
            "layers":     [],
            "algorithms": {},
            "project_crs": None,
            "canvas_extent": None,
        }

        if not _HAS_QGIS:
            return ctx

        # ── Layers ────────────────────────────────────────────────────
        if include_layers:
            for layer_id, layer in QgsProject.instance().mapLayers().items():
                info: Dict[str, Any] = {
                    "id":    layer_id,
                    "name":  layer.name(),
                    "type":  "vector" if isinstance(layer, QgsVectorLayer) else (
                             "raster" if isinstance(layer, QgsRasterLayer) else "other"),
                    "crs":   layer.crs().authid() if layer.isValid() else None,
                }
                if isinstance(layer, QgsVectorLayer):
                    info["geometry_type"] = QgsWkbTypes.displayString(
                        layer.wkbType()
                    )
                    info["feature_count"] = layer.featureCount()
                    info["fields"] = [
                        {"name": f.name(), "type": f.typeName()}
                        for f in layer.fields()
                    ]
                ctx["layers"].append(info)

        # ── Project CRS ───────────────────────────────────────────────
        crs = QgsProject.instance().crs()
        if crs.isValid():
            ctx["project_crs"] = crs.authid()

        # ── Algorithm registry (limited subset) ───────────────────────
        registry = QgsApplication.processingRegistry()
        count = 0

        if algorithm_ids:
            for alg_id in algorithm_ids:
                alg = registry.algorithmById(alg_id)
                if alg:
                    ctx["algorithms"][alg_id] = {
                        "name":  alg.displayName(),
                        "group": alg.group(),
                        "parameters": [
                            {"name": p.name(), "type": p.type()}
                            for p in alg.parameterDefinitions()
                        ],
                    }
            return ctx

        for provider in registry.providers():
            pid = provider.id()
            if pid not in ("native", "gdal"):
                continue
            for alg in provider.algorithms():
                if count >= max_algorithms:
                    break
                ctx["algorithms"][alg.id()] = {
                    "name":  alg.displayName(),
                    "group": alg.group(),
                    "parameters": [
                        {"name": p.name(), "type": p.type()}
                        for p in alg.parameterDefinitions()
                    ],
                }
                count += 1

        return ctx
