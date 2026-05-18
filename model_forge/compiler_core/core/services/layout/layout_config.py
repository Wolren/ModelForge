"""
LayoutConfig - layout configuration dataclass for GraphLayoutService.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class LayoutConfig:
    h_spacing: float = 300.0   # horizontal gap between ranks (px)
    v_spacing: float = 120.0   # vertical gap between nodes in same rank
    input_x: float = 120.0    # x position of model inputs column
    start_x: float = 400.0    # x origin of first algorithm rank

    @classmethod
    def compact(cls) -> "LayoutConfig":
        return cls(h_spacing=280.0, v_spacing=100.0, input_x=100.0, start_x=360.0)

    @classmethod
    def balanced(cls) -> "LayoutConfig":
        return cls(h_spacing=300.0, v_spacing=120.0, input_x=120.0, start_x=400.0)

    @classmethod
    def dense(cls) -> "LayoutConfig":
        return cls(h_spacing=250.0, v_spacing=80.0, input_x=80.0, start_x=320.0)

    @classmethod
    def spacious(cls) -> "LayoutConfig":
        return cls(h_spacing=520.0, v_spacing=180.0, input_x=140.0, start_x=480.0)

    @classmethod
    def debug(cls) -> "LayoutConfig":
        return cls(h_spacing=420.0, v_spacing=160.0, input_x=120.0, start_x=420.0)