"""
Collects rich context from the current QGIS session:
- Layer metadata (names, fields, CRS, geometry, raster info)
- Processing algorithm registry (available tools + parameter signatures + enum options)
- QGIS version and provider info
"""

import json

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsProcessingParameterDefinition,
    QgsProcessingParameterEnum,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)

CURATED_ALGORITHMS = [
    "native:clip", "native:dissolve", "native:buffer", "native:intersection",
    "native:difference", "native:union", "native:mergevectorlayers",
    "native:reprojectlayer", "native:fieldcalculator", "native:extractbyattribute",
    "native:extractbyexpression", "native:selectbyexpression",
    "native:joinattributesbylocation", "native:joinattributestable",
    "native:centroids", "native:convexhull", "native:minimumboundinggeometry",
    "native:multiparttosingleparts", "native:fixgeometries",
    "native:savefeatures", "native:createspatialindex",
    "native:countpointsinpolygon", "native:addautoincrementalfield",
    "qgis:zonalstatisticsfb", "qgis:basicstatisticsforfields",
    "qgis:randompointsinextent",
    "gdal:slope", "gdal:hillshade", "gdal:aspect", "gdal:roughness",
    "gdal:contour", "gdal:warpreproject", "gdal:cliprasterbymasklayer",
    "gdal:rasterize", "gdal:translate", "gdal:merge",
    "native:reclassifybytable",
]

GEOMETRY_NAMES = {0: "Point", 1: "Line", 2: "Polygon", 3: "Unknown", 4: "Null"}
FIELD_TYPE_NAMES = {
    1: "Boolean", 2: "Int", 4: "LongLong", 6: "Double",
    10: "String", 14: "Date", 15: "Time", 16: "DateTime",
}


