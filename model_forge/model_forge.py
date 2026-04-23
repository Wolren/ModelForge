import os
import importlib.util
import sys
from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction
from .forge_dock import ForgeDock

try:
    from qgis.core import QgsApplication, QgsProcessingAlgorithm
    _HAS_QGIS = True
except ImportError:
    _HAS_QGIS = False


class ModelForge:

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu_name = u"&Model Forge"
        self.dock = None
        self._provider = None

    def tr(self, message):
        return QCoreApplication.translate("ModelForge", message)

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, "legacy_ui", "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon.fromTheme("panel-show")
        self.action_open = QAction(icon, self.tr(u"Model Forge"), self.iface.mainWindow())
        self.action_open.setStatusTip(self.tr("Open Model Forge panel"))
        self.action_open.triggered.connect(self.toggle_dock)
        self.iface.addToolBarIcon(self.action_open)
        self.iface.addPluginToMenu(self.menu_name, self.action_open)
        self.actions.append(self.action_open)
        self._init_processing()

    def unload(self):
        if self.dock:
            self.iface.removeDockWidget(self.dock)
            self.dock.close()
            self.dock = None
        for action in self.actions:
            self.iface.removePluginMenu(self.menu_name, action)
            self.iface.removeToolBarIcon(action)
        if _HAS_QGIS and self._provider:
            QgsApplication.processingRegistry().removeProvider(self._provider)
            self._provider = None

    def toggle_dock(self):
        if self.dock:
            self.iface.removeDockWidget(self.dock)
            self.dock.close()
            self.dock = None
        self.dock = ForgeDock(self.iface, self, self.iface.mainWindow())
        self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock)
        self.dock.show()

    def _init_processing(self):
        if not _HAS_QGIS or self._provider is not None:
            return
        from .compiler_core.provider.model_forge_provider import ModelForgeProvider
        self._provider = ModelForgeProvider()
        QgsApplication.processingRegistry().addProvider(self._provider)
        self._load_user_algorithms()

    def _load_user_algorithms(self):
        from .compiler_core.core.services.generation.custom_step_author import _STEPS_DIR
        import warnings

        if not os.path.isdir(_STEPS_DIR):
            return

        for fname in sorted(os.listdir(_STEPS_DIR)):
            if not fname.endswith(".py"):
                continue
            if fname == "__init__.py":
                continue
            mod_path = os.path.join(_STEPS_DIR, fname)
            mod_name = f"{__package__}.compiler_core.user_steps.{fname[:-3]}"
            try:
                self.register_generated_step(mod_path, module_name=mod_name)
            except Exception as e:
                warnings.warn(f"ModelForge: failed to register user step {fname}: {e}")

    def register_generated_step(self, py_path, module_name=None):
        if not _HAS_QGIS:
            raise RuntimeError("QGIS runtime is required for provider registration.")
        if self._provider is None:
            self._init_processing()

        module_name = module_name or f"{__package__}.compiler_core.user_steps.{os.path.basename(py_path)[:-3]}"
        spec = importlib.util.spec_from_file_location(module_name, py_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not load generated step module: {py_path}")

        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)

        found_alg = None
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, QgsProcessingAlgorithm)
                and attr is not QgsProcessingAlgorithm
            ):
                found_alg = attr()
                break

        if found_alg is None:
            raise RuntimeError(f"No QgsProcessingAlgorithm subclass found in: {py_path}")

        self._provider.register_user_algorithm(found_alg)
        return f"{self._provider.id()}:{found_alg.name()}"
