def classFactory(iface):
    from .model_forge import ModelForge
    return ModelForge(iface)