class ContextCollector:

    def get_qgis_version(self):
        return Qgis.QGIS_VERSION

    def get_providers_summary(self):
        registry = QgsApplication.processingRegistry()
        providers = []
        for p in registry.providers():
            providers.append({
                "id": p.id(),
                "name": p.name(),
                "algorithm_count": len(p.algorithms()),
            })
        return providers

    def get_layer_catalog(self):
        layers = []
        for layer in QgsProject.instance().mapLayers().values():
            info = {
                "name": layer.name(),
                "id": layer.id(),
                "crs": layer.crs().authid(),
                "source": layer.source(),
            }

            if isinstance(layer, QgsVectorLayer):
                info["layer_type"] = "vector"
                info["geometry_type"] = GEOMETRY_NAMES.get(layer.geometryType(), "Unknown")
                info["feature_count"] = layer.featureCount()
                info["fields"] = []
                for field in layer.fields():
                    info["fields"].append({
                        "name": field.name(),
                        "type": FIELD_TYPE_NAMES.get(field.type(), str(field.type())),
                        "length": field.length(),
                    })
                storage = layer.dataProvider().storageType() if layer.dataProvider() else "unknown"
                info["storage_type"] = storage

            elif isinstance(layer, QgsRasterLayer):
                info["layer_type"] = "raster"
                info["band_count"] = layer.bandCount()
                info["width"] = layer.width()
                info["height"] = layer.height()
                info["bands"] = []
                dp = layer.dataProvider()
                if dp:
                    for b in range(1, layer.bandCount() + 1):
                        info["bands"].append({
                            "number": b,
                            "name": layer.bandName(b),
                            "nodata": dp.sourceNoDataValue(b),
                            "data_type": dp.dataType(b),
                        })
                    info["pixel_size_x"] = dp.xSize()
                    info["pixel_size_y"] = dp.ySize()
                src = layer.source().lower()
                if ".tif" in src or ".tiff" in src:
                    info["file_format"] = "GeoTIFF"
                elif ".img" in src:
                    info["file_format"] = "ERDAS IMG"
                elif ".asc" in src:
                    info["file_format"] = "ASCII Grid"
                else:
                    info["file_format"] = "other"
            else:
                continue

            layers.append(info)
        return layers

    def get_algorithm_catalog(self, algorithm_ids=None):
        if algorithm_ids is None:
            algorithm_ids = CURATED_ALGORITHMS

        registry = QgsApplication.processingRegistry()
        catalog = {}

        for alg_id in algorithm_ids:
            alg = registry.algorithmById(alg_id)
            if alg is None:
                continue

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
                        json.dumps(p.defaultValue())
                        pinfo["default"] = p.defaultValue()
                    except (TypeError, ValueError):
                        pinfo["default"] = str(p.defaultValue())

                if isinstance(p, QgsProcessingParameterEnum):
                    pinfo["enum_options"] = p.options()
                    pinfo["uses_static_strings"] = p.usesStaticStrings()

                params.append(pinfo)

            catalog[alg_id] = {
                "name": alg.displayName(),
                "group": alg.group(),
                "parameters": params,
            }

        return catalog

    def build_full_context(self, selected_layer_ids=None):
        context = {
            "qgis_version": self.get_qgis_version(),
            "providers": self.get_providers_summary(),
            "layers": self.get_layer_catalog(),
            "algorithms": self.get_algorithm_catalog(),
        }
        if selected_layer_ids:
            context["selected_layers"] = [
                layer for layer in context["layers"] if layer["id"] in selected_layer_ids
            ]
        return context

    def context_to_prompt_text(self, context, max_chars=8000):
        lines = []
        lines.append(f"QGIS Version: {context['qgis_version']}")
        lines.append(f"Providers: {', '.join(p['id'] for p in context['providers'])}")
        lines.append("")

        lines.append("=== LOADED LAYERS ===")
        for layer in context.get("selected_layers", context.get("layers", [])):
            if layer.get("layer_type") == "vector":
                fields_str = ", ".join(
                    f"{f['name']}({f['type']})" for f in layer.get("fields", [])[:20]
                )
                lines.append(
                    f"  [Vector/{layer['geometry_type']}] \"{layer['name']}\" "
                    f"CRS={layer['crs']} features={layer.get('feature_count', '?')} "
                    f"storage={layer.get('storage_type', '?')} "
                    f"fields=[{fields_str}]"
                )
            elif layer.get("layer_type") == "raster":
                bands_str = ", ".join(
                    f"band{b['number']}(nodata={b.get('nodata', 'none')})"
                    for b in layer.get("bands", [])[:5]
                )
                lines.append(
                    f"  [Raster] \"{layer['name']}\" "
                    f"CRS={layer['crs']} {layer.get('width', '?')}x{layer.get('height', '?')} "
                    f"format={layer.get('file_format', '?')} "
                    f"bands=[{bands_str}]"
                )
        lines.append("")

        lines.append("=== AVAILABLE ALGORITHMS (with exact parameter signatures) ===")
        for alg_id, alg_info in context.get("algorithms", {}).items():
            params_brief = []
            for p in alg_info["parameters"]:
                flag = ""
                if p.get("is_destination"):
                    flag = " [OUTPUT]"
                elif p.get("optional"):
                    flag = " [opt]"

                enum_str = ""
                if "enum_options" in p:
                    opts = p["enum_options"]
                    indexed = [f"{i}={opts[i]}" for i in range(len(opts))]
                    enum_str = " ENUM[" + ", ".join(indexed) + "]"

                default_str = ""
                if "default" in p:
                    default_str = f" default={p['default']}"

                params_brief.append(
                    f"{p['name']}:{p['type']}{enum_str}{default_str}{flag}"
                )
            params_str = ", ".join(params_brief)
            lines.append(f"  {alg_id}: {alg_info['name']} -> ({params_str})")

        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... [truncated]"
        return text

    def collect(self, layers=None, algo_config=None, max_chars=8000):
        selected_ids = None
        if layers:
            selected_ids = [lyr.id() for lyr in layers]

        context = self.build_full_context(selected_layer_ids=selected_ids)
        return self.context_to_prompt_text(context, max_chars=max_chars)
