from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QDockWidget, QWidget, QVBoxLayout
from .forge_widget import ForgeWidget


class ForgeDock(QDockWidget):

    def __init__(self, iface, plugin=None, parent=None):
        super().__init__("Model Forge Linked", parent)
        self.iface = iface
        self.plugin = plugin
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.setMinimumWidth(480)
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
