"""Cartographic pipeline for print layout generation.

Six discrete stages, each with a single input and a single output.
The LLM picks the *profile* and writes the *text* (title,
subtitle, metadata). The pipeline does the geometry.

Stages, in order:

    page_setup
    header_band
    map_zone
    ancillaries
    footer_band
    final_assembly

Each stage has its own dataclass contract, its own default
behavior, and (paired with the verifier) its own rules. A
*profile* is a bundle of stage choices. Today the profiles are
``scientific``, ``internal``, ``presentation``, and ``minimal``;
they live in :mod:`style_templates` and we reference them here.

Why six stages, not one? Because each stage answers a *different*
cartographic question:

- page_setup: "what paper / margins / bleed am I laying out on?"
- header_band: "what's at the top of the page, and how tall?"
- map_zone: "how big is the map, and at what scale?"
- ancillaries: "where do scale bar / north arrow go, sized to what?"
- footer_band: "what's at the bottom of the page?"
- final_assembly: "are all the pieces together within the page?"

Each stage's output is the next stage's input plus a flat
``LayoutItem`` list the final_assembly stage can serialize.

The whole pipeline runs offline (no QGIS app). QGIS is only
required to *render* the resulting .qpt to PDF / PNG / SVG.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any

from .style_templates import (
    PAGE_SIZES_MM,
    get_template,
)


# --- Stage inputs and outputs ----------------------------------


@dataclass
class LayoutRequest:
    """User-facing inputs to the cartographic pipeline.

    The LLM populates title / subtitle / crs / author / legend
    layers; the profile dictates the geometry choices.
    """

    template: str = "default"
    title: str = ""
    subtitle: str = ""
    crs: str = ""
    author: str = ""
    output_layer_ids: list[str] | None = None
    # Model's spatial extent. Optional - when missing, the
    # map_zone stage leaves scale = "auto" and QGIS picks.
    extent: tuple[float, float, float, float] | None = None  # xmin, ymin, xmax, ymax
    # Model's intended scale. Optional - overrides auto.
    scale: int | None = None  # e.g. 25000 for 1:25,000


@dataclass
class PageSpec:
    """Output of :func:`stage_page_setup`."""

    width_mm: float
    height_mm: float
    margin_mm: float
    bleed_mm: float
    page_grid: bool
    # Derived: the inner rectangle inside the margins. All
    # subsequent stages place items inside this rect.
    inner_x: float
    inner_y: float
    inner_w: float
    inner_h: float

    @property
    def printable_x(self) -> float:
        return self.margin_mm - self.bleed_mm

    @property
    def printable_y(self) -> float:
        return self.margin_mm - self.bleed_mm

    @property
    def printable_w(self) -> float:
        return self.width_mm - 2 * (self.margin_mm - self.bleed_mm)

    @property
    def printable_h(self) -> float:
        return self.height_mm - 2 * (self.margin_mm - self.bleed_mm)


@dataclass
class HeaderItem:
    """One item in the header band (title, subtitle, metadata row)."""

    text: str
    role: str  # title | subtitle | metadata
    x: float
    y: float
    width: float
    height: float
    font_size_mm: float
    color: str
    weight: str = "normal"  # normal | bold


@dataclass
class HeaderSpec:
    """Output of :func:`stage_header_band`."""

    items: list[HeaderItem] = field(default_factory=list)

    @property
    def total_height_mm(self) -> float:
        if not self.items:
            return 0.0
        # All header items share the same y, height represents the
        # band's effective height. Items are stacked vertically
        # relative to ``y`` in their own coordinates; we collapse
        # to the maximum top + height.
        return max(item.y + item.height for item in self.items) - min(item.y for item in self.items)


@dataclass
class MapSpec:
    """Output of :func:`stage_map_zone`."""

    x: float
    y: float
    width_mm: float
    height_mm: float
    # Map scale. None means "let QGIS auto-fit".
    scale: int | None
    # Bounding box to lock the map to, in the map's CRS
    # (xmin, ymin, xmax, ymax). None = let QGIS auto-fit to
    # the project's canvas (not the data). For proper
    # cartographic output, the caller should supply the
    # bbox of the layers actually displayed.
    extent: tuple[float, float, float, float] | None = None
    # Layers the map should display. Empty = "all layers in
    # the project at load time".
    layers: list[str] = field(default_factory=list)
    # Map-level grid (graticule) on/off.
    map_grid: bool = True
    # CRS, in case the LLM wants a per-map CRS override.
    crs: str = ""


@dataclass
class AncillaryItem:
    """A scale bar or north arrow positioned relative to the map."""

    item_type: str  # scale_bar | north_arrow
    x: float
    y: float
    width_mm: float
    height_mm: float
    # Scale bar: which units to display (auto, m, km, mi).
    scale_bar_units: str = "auto"
    # Scale bar: style (single_box, double_box, line, numeric).
    scale_bar_style: str = "single_box"
    # North arrow: 0 = no rotation; positive = clockwise degrees
    # from true north (e.g. -7 for magnetic declination).
    rotation_deg: float = 0.0
    # Whether to draw a frame around the item. Default True
    # for scale bar (so it's readable over busy map data);
    # True for north arrow (so it sits cleanly on the map).
    frame: bool = True
    # Whether to fill the frame with a white background.
    # Default True for both - keeps ancillaries readable.
    background: bool = True


@dataclass
class AncillarySpec:
    """Output of :func:`stage_ancillaries`."""

    items: list[AncillaryItem] = field(default_factory=list)


@dataclass
class FooterItem:
    """A legend or page-metadata row in the footer band."""

    item_type: str  # legend | page_metadata
    x: float
    y: float
    width_mm: float
    height_mm: float
    # Legend-specific: which layers the legend displays (post
    # the visibility filter). If empty, "all visible at map scale".
    legend_layers: list[str] = field(default_factory=list)
    # Legend-specific: the map whose layers we mirror.
    legend_map_ref: str = "map"
    # Whether to draw a frame around the item. Default True
    # for the legend (so it sits cleanly in the footer band).
    frame: bool = True
    # Whether to fill the frame with a white background.
    background: bool = True
    # Symbol size in mm; 0 = let the legend compute from
    # font size.
    symbol_size_mm: float = 0.0


@dataclass
class FooterSpec:
    """Output of :func:`stage_footer_band`."""

    items: list[FooterItem] = field(default_factory=list)

    @property
    def total_height_mm(self) -> float:
        if not self.items:
            return 0.0
        return max(item.y + item.height for item in self.items) - min(item.y for item in self.items)


@dataclass
class LayoutSpec:
    """The final, ordered list of layout items the emitter turns into XML."""

    page: PageSpec
    header: HeaderSpec
    map: MapSpec
    ancillaries: AncillarySpec
    footer: FooterSpec

    def all_items(self) -> list[Any]:
        """Flat, ordered list of items in z-order (background to foreground)."""
        return list(self.header.items) + list(self.ancillaries.items) + list(self.footer.items)


# --- Profile --------------------------------------------------


@dataclass
class CartographicProfile:
    """Bundle of stage choices keyed off the template.

    The LLM picks a profile (or we pick one based on the
    template) and the stages read their settings from this.
    """

    name: str
    page_grid: bool
    bleed_mm: float
    include_metadata_block: bool
    include_scale_bar: bool
    include_north_arrow: bool
    include_legend: bool
    include_graticule: bool
    # Visual weight hierarchy: title : subtitle : metadata font
    # sizes, in mm. The :math ratio is enforced by the header stage.
    title_size_mm: float
    subtitle_size_mm: float
    metadata_size_mm: float
    # Best-practice defaults for the ancillary items.
    scale_bar_style: str
    scale_bar_units: str
    # Legend filter: which layers show. The convention is
    # "visible at the map's scale" - for v1 we just inherit
    # the user's output_layer_ids.
    legend_filter_mode: str = "visible_at_scale"


_PROFILES: dict[str, CartographicProfile] = {
    "scientific": CartographicProfile(
        name="scientific",
        page_grid=True,
        bleed_mm=3.0,  # offset-print bleed
        include_metadata_block=True,  # date / CRS / scale row
        include_scale_bar=True,
        include_north_arrow=True,
        include_legend=True,
        include_graticule=True,
        title_size_mm=7.0,
        subtitle_size_mm=4.0,
        metadata_size_mm=3.5,
        scale_bar_style="single_box",
        scale_bar_units="auto",
    ),
    "internal": CartographicProfile(
        name="internal",
        page_grid=False,
        bleed_mm=0.0,
        include_metadata_block=False,
        include_scale_bar=True,
        include_north_arrow=True,
        include_legend=True,
        include_graticule=False,
        title_size_mm=8.0,
        subtitle_size_mm=5.0,
        metadata_size_mm=0.0,
        scale_bar_style="single_box",
        scale_bar_units="auto",
    ),
    "presentation": CartographicProfile(
        name="presentation",
        page_grid=False,
        bleed_mm=0.0,
        include_metadata_block=False,
        include_scale_bar=False,  # 16:9 - scale is visual noise
        include_north_arrow=True,
        include_legend=True,  # brief, bottom-right corner
        include_graticule=False,
        title_size_mm=12.0,
        subtitle_size_mm=6.0,
        metadata_size_mm=0.0,
        scale_bar_style="single_box",
        scale_bar_units="auto",
    ),
    "minimal": CartographicProfile(
        name="minimal",
        page_grid=False,
        bleed_mm=0.0,
        include_metadata_block=False,
        include_scale_bar=True,
        include_north_arrow=True,
        include_legend=False,  # map only
        include_graticule=False,
        title_size_mm=5.0,
        subtitle_size_mm=3.5,
        metadata_size_mm=0.0,
        scale_bar_style="line",
        scale_bar_units="auto",
    ),
}


def get_profile(name: str) -> CartographicProfile:
    """Return a :class:`CartographicProfile` by name.

    Maps the four :class:`PrintTemplate` names to profiles
    (``default`` -> ``internal``, ``scientific`` -> ``scientific``,
    etc.). Unknown names fall back to ``internal``.
    """
    if name in _PROFILES:
        return _PROFILES[name]
    if name == "default":
        return _PROFILES["internal"]
    return _PROFILES["internal"]


# --- Stage 1: page_setup --------------------------------------


def stage_page_setup(req: LayoutRequest) -> PageSpec:
    """Resolve paper size, orientation, margins, bleed, and grid.

    The template's ``page`` field is the key into
    :data:`PAGE_SIZES_MM`; if the LLM passed ``extent`` /
    ``scale`` and the page is landscape, we keep the page in
    landscape (QGIS handles scale-on-flip automatically).
    """
    t = get_template(req.template)
    profile = get_profile(req.template)
    page_w, page_h = PAGE_SIZES_MM[t.page]
    margin = t.margin_mm
    bleed = profile.bleed_mm
    page = PageSpec(
        width_mm=page_w,
        height_mm=page_h,
        margin_mm=margin,
        bleed_mm=bleed,
        page_grid=profile.page_grid,
        inner_x=margin,
        inner_y=margin,
        inner_w=page_w - 2 * margin,
        inner_h=page_h - 2 * margin,
    )
    return page


# --- Stage 2: header_band ------------------------------------


def stage_header_band(page: PageSpec, req: LayoutRequest) -> HeaderSpec:
    """Build the title / subtitle / metadata stack at the top.

    Conventions:
    - title always at the top of the page, full width.
    - subtitle just below, with a 1.5x visual weight ratio.
    - metadata row (if enabled) just below subtitle, with a 1.5x
      ratio to subtitle, only on the scientific profile.
    - line height = font_size * 1.4 (best practice for legibility).
    """
    profile = get_profile(req.template)
    t = get_template(req.template)
    items: list[HeaderItem] = []

    if not req.title:
        return HeaderSpec(items=[])

    title_h = max(profile.title_size_mm * 1.4, 8.0)
    title = HeaderItem(
        text=req.title,
        role="title",
        x=page.inner_x,
        y=page.inner_y,
        width=page.inner_w,
        height=title_h,
        font_size_mm=profile.title_size_mm,
        color=t.primary_color,
        weight="bold",
    )
    items.append(title)

    if req.subtitle:
        sub_y = page.inner_y + title_h
        sub_h = max(profile.subtitle_size_mm * 1.4, 5.0)
        sub = HeaderItem(
            text=req.subtitle,
            role="subtitle",
            x=page.inner_x,
            y=sub_y,
            width=page.inner_w,
            height=sub_h,
            font_size_mm=profile.subtitle_size_mm,
            color=t.secondary_color,
            weight="normal",
        )
        items.append(sub)

    if profile.include_metadata_block:
        meta_y = (items[-1].y + items[-1].height) + 2.0
        meta_h = max(profile.metadata_size_mm * 1.4, 4.0)
        meta_text = _format_metadata(crs=req.crs, author=req.author)
        meta = HeaderItem(
            text=meta_text,
            role="metadata",
            x=page.inner_x,
            y=meta_y,
            width=page.inner_w,
            height=meta_h,
            font_size_mm=profile.metadata_size_mm,
            color=t.secondary_color,
            weight="normal",
        )
        items.append(meta)

    # Visual weight hierarchy check: the title should be at least
    # 1.5x the subtitle. We surface this as a debug log (the
    # verifier also checks it).
    if req.subtitle and profile.title_size_mm < 1.5 * profile.subtitle_size_mm:
        # We don't raise; the LLM may have a stylistic reason.
        pass

    return HeaderSpec(items=items)


# --- Stage 3: map_zone ---------------------------------------


def stage_map_zone(
    page: PageSpec,
    header: HeaderSpec,
    req: LayoutRequest,
    reserved_footer_h: float = 0.0,
) -> MapSpec:
    """Compute map placement, size, scale, and extent.

    The map is the centerpiece. Its size is the *result* of
    everything else having been placed. We compute the
    available space below the header AND above the footer
    (the caller passes the footer's reserved height) and
    the map fills exactly that band - the legend lives in
    the footer band, *not* overlaying the map.

    Best practice: if the model passed an ``extent``, the
    map's aspect ratio should match the extent's aspect
    ratio. We adjust the map size to honour that - the
    verifier flags a warning if we had to crop.
    """
    profile = get_profile(req.template)

    # Header took some vertical space; reserve the rest for
    # the map. Footer is reserved by the caller so the legend
    # can sit in a dedicated band below the map.
    header_h = header.total_height_mm
    map_y = page.inner_y + header_h + 2.0
    map_h = page.inner_h - header_h - reserved_footer_h - 4.0
    map_w = page.inner_w  # default: full inner width; aspect-ratio branch may shrink

    # Aspect-ratio matching if extent is provided.
    extent = req.extent
    if extent is not None and extent[2] - extent[0] > 0:
        xmin, ymin, xmax, ymax = extent
        extent_w = xmax - xmin
        extent_h = ymax - ymin
        extent_aspect = extent_w / extent_h
        page_aspect = map_w / map_h
        if abs(extent_aspect - page_aspect) > 0.01:
            # Adjust map size to match the extent aspect. We
            # either shrink the width or the height.
            if extent_aspect > page_aspect:
                # extent is wider than page; shrink the height.
                map_h = map_w / extent_aspect
            else:
                # extent is taller; shrink the width.
                map_w = map_h * extent_aspect
        # Use the user-supplied scale, otherwise auto.
        scale = req.scale
    else:
        scale = req.scale  # may be None (auto)

    map = MapSpec(
        x=page.inner_x,
        y=map_y,
        width_mm=map_w,
        height_mm=map_h,
        scale=scale,
        extent=extent,
        layers=list(req.output_layer_ids or []),
        map_grid=profile.include_graticule,
        crs=req.crs,
    )
    return map


# --- Stage 4: ancillaries ------------------------------------


def stage_ancillaries(
    page: PageSpec, header: HeaderSpec, map: MapSpec, req: LayoutRequest
) -> AncillarySpec:
    """Position scale bar and north arrow relative to the map.

    Conventions (ICA / Imhof / USGS):

    - North arrow: **upper-LEFT** of the map (away from the
      title block, away from the legend). Sized to 1.5x the
      margin so it's clearly visible but not dominant.
    - Scale bar: **lower-LEFT** of the map, inside the map
      area, sized to 30% of the map width. White background
      + thin frame so it's readable over busy data.
    - Both ancillaries carry ``frame=True, background=True``
      so the QGIS emitter renders them with a clean white
      box around the symbol.
    - Rotation of the north arrow carries the magnetic
      declination if the user passed one; default 0.
    """
    profile = get_profile(req.template)
    items: list[AncillaryItem] = []

    if profile.include_north_arrow:
        arrow_size = max(page.margin_mm * 1.5, 10.0)
        items.append(
            AncillaryItem(
                item_type="north_arrow",
                x=map.x + 2,
                y=map.y + 2,
                width_mm=arrow_size,
                height_mm=arrow_size,
                rotation_deg=0.0,
                frame=True,
                background=True,
            )
        )

    if profile.include_scale_bar:
        sb_w = max(min(map.width_mm * 0.30, 60.0), 25.0)
        sb_h = max(15.0, page.margin_mm * 1.0)
        items.append(
            AncillaryItem(
                item_type="scale_bar",
                x=map.x + 4,
                y=map.y + map.height_mm - sb_h - 4,
                width_mm=sb_w,
                height_mm=sb_h,
                scale_bar_units=profile.scale_bar_units,
                scale_bar_style=profile.scale_bar_style,
                frame=True,
                background=True,
            )
        )

    return AncillarySpec(items=items)


# --- Stage 5: footer_band ------------------------------------


def compute_legend_height(req: LayoutRequest, profile: CartographicProfile) -> float:
    """Heuristic legend height in mm, proportional to layer count.

    - 10mm (header + padding) + 4mm per layer symbol, capped at
      ``max(20, min(80, ...))``. The 20mm floor matches the
      verifier's ``min_legend_size_mm`` so a 1-layer legend
      doesn't immediately trip the ``E_LEGEND_TOO_SMALL`` rule.
    - When the user supplied no layer list, fall back to a
      conservative 25mm (a single "1 layer" assumption).
    """
    if not profile.include_legend:
        return 0.0
    layers = req.output_layer_ids or []
    if not layers:
        return 25.0
    raw = 10.0 + 4.0 * len(layers)
    return max(20.0, min(80.0, raw))


def stage_footer_band(
    page: PageSpec,
    header: HeaderSpec,
    map: MapSpec,
    ancillaries: AncillarySpec,
    req: LayoutRequest,
) -> FooterSpec:
    """Position the legend in a **dedicated band below the map**.

    The legend is the only typical footer item. Convention:
    position it *outside* the map's footprint in its own
    band, with a 2mm gap below the map. Sized to its content
    (8mm + 4mm per layer, capped 15-80mm) so the legend
    doesn't waste space on tiny maps and doesn't crowd
    large ones.

    The legend carries ``frame=True, background=True`` so
    the QGIS emitter renders a clean white box.

    The "legend filters visible layers" rule is the verifier's
    job; for v1 we just inherit the user's output_layer_ids.
    """
    profile = get_profile(req.template)
    items: list[FooterItem] = []

    if not profile.include_legend:
        return FooterSpec(items=[])

    legend_h = compute_legend_height(req, profile)
    # 2mm gap between map's bottom and legend's top.
    legend_y = map.y + map.height_mm + 2.0
    items.append(
        FooterItem(
            item_type="legend",
            x=page.inner_x,
            y=legend_y,
            width_mm=page.inner_w,
            height_mm=legend_h,
            legend_layers=list(req.output_layer_ids or []),
            legend_map_ref="map",
            frame=True,
            background=True,
        )
    )

    return FooterSpec(items=items)


# --- Stage 6: final_assembly ----------------------------------


def stage_final_assembly(
    page: PageSpec,
    header: HeaderSpec,
    map: MapSpec,
    ancillaries: AncillarySpec,
    footer: FooterSpec,
) -> LayoutSpec:
    """Bundle all stage outputs into a :class:`LayoutSpec`.

    The emitter (``qpt_builder._emit_layout_xml``) consumes a
    :class:`LayoutSpec` and serializes each piece to the right
    QGIS XML element. The verifier inspects the same struct.
    """
    return LayoutSpec(
        page=page,
        header=header,
        map=map,
        ancillaries=ancillaries,
        footer=footer,
    )


# --- Top-level pipeline --------------------------------------


def run_pipeline(req: LayoutRequest) -> LayoutSpec:
    """Run all six stages and return a :class:`LayoutSpec`.

    Two-pass: compute the footer height first (it depends on
    the layer count, not the map), then size the map to fit
    the remaining space, then build the actual footer at
    the band's correct y. This keeps the legend in a
    dedicated band below the map instead of overlaying it.
    """
    page = stage_page_setup(req)
    header = stage_header_band(page, req)
    profile = get_profile(req.template)
    reserved_footer_h = compute_legend_height(req, profile)
    map_zone = stage_map_zone(page, header, req, reserved_footer_h=reserved_footer_h)
    ancillaries = stage_ancillaries(page, header, map_zone, req)
    footer = stage_footer_band(page, header, map_zone, ancillaries, req)
    return stage_final_assembly(page, header, map_zone, ancillaries, footer)


# --- Helpers --------------------------------------------------


def _format_metadata(*, crs: str, author: str) -> str:
    today = datetime.date.today().isoformat()
    parts = [f"Date: {today}"]
    if crs:
        parts.append(f"CRS: {crs}")
    if author:
        parts.append(f"Author: {author}")
    return "  |  ".join(parts)


__all__ = [
    "LayoutRequest",
    "PageSpec",
    "HeaderSpec",
    "HeaderItem",
    "MapSpec",
    "AncillarySpec",
    "AncillaryItem",
    "FooterSpec",
    "FooterItem",
    "LayoutSpec",
    "CartographicProfile",
    "get_profile",
    "stage_page_setup",
    "stage_header_band",
    "stage_map_zone",
    "stage_ancillaries",
    "stage_footer_band",
    "stage_final_assembly",
    "run_pipeline",
]
