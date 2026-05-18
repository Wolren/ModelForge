"""
Model Forge - QGIS Plugin

EXPERIMENTAL PROJECT — Features may change without notice. Links may break.

This plugin works with ANY OpenAI-compatible LLM provider (OpenAI, Ollama,
Anthropic, local models, custom endpoints). Unlike IntelliGeo and similar
projects, Model Forge is designed to be provider-agnostic from the ground up.
"""


def classFactory(iface):
    from .model_forge import ModelForge

    return ModelForge(iface)
