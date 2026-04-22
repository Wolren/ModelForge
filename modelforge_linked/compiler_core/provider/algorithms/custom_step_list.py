"""
ListCustomStepsAlgorithm
========================
Simple algorithm to list all custom step specs saved in user_steps/.
"""
from __future__ import annotations

try:
    from qgis.core import (
        QgsProcessingAlgorithm,
        QgsProcessingOutputString,
    )
    from qgis.PyQt.QtCore import QCoreApplication
    _HAS_QGIS = True
except ImportError:
    _HAS_QGIS = False

if _HAS_QGIS:
    import json

    class ListCustomStepsAlgorithm(QgsProcessingAlgorithm):

        def tr(self, message: str) -> str:
            return QCoreApplication.translate("ListCustomStepsAlgorithm", message)

        OUTPUT_LIST = "OUTPUT_LIST"

        def name(self)        -> str: return "mcp_list_custom_steps"
        def displayName(self) -> str: return self.tr("List Custom Steps (MCP)")
        def group(self)       -> str: return self.tr("ModelForge")
        def groupId(self)     -> str: return "model_forge"

        def createInstance(self): return ListCustomStepsAlgorithm()

        def initAlgorithm(self, config=None):
            self.addOutput(QgsProcessingOutputString(self.OUTPUT_LIST, self.tr("Custom steps JSON")))

        def processAlgorithm(self, parameters, context, feedback):
            from ...core.services.custom_step_author import CustomStepAuthorService
            svc   = CustomStepAuthorService()
            specs = svc.list_specs()
            summary = [
                {
                    "step_id":      s.step_id,
                    "display_name": s.display_name,
                    "group":        s.group,
                    "params":       len(s.parameters),
                    "outputs":      len(s.outputs),
                    "version":      s.version,
                }
                for s in specs
            ]
            feedback.pushInfo(f"Found {len(specs)} custom step(s).")
            return {self.OUTPUT_LIST: json.dumps(summary, indent=2)}

else:
    class ListCustomStepsAlgorithm:
        pass
