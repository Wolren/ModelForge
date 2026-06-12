"""
Model Forge - Dock Widget

⚠️  EXPERIMENTAL PROJECT
This plugin is experimental. Features may change, break, or be removed without notice.
Links and documentation may become outdated or broken.
Use at your own risk.
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QDockWidget, QVBoxLayout, QWidget

from .forge_widget import ForgeWidget


class ForgeDock(QDockWidget):
    def __init__(self, iface, plugin=None, parent=None):
        super().__init__("Model Forge", parent)
        self.iface = iface
        self.plugin = plugin
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.setMinimumWidth(420)
        main_widget = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        self.forge_widget = ForgeWidget(iface, plugin=plugin)
        layout.addWidget(self.forge_widget)
        main_widget.setLayout(layout)
        self.setWidget(main_widget)

    def closeEvent(self, event, **kwargs):
        self.forge_widget.disconnect_signals()
        event.accept()
