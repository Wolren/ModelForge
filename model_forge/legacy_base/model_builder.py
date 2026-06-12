"""
Converts LLM-generated workflow JSON into a QgsProcessingModelAlgorithm.
Includes validation pass and error collection.
"""

import logging

log = logging.getLogger(__name__)

from qgis.core import (
    QgsProcessing,
    QgsProcessingModelAlgorithm,
    QgsProcessingModelChildAlgorithm,
    QgsProcessingModelChildParameterSource,
    QgsProcessingModelParameter,
    QgsProcessingParameterBand,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterCrs,
    QgsProcessingParameterEnum,
    QgsProcessingParameterExtent,
    QgsProcessingParameterField,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterNumber,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterString,
    QgsProcessingParameterVectorLayer,
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
    "layer": QgsProcessingParameterVectorLayer,
}

GEOMETRY_TYPE_MAP = {
    -1: QgsProcessing.TypeVectorAnyGeometry,
    0: QgsProcessing.TypeVectorPoint,
    1: QgsProcessing.TypeVectorLine,
    2: QgsProcessing.TypeVectorPolygon,
}


class ModelBuilder:
    X_START = 200
    Y_START = 50
    Y_STEP = 120

    def build_model(self, workflow, model_name="generated_model", model_group="Model Forge"):
        model = QgsProcessingModelAlgorithm()
        model.setName(model_name)
        model.setGroup(model_group)
        self._add_inputs(model, workflow.get("inputs", []))
        self._add_algorithms(model, workflow.get("algorithms", []))
        model.updateDestinationParameters()
        return model

    def validate_model(self, model):
        errors = []
        try:
            result = model.validate()
            if isinstance(result, tuple):
                is_valid, issues = result
                if not is_valid:
                    for issue in issues:
                        msg = issue.message() if hasattr(issue, "message") else str(issue)
                        if msg:
                            errors.append(msg)
            elif isinstance(result, list):
                for issue in result:
                    msg = issue.message() if hasattr(issue, "message") else str(issue)
                    if msg:
                        errors.append(msg)
        except Exception as e:
            errors.append("Validation call failed: " + str(e))

        for child_id in model.childAlgorithms():
            try:
                child_result = model.validateChildAlgorithm(child_id)
                if isinstance(child_result, tuple):
                    child_valid, child_issues = child_result
                    if not child_valid:
                        for issue in child_issues:
                            msg = issue.message() if hasattr(issue, "message") else str(issue)
                            if msg:
                                errors.append("[" + child_id + "] " + msg)
                elif isinstance(child_result, list):
                    for issue in child_result:
                        msg = issue.message() if hasattr(issue, "message") else str(issue)
                        if msg:
                            errors.append("[" + child_id + "] " + msg)
            except Exception:
                log.warning("Failed to validate child %s", child_id)
                pass

        return errors

    def _add_inputs(self, model, inputs):
        x = self.X_START - 250
        y = self.Y_START

        for inp in inputs:
            name = inp["name"]
            label = inp.get("label", name)
            inp_type = inp.get("type", "vector")

            param_class = INPUT_TYPE_MAP.get(inp_type, QgsProcessingParameterString)

            if inp_type in ("vector", "layer"):
                geom = inp.get("geometry", -1)
                param_def = param_class(
                    name,
                    label,
                    types=[GEOMETRY_TYPE_MAP.get(geom, QgsProcessing.TypeVectorAnyGeometry)],
                )
            elif inp_type == "raster":
                param_def = param_class(name, label)
            elif inp_type == "number":
                param_def = param_class(
                    name,
                    label,
                    type=QgsProcessingParameterNumber.Double,
                    defaultValue=inp.get("default", 0),
                )
            elif inp_type == "field":
                param_def = param_class(name, label, parentLayerParameterName=inp.get("parent", ""))
            elif inp_type == "multilayer":
                param_def = param_class(name, label, layerType=QgsProcessing.TypeVectorAnyGeometry)
            elif inp_type == "boolean":
                param_def = param_class(name, label, defaultValue=inp.get("default", False))
            elif inp_type == "crs":
                param_def = param_class(name, label, defaultValue=inp.get("default", "EPSG:4326"))
            elif inp_type == "enum":
                param_def = param_class(
                    name,
                    label,
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
                    QgsProcessingModelChildParameterSource.fromModelParameter(source_def["name"])
                ]
            elif src_type == "child_output":
                return [
                    QgsProcessingModelChildParameterSource.fromChildOutput(
                        source_def["child_id"], source_def["output_name"]
                    )
                ]
            elif src_type == "expression":
                return [
                    QgsProcessingModelChildParameterSource.fromExpression(source_def["expression"])
                ]
            elif src_type == "static":
                return [
                    QgsProcessingModelChildParameterSource.fromStaticValue(source_def.get("value"))
                ]

        elif isinstance(source_def, (str, int, float, bool)):
            return [QgsProcessingModelChildParameterSource.fromStaticValue(source_def)]

        return None
