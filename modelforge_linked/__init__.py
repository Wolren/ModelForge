def classFactory(iface):
    from .model_forge import ModelForgeLinked
    return ModelForgeLinked(iface)
