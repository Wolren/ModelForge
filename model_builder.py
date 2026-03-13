"""
Converts the LLM-generated workflow JSON into a QgsProcessingModelAlgorithm
that can be saved as .model3 and opened in the QGIS Model Designer.
"""

from qgis.core import (
    QgsProcessingModelAlgorithm,
    QgsProcessingModelChildAlgorithm,
    QgsProcessingModelChildParameterSource,
    QgsProcessingModelParameter,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsProcessingParameterField,
    QgsProcessingParameterEnum,
    QgsProcessingParameterCrs,
    QgsProcessingParameterExtent,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterBand,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterRasterDestination,
    QgsProcessingModelOutput,
    QgsProcessing,
    QgsApplication,
)
from qgis.PyQt.QtCore import QPointF


INPUT_TYPE_MAP = {
    "vector": QgsProcessingParameterVectorLayer,
    "raster": QgsProcessingParameterRasterLayer,
    "number": QgsProcessingParameterNumber,
    "string": QgsProcessingParameterString,
    "field": QgsProcessingParameterField,
    "enum": QgsProcessingParameterEnum,
    "crs": QgsProcessingParameterCrs,
    "extent": QgsProcessingParameterExtent,
    "boolean": QgsProcessingParameterBoolean,
    "multilayer": QgsProcessingParameterMultipleLayers,
    "band": QgsProcessingParameterBand,
}

GEOMETRY_TYPE_MAP = {
    -1: QgsProcessing.TypeVectorAnyGeometry,
    0: QgsProcessing.TypeVectorPoint,
    1: QgsProcessing.TypeVectorLine,
    2: QgsProcessing.TypeVectorPolygon,
}


class ModelBuilder:
    """Builds a QgsProcessingModelAlgorithm from a structured workflow dict."""

    X_START = 200
    Y_START = 50
    X_STEP = 0
    Y_STEP = 120

    def build_model(self, workflow, model_name="generated_model", model_group="Model Forge"):
        model = QgsProcessingModelAlgorithm()
        model.setName(model_name)
        model.setGroup(model_group)

        self._add_inputs(model, workflow.get("inputs", []))
        self._add_algorithms(model, workflow.get("algorithms", []))

        model.updateDestinationParameters()
        return model

    def _add_inputs(self, model, inputs):
        x = self.X_START - 250
        y = self.Y_START

        for inp in inputs:
            name = inp["name"]
            label = inp.get("label", name)
            inp_type = inp.get("type", "vector")

            param_class = INPUT_TYPE_MAP.get(inp_type, QgsProcessingParameterString)

            kwargs = {}

            if inp_type == "vector":
                geom = inp.get("geometry", -1)
                kwargs["types"] = [GEOMETRY_TYPE_MAP.get(geom, QgsProcessing.TypeVectorAnyGeometry)]
                param_def = param_class(name, label, **kwargs)
            elif inp_type == "raster":
                param_def = param_class(name, label)
            elif inp_type == "number":
                param_def = param_class(
                    name, label,
                    type=QgsProcessingParameterNumber.Double,
                    defaultValue=inp.get("default", 0),
                )
            elif inp_type == "field":
                param_def = param_class(name, label, parentLayerParameterName=inp.get("parent", ""))
                param_def.setAllowMultiple(False)
            elif inp_type == "multilayer":
                param_def = param_class(name, label, layerType=QgsProcessing.TypeVectorAnyGeometry)
            elif inp_type == "boolean":
                param_def = param_class(name, label, defaultValue=inp.get("default", False))
            elif inp_type == "crs":
                param_def = param_class(name, label, defaultValue=inp.get("default", "EPSG:4326"))
            elif inp_type == "enum":
                param_def = param_class(
                    name, label,
                    options=inp.get("options", []),
                    defaultValue=inp.get("default", 0),
                )
            else:
                param_def = param_class(name, label, defaultValue=inp.get("default", ""))

            component = QgsProcessingModelParameter(name)
            component.setPosition(QPointF(x, y))
            component.setDescription(label)

            model.addModelParameter(param_def, component)
            y += self.Y_STEP

    def _add_algorithms(self, model, algorithms):
        x = self.X_START
        y = self.Y_START

        for alg_def in algorithms:
            child = QgsProcessingModelChildAlgorithm(alg_def["algorithm_id"])
            child.setChildId(alg_def["id"])
            child.setDescription(alg_def.get("description", alg_def["id"]))
            child.setPosition(QPointF(x, y))

            for param_name, source_def in alg_def.get("parameters", {}).items():
                sources = self._resolve_source(source_def)
                if sources is not None:
                    child.addParameterSources(param_name, sources)

            if "outputs" in alg_def:
                for output_key, output_info in alg_def["outputs"].items():
                    model_output = QgsProcessingModelOutput(output_key)
                    model_output.setDescription(
                        output_info.get("label", output_key) if isinstance(output_info, dict) else str(output_info)
                    )
                    model_output.setChildId(alg_def["id"])
                    model_output.setChildOutputName(output_key)
                    model_output.setPosition(QPointF(x + 250, y))
                    child.setModelOutput(output_key, model_output)

            model.addChildAlgorithm(child)
            y += self.Y_STEP

    def _resolve_source(self, source_def):
        if isinstance(source_def, list):
            all_sources = []
            for sd in source_def:
                resolved = self._resolve_source(sd)
                if resolved:
                    all_sources.extend(resolved)
            return all_sources if all_sources else None

        if isinstance(source_def, dict):
            src_type = source_def.get("type", "static")

            if src_type == "model_input":
                return [
                    QgsProcessingModelChildParameterSource.fromModelParameter(
                        source_def["name"]
                    )
                ]
            elif src_type == "child_output":
                return [
                    QgsProcessingModelChildParameterSource.fromChildOutput(
                        source_def["child_id"],
                        source_def["output_name"],
                    )
                ]
            elif src_type == "expression":
                return [
                    QgsProcessingModelChildParameterSource.fromExpression(
                        source_def["expression"]
                    )
                ]
            elif src_type == "static":
                return [
                    QgsProcessingModelChildParameterSource.fromStaticValue(
                        source_def.get("value")
                    )
                ]
        elif isinstance(source_def, (str, int, float, bool)):
            return [
                QgsProcessingModelChildParameterSource.fromStaticValue(source_def)
            ]

        return None
