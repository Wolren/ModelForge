"""Style templates: color palettes, font stacks, default sizes per template.

Used by ``qml_builder`` (per-layer-type default symbology) and
``qpt_builder`` (print layout). Each template is a flat dict so
callers don't have to know which fields are optional.

Template choices are biased toward legibility at A4/Letter print
size; the LLM is the one picking the template + tweaking colors
based on user intent ("scientific paper", "client presentation",
"internal report"), and the verifier catches regressions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# --- Page sizes in millimetres (QGIS's print layout uses mm) --------

PAGE_SIZES_MM: dict[str, tuple[float, float]] = {
    "A4_portrait": (210.0, 297.0),
    "A4_landscape": (297.0, 210.0),
    "A3_portrait": (297.0, 420.0),
    "A3_landscape": (420.0, 297.0),
    "Letter_portrait": (215.9, 279.4),
    "Letter_landscape": (279.4, 215.9),
    "16_9_landscape": (297.0, 167.0),  # ~ 16:9 of A4 width
}


# --- Per-template theme --------------------------------------------


@dataclass(frozen=True)
class PrintTemplate:
    name: str
    page: str  # key into PAGE_SIZES_MM
    palette: list[str]  # hex colors, used for map / legend / scale bar
    primary_color: str  # title text
    secondary_color: str  # subtitle / metadata
    background: str  # page background
    font_family: str  # title / body
    mono_family: str  # legend / scale text
    title_size: float  # mm
    subtitle_size: float  # mm
    margin_mm: float  # top/right/bottom/left margin
    include_legend: bool = True
    include_scale_bar: bool = True
    include_north_arrow: bool = True
    include_grid: bool = True
    include_atlas: bool = False
    metadata_block: bool = False  # scientific-style: date / CRS / scale row
    notes: str = ""


# Built-in templates. The LLM can request a name; missing names
# fall back to "default".
DEFAULT_TEMPLATES: dict[str, PrintTemplate] = {
    "default": PrintTemplate(
        name="default",
        page="A4_portrait",
        palette=[
            "#1f77b4",
            "#ff7f0e",
            "#2ca02c",
            "#d62728",
            "#9467bd",
            "#8c564b",
            "#e377c2",
            "#7f7f7f",
            "#bcbd22",
            "#17becf",
        ],
        primary_color="#1a1a1a",
        secondary_color="#595959",
        background="#ffffff",
        font_family="DejaVu Sans",
        mono_family="DejaVu Sans Mono",
        title_size=8.0,
        subtitle_size=5.0,
        margin_mm=10.0,
        notes="A4 portrait with full legend + scale bar + north arrow.",
    ),
    "scientific": PrintTemplate(
        name="scientific",
        page="Letter_portrait",
        palette=[
            "#000000",
            "#404040",
            "#808080",
            "#bfbfbf",
            "#ffffff",
        ],
        primary_color="#000000",
        secondary_color="#404040",
        background="#ffffff",
        font_family="Liberation Serif",
        mono_family="Liberation Mono",
        title_size=7.0,
        subtitle_size=4.0,
        margin_mm=15.0,
        include_grid=True,
        metadata_block=True,
        notes="Letter portrait, monochrome, metadata block (date / CRS / scale).",
    ),
    "presentation": PrintTemplate(
        name="presentation",
        page="16_9_landscape",
        palette=[
            "#2c3e50",
            "#e74c3c",
            "#3498db",
            "#f39c12",
            "#27ae60",
            "#9b59b6",
            "#1abc9c",
            "#34495e",
        ],
        primary_color="#2c3e50",
        secondary_color="#7f8c8d",
        background="#ffffff",
        font_family="DejaVu Sans",
        mono_family="DejaVu Sans Mono",
        title_size=12.0,
        subtitle_size=6.0,
        margin_mm=8.0,
        include_legend=True,
        include_scale_bar=False,  # 16:9 - at-a-glance map, no scale
        include_north_arrow=True,
        include_grid=False,
        notes="16:9 landscape, large map, no scale bar.",
    ),
    "minimal": PrintTemplate(
        name="minimal",
        page="A4_landscape",
        palette=["#333333", "#888888", "#bbbbbb", "#dddddd"],
        primary_color="#222222",
        secondary_color="#666666",
        background="#ffffff",
        font_family="DejaVu Sans",
        mono_family="DejaVu Sans Mono",
        title_size=5.0,
        subtitle_size=3.5,
        margin_mm=12.0,
        include_legend=False,
        include_scale_bar=True,
        include_north_arrow=True,
        include_grid=False,
        notes="A4 landscape, title + map + scale bar + north arrow only.",
    ),
}


def get_template(name: str) -> PrintTemplate:
    """Return a PrintTemplate by name; fall back to ``default``."""
    return DEFAULT_TEMPLATES.get(name, DEFAULT_TEMPLATES["default"])


# --- Per-geometry-type default colors (for qml_builder) -----------


# 6-color blind-friendly palette. Order matters: the qml_builder
# uses it as the default for single-symbol fill colors.
DEFAULT_VECTOR_PALETTE: list[str] = [
    "#1f77b4",
    "#ff7f0e",
    "#2ca02c",
    "#d62728",
    "#9467bd",
    "#8c564b",
]


# Symbol defaults per layer type. The LLM can override these
# when generating a model.
@dataclass(frozen=True)
class SymbolDefaults:
    fill_color: str = "#1f77b4"
    stroke_color: str = "#000000"
    stroke_width: float = 0.26
    point_size: float = 2.0
    point_shape: str = "circle"
    label_color: str = "#000000"
    label_size: float = 10.0
    opacity: float = 1.0


DEFAULT_SYMBOLS: dict[str, SymbolDefaults] = {
    "polygon": SymbolDefaults(
        fill_color="#1f77b4",
        stroke_color="#1a1a1a",
        stroke_width=0.26,
        opacity=0.7,
    ),
    "line": SymbolDefaults(
        fill_color="#1f77b4",
        stroke_color="#1f77b4",
        stroke_width=1.0,
        opacity=1.0,
    ),
    "point": SymbolDefaults(
        fill_color="#e74c3c",
        stroke_color="#7f1d12",
        point_size=2.5,
        point_shape="circle",
        opacity=0.9,
    ),
    "raster": SymbolDefaults(
        fill_color="#3b3b3b",
        stroke_color="#000000",
        stroke_width=0.0,
        opacity=1.0,
    ),
}


def get_symbol_defaults(geometry_kind: str) -> SymbolDefaults:
    """Return the default ``SymbolDefaults`` for a geometry kind."""
    return DEFAULT_SYMBOLS.get(geometry_kind, DEFAULT_SYMBOLS["polygon"])


# --- Verifier rule limits (used by layout_verifier.py) -------------


@dataclass(frozen=True)
class VerifierLimits:
    min_title_size_mm: float = 4.0
    max_title_size_mm: float = 30.0
    min_subtitle_size_mm: float = 3.0
    min_scale_bar_length_mm: float = 15.0
    min_north_arrow_size_mm: float = 5.0
    min_legend_size_mm: float = 20.0
    require_metadata_in: tuple[str, ...] = ("scientific",)


VERIFIER_LIMITS = VerifierLimits()


# --- Convenience: serialize a PrintTemplate as a plain dict ------


def template_to_dict(t: PrintTemplate) -> dict[str, Any]:
    return {
        "name": t.name,
        "page": t.page,
        "page_size_mm": PAGE_SIZES_MM[t.page],
        "palette": list(t.palette),
        "primary_color": t.primary_color,
        "secondary_color": t.secondary_color,
        "background": t.background,
        "font_family": t.font_family,
        "mono_family": t.mono_family,
        "title_size": t.title_size,
        "subtitle_size": t.subtitle_size,
        "margin_mm": t.margin_mm,
        "include_legend": t.include_legend,
        "include_scale_bar": t.include_scale_bar,
        "include_north_arrow": t.include_north_arrow,
        "include_grid": t.include_grid,
        "include_atlas": t.include_atlas,
        "metadata_block": t.metadata_block,
        "notes": t.notes,
    }


__all__ = [
    "PAGE_SIZES_MM",
    "DEFAULT_TEMPLATES",
    "DEFAULT_VECTOR_PALETTE",
    "DEFAULT_SYMBOLS",
    "PrintTemplate",
    "SymbolDefaults",
    "VerifierLimits",
    "VERIFIER_LIMITS",
    "get_template",
    "get_symbol_defaults",
    "template_to_dict",
]
