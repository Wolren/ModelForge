"""
ModelForgePlugin
================
QGIS plugin class - integrates all subsystems.
"""
from __future__ import annotations
import os

try:
    from qgis.PyQt.QtWidgets import QAction, QToolBar
    from qgis.PyQt.QtGui     import QIcon
    from qgis.core           import QgsApplication
    _HAS_QGIS = True
except ImportError:
    _HAS_QGIS = False


class ModelForgePlugin:

    def __init__(self, iface):
        self.iface   = iface
        self._provider = None
        self._actions  = []

    def initGui(self):
        if not _HAS_QGIS:
            return
        # MCP Workflow Builder action
        mcp_action = QAction(
            QIcon(os.path.join(os.path.dirname(__file__), "resources", "icons", "mcp.svg")),
            "Build Workflow (MCP)…",
            self.iface.mainWindow(),
        )
        mcp_action.triggered.connect(self._open_mcp_dialog)
        self.iface.addPluginToMenu("&ModelForge", mcp_action)
        self._actions.append(mcp_action)

        # Custom Step Author action
        step_action = QAction(
            QIcon(os.path.join(os.path.dirname(__file__), "resources", "icons", "custom_step.svg")),
            "Custom Step Author…",
            self.iface.mainWindow(),
        )
        step_action.triggered.connect(self._open_custom_step_dialog)
        self.iface.addPluginToMenu("&ModelForge", step_action)
        self._actions.append(step_action)

    def initProcessing(self):
        from .provider.model_forge_provider import ModelForgeProvider
        self._provider = ModelForgeProvider()
        QgsApplication.processingRegistry().addProvider(self._provider)
        self._load_user_algorithms()

    def _load_user_algorithms(self):
        """Dynamically import and register all generated user step .py files."""
        from .core.services.generation.custom_step_author import CustomStepAuthorService, _STEPS_DIR
        import importlib.util
        import sys

        svc = CustomStepAuthorService()
        for fname in os.listdir(_STEPS_DIR):
            if not fname.endswith(".py"):
                continue
            mod_path = os.path.join(_STEPS_DIR, fname)
            mod_name = f"model_forge.user_steps.{fname[:-3]}"
            try:
                spec = importlib.util.spec_from_file_location(mod_name, mod_path)
                mod  = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = mod
                spec.loader.exec_module(mod)
                # Find the QgsProcessingAlgorithm subclass in the module
                for attr_name in dir(mod):
                    attr = getattr(mod, attr_name)
                    try:
                        from qgis.core import QgsProcessingAlgorithm
                        if (isinstance(attr, type)
                                and issubclass(attr, QgsProcessingAlgorithm)
                                and attr is not QgsProcessingAlgorithm):
                            self._provider.register_user_algorithm(attr())
                    except Exception:
                        pass
            except Exception as e:
                import warnings
                warnings.warn(f"ModelForge: failed to load user step {fname}: {e}")

    def unload(self):
        if _HAS_QGIS:
            for action in self._actions:
                self.iface.removePluginMenu("&ModelForge", action)
        if self._provider:
            QgsApplication.processingRegistry().removeProvider(self._provider)

    def _open_mcp_dialog(self):
        from .ui.mcp_dialog import MCPDialog
        dlg = MCPDialog(self.iface, parent=self.iface.mainWindow())
        dlg.exec()

    def _open_custom_step_dialog(self):
        from .ui.custom_step_dialog import CustomStepDialog
        dlg = CustomStepDialog(parent=self.iface.mainWindow())
        dlg.exec()