"""
AutoLayoutAlgorithm
===================
Processing algorithm that applies GraphLayoutService to an existing model JSON
string and returns the re-laid-out JSON. Can be called from scripts or the
ModelForge Designer.
"""
from __future__ import annotations

try:
    from qgis.core import (
        QgsProcessingAlgorithm,
        QgsProcessingParameterString,
        QgsProcessingParameterEnum,
        QgsProcessingOutputString,
        QgsProcessingException,
    )
    from qgis.PyQt.QtCore import QCoreApplication
    _HAS_QGIS = True
except ImportError:
    _HAS_QGIS = False

if _HAS_QGIS:
    import json

    class AutoLayoutAlgorithm(QgsProcessingAlgorithm):

        def tr(self, message: str) -> str:
            return QCoreApplication.translate("AutoLayoutAlgorithm", message)

        INPUT_JSON   = "INPUT_JSON"
        LAYOUT_MODE  = "LAYOUT_MODE"
        OUTPUT_JSON  = "OUTPUT_JSON"

        def name(self)        -> str: return "mcp_auto_layout"
        def displayName(self) -> str: return self.tr("Auto-Layout Model Graph (MCP)")
        def group(self)       -> str: return self.tr("ModelForge")
        def groupId(self)     -> str: return "model_forge"

        def createInstance(self): return AutoLayoutAlgorithm()

        def initAlgorithm(self, config=None):
            self.addParameter(QgsProcessingParameterString(
                self.INPUT_JSON, self.tr("Model JSON"), multiLine=True))
            self.addParameter(QgsProcessingParameterEnum(
                self.LAYOUT_MODE, self.tr("Layout mode"),
                options=["compact", "balanced", "dense", "spacious", "debug"],
                defaultValue=1,
            ))
            self.addOutput(QgsProcessingOutputString(
                self.OUTPUT_JSON, self.tr("Re-laid-out model JSON")))

        def processAlgorithm(self, parameters, context, feedback):
            raw_json   = self.parameterAsString(parameters, self.INPUT_JSON,   context)
            layout_idx = self.parameterAsEnum  (parameters, self.LAYOUT_MODE,  context)
            modes      = ["compact", "balanced", "dense", "spacious", "debug"]
            mode       = modes[layout_idx]

            try:
                model_json = json.loads(raw_json)
            except json.JSONDecodeError as e:
                raise QgsProcessingException(f"Invalid JSON: {e}") from e

            from ...core.services.graph_layout import GraphLayoutService
            svc        = GraphLayoutService()
            result     = svc.layout_model_json(model_json, mode=mode)
            result_str = json.dumps(result, indent=2, ensure_ascii=False)
            feedback.pushInfo(f"Applied {mode} layout to model with "
                              f"{len(result.get('algorithms', []))} steps.")
            return {self.OUTPUT_JSON: result_str}

else:
    class AutoLayoutAlgorithm:
        pass
