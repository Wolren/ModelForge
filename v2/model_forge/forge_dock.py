from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QDockWidget, QWidget, QVBoxLayout
from .forge_widget import ForgeWidget


class ForgeDock(QDockWidget):
    """Dockable wrapper for Model Forge"""

    def __init__(self, iface, parent=None):
        super().__init__("Model Forge", parent)
        self.iface = iface
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.setMinimumWidth(480)

        main_widget = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(4, 4, 4, 4)
        self.forge_widget = ForgeWidget(iface)
        layout.addWidget(self.forge_widget)
        main_widget.setLayout(layout)
        self.setWidget(main_widget)

    def closeEvent(self, event):
        self.forge_widget.disconnect_signals()
        event.accept()
