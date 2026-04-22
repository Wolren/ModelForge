"""
ModelForge Plugin
=================
QGIS plugin entrypoint.
"""


def classFactory(iface):
    from .model_forge_plugin import ModelForgePlugin
    return ModelForgePlugin(iface)
