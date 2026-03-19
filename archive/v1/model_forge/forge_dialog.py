from qgis.PyQt.QtWidgets import QDialog, QVBoxLayout
from .forge_widget import ForgeWidget


class ForgeDialog(QDialog):
    """Dialog window containing the forge widget"""

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface

        self.setWindowTitle("Model Forge")
        self.setMinimumWidth(640)
        self.setMinimumHeight(720)

        layout = QVBoxLayout()
        layout.setContentsMargins(10, 10, 10, 10)

        self.forge_widget = ForgeWidget(iface)
        layout.addWidget(self.forge_widget)

        self.setLayout(layout)

    def closeEvent(self, event):
        self.forge_widget.disconnect_signals()
        event.accept()
