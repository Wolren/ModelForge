import os
from qgis.PyQt.QtCore import QSettings, QCoreApplication, Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from .forge_dialog import ForgeDialog
from .forge_dock import ForgeDock


class ModelForge:
    """Model Forge - Generate Processing models from text descriptions"""

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu_name = u'&Model Forge'
        self.window = None

    def tr(self, message):
        return QCoreApplication.translate('ModelForge', message)

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, 'icon.png')

        dock_icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon.fromTheme('panel-show')
        window_icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon.fromTheme('window-new')

        self.action_dock = QAction(
            dock_icon,
            self.tr(u'Open as Dock'),
            self.iface.mainWindow()
        )
        self.action_dock.setStatusTip(self.tr('Open Model Forge as dockable panel'))
        self.action_dock.triggered.connect(self.toggle_dock_mode)

        self.action_window = QAction(
            window_icon,
            self.tr(u'Open as Window'),
            self.iface.mainWindow()
        )
        self.action_window.setStatusTip(self.tr('Open Model Forge as floating window'))
        self.action_window.triggered.connect(self.toggle_window_mode)

        self.iface.addToolBarIcon(self.action_dock)
        self.iface.addToolBarIcon(self.action_window)

        self.iface.addPluginToMenu(self.menu_name, self.action_dock)
        self.iface.addPluginToMenu(self.menu_name, self.action_window)

        self.actions.append(self.action_dock)
        self.actions.append(self.action_window)

    def unload(self):
        if self.window:
            if isinstance(self.window, ForgeDock):
                self.iface.removeDockWidget(self.window)
            self.window.close()
            self.window = None

        for action in self.actions:
            self.iface.removePluginMenu(self.menu_name, action)
            self.iface.removeToolBarIcon(action)

    def toggle_dock_mode(self):
        self.close_window()
        self.window = ForgeDock(self.iface, self.iface.mainWindow())
        self.iface.addDockWidget(Qt.RightDockWidgetArea, self.window)
        self.window.show()
        QSettings().setValue('ModelForge/use_dock', True)

    def toggle_window_mode(self):
        self.close_window()
        self.window = ForgeDialog(self.iface, self.iface.mainWindow())
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        QSettings().setValue('ModelForge/use_dock', False)

    def close_window(self):
        if self.window:
            if isinstance(self.window, ForgeDock):
                try:
                    self.iface.removeDockWidget(self.window)
                except:
                    pass
            self.window.close()
            self.window = None
