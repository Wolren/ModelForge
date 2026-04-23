"""
GraphLayoutService - legacy import compatibility layer.
Import from layout_config and layout_service instead.
"""
from .layout_config import LayoutConfig
from .layout_service import GraphLayoutService

__all__ = ["LayoutConfig", "GraphLayoutService"]