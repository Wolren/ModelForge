"""
RegistryCatalogService
=======================
Builds the algorithm catalog dict used by the compiler pipeline from the
live QGIS Processing registry.  Wraps ContextCollector but exposes a
richer filtering API.
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional

try:
    from qgis.core import QgsApplication, QgsProcessingParameterEnum, QgsProcessingParameterDefinition
    _HAS_QGIS = True
except ImportError:
    _HAS_QGIS = False


class RegistryCatalogService:

    def get_algorithm_catalog(
        self,
        include_native: bool = True,
        include_gdal: bool = True,
        include_grass: bool = False,
        include_saga: bool = False,
        include_all: bool = False,
        max_algorithms: int = 60,
        algorithm_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Returns a catalog dict: { algorithm_id -> {name, group, parameters} }
        """
        if not _HAS_QGIS:
            return {}

        registry = QgsApplication.processingRegistry()

        provider_filter = set()
        if include_native:  provider_filter.add("native")
        if include_gdal:    provider_filter.add("gdal")
        if include_grass:   provider_filter.add("grass7")
        if include_saga:    provider_filter.add("saga")

        catalog = {}
        count = 0

        if algorithm_ids:
            # Explicit list takes priority
            for alg_id in algorithm_ids:
                if count >= max_algorithms:
                    break
                alg = registry.algorithmById(alg_id)
                if alg:
                    catalog[alg_id] = self._describe(alg)
                    count += 1
            return catalog

        for provider in registry.providers():
            pid = provider.id()
            if not include_all and pid not in provider_filter:
                continue
            for alg in provider.algorithms():
                if count >= max_algorithms:
                    break
                catalog[alg.id()] = self._describe(alg)
                count += 1

        return catalog

    def _describe(self, alg) -> Dict[str, Any]:
        params = []
        for p in alg.parameterDefinitions():
            pinfo = {
                "name":           p.name(),
                "description":    p.description(),
                "type":           p.type(),
                "optional":       bool(p.flags() & QgsProcessingParameterDefinition.Flag.FlagOptional),
                "is_destination": p.isDestination(),
            }
            if p.defaultValue() is not None:
                try:
                    import json
                    json.dumps(p.defaultValue())
                    pinfo["default"] = p.defaultValue()
                except (TypeError, ValueError):
                    pinfo["default"] = str(p.defaultValue())
            if isinstance(p, QgsProcessingParameterEnum):
                pinfo["enum_options"] = p.options()
            params.append(pinfo)
        return {
            "name":       alg.displayName(),
            "group":      alg.group(),
            "parameters": params,
        }
