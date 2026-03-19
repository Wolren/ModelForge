import os
from qgis.PyQt.QtCore import QSettings, QCoreApplication, Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction
from .forge_dock import ForgeDock


class ModelForge:
    """Model Forge - Generate Processing models from text descriptions"""

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu_name = u'&Model Forge'
        self.dock = None

    def tr(self, message):
        return QCoreApplication.translate('ModelForge', message)

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, 'icon.png')
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon.fromTheme('panel-show')

        self.action_open = QAction(
            icon,
            self.tr(u'Model Forge'),
            self.iface.mainWindow()
        )
        self.action_open.setStatusTip(self.tr('Open Model Forge panel'))
        self.action_open.triggered.connect(self.toggle_dock)

        self.iface.addToolBarIcon(self.action_open)
        self.iface.addPluginToMenu(self.menu_name, self.action_open)
        self.actions.append(self.action_open)

    def unload(self):
        if self.dock:
            self.iface.removeDockWidget(self.dock)
            self.dock.close()
            self.dock = None
        for action in self.actions:
            self.iface.removePluginMenu(self.menu_name, action)
            self.iface.removeToolBarIcon(action)

    def toggle_dock(self):
        if self.dock:
            self.iface.removeDockWidget(self.dock)
            self.dock.close()
            self.dock = None
        self.dock = ForgeDock(self.iface, self.iface.mainWindow())
        self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock)
        self.dock.show()
