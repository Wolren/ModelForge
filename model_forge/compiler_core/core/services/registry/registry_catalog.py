"""
RegistryCatalogService
=======================
Builds the algorithm catalog dict used by the compiler pipeline from the
live QGIS Processing registry.  Wraps ContextCollector but exposes a
richer filtering API.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

try:
    from qgis.core import (
        QgsApplication,
        QgsProcessingParameterDefinition,
        QgsProcessingParameterEnum,
    )

    _HAS_QGIS = True
except ImportError:
    _HAS_QGIS = False


class RegistryCatalogService:
    """Builds algorithm catalog from live QGIS registry. Results cached per session."""

    _catalog_cache: dict[str, dict[str, Any]] = {}
    _cache_key: str = ""

    def get_algorithm_catalog(
        self,
        include_native: bool = True,
        include_gdal: bool = True,
        include_grass: bool = False,
        include_saga: bool = False,
        include_all: bool = False,
        max_algorithms: int = 60,
        algorithm_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Returns a catalog dict: { algorithm_id -> {name, group, parameters, outputs} }
        Cached per session — invalidated when provider filter changes.
        """
        if not _HAS_QGIS:
            return {}

        cache_key = (
            f"n={include_native}_g={include_gdal}_gr={include_grass}_s={include_saga}"
            f"_a={include_all}_max={max_algorithms}"
        )
        if algorithm_ids:
            cache_key += "_ids=" + ",".join(sorted(algorithm_ids))

        if cache_key == self._cache_key and self._catalog_cache.get("_entries"):
            return dict(self._catalog_cache["_entries"])

        registry = QgsApplication.processingRegistry()

        provider_filter = set()
        if include_native:
            provider_filter.add("native")
        if include_gdal:
            provider_filter.add("gdal")
        if include_grass:
            provider_filter.add("grass7")
        if include_saga:
            provider_filter.add("saga")

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
            self._cache_key = cache_key
            self._catalog_cache["_entries"] = dict(catalog)
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

        self._cache_key = cache_key
        self._catalog_cache["_entries"] = dict(catalog)
        return catalog

    def _describe(self, alg) -> dict[str, Any]:
        params = []
        for p in alg.parameterDefinitions():
            pinfo = {
                "name": p.name(),
                "description": p.description(),
                "type": p.type(),
                "optional": bool(p.flags() & QgsProcessingParameterDefinition.Flag.FlagOptional),
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
        outputs = []
        try:
            for o in alg.outputDefinitions():
                outputs.append(
                    {
                        "name": o.name(),
                        "description": o.description(),
                        "type": o.type(),
                    }
                )
        except Exception:
            log.warning("Failed to read outputs for %s", alg.id() if hasattr(alg, "id") else alg)
            outputs = []

        return {
            "name": alg.displayName(),
            "group": alg.group(),
            "parameters": params,
            "outputs": outputs,
        }
