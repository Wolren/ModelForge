"""
qpt_builder - pure-Python emitter for QGIS Print Template (.qpt) XML.

QGIS's print layout is the page-layout document. The ``.qpt`` file
is what you load into a project via "Add Item from Template" or the
"New Print Layout" → "from Template" action. The format is an XML
serialization of a ``QgsLayout`` plus its items.

We emit a self-contained ``.qpt`` that opens correctly in QGIS
without requiring a running QGIS app. It is *not* a round-trip
serialization of a live ``QgsLayout`` instance - the LLM may have
specified the layout from scratch - but the XML is well-formed
and uses QGIS's documented element names (``Layout``, ``LayoutItemMap``,
``LayoutItemLegend``, ``LayoutItemScaleBar``, ``LayoutItemNorthArrow``,
``LayoutItemLabel``).

The output is consumable by QGIS's "Add Item from Template" and
can be rendered to PDF/PNG/SVG via the MCP ``export_layout`` tool,
which uses ``QgsLayoutExporter`` against a live QGIS app.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree as ET

from .pipeline import (
    AncillaryItem,
    FooterItem,
    HeaderItem,
    LayoutRequest,
    LayoutSpec,
    MapSpec,
    PageSpec,
    run_pipeline,
)
from .style_templates import (
    PAGE_SIZES_MM,
    PrintTemplate,
    get_template,
)


# --- Item types -----------------------------------------------------


@dataclass
class LayoutItem:
    item_id: str
    item_type: str  # map | legend | scale_bar | north_arrow | label | shape
    x: float
    y: float
    width: float
    height: float
    text: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


# --- Top-level build ----------------------------------------------


def build_qpt(
    template: str = "default",
    *,
    title: str = "",
    subtitle: str = "",
    crs: str = "",
    author: str = "",
    output_layer_ids: list[str] | None = None,
    extent: tuple[float, float, float, float] | None = None,
    scale: int | None = None,
    layer_meta: dict[str, dict[str, str]] | None = None,
    custom_items: list[LayoutItem] | None = None,
) -> str:
    """Build a .qpt document.

    The default path runs the cartographic pipeline
    (:func:`run_pipeline`) which executes the six stages
    (page_setup, header_band, map_zone, ancillaries,
    footer_band, final_assembly) and emits the resulting
    ``LayoutSpec`` as XML.

    The ``custom_items`` parameter is the verifier-driven
    re-try path: callers (typically the LLM after a
    verification failure) pass a list of pre-positioned
    ``LayoutItem`` and the emitter uses them verbatim
    instead of running the pipeline. This is the
    "skip stages, render what we already negotiated" path.

    Parameters
    ----------
    template
        One of the keys in ``DEFAULT_TEMPLATES``: ``default``,
        ``scientific``, ``presentation``, ``minimal``. Unknown
        names fall back to ``default``.
    title, subtitle, crs, author
        Optional metadata. ``crs`` and ``author`` populate the
        metadata block in scientific-style templates.
    output_layer_ids
        The list of model output layer ids to show in the
        legend. ``None`` means "all layers in the project" - the
        LLM (or the project loader) populates this at load time.
    extent
        ``(xmin, ymin, xmax, ymax)`` of the data, in the
        project's CRS. When provided, the map is pinned to
        this bbox on load (no more "fit to canvas" surprise).
    scale
        Optional map scale denominator (e.g. 25000 for
        1:25,000). Overrides auto-fit.
    custom_items
        Optional list of pre-positioned ``LayoutItem`` to render
        instead of the template's defaults. Used by the
        verifier-driven re-try loop.
    """
    t = get_template(template)

    if custom_items is not None:
        # Re-try path: render the caller's items verbatim.
        return _emit_custom_items(t, custom_items)

    # Default path: run the six-stage pipeline.
    req = LayoutRequest(
        template=template,
        title=title,
        subtitle=subtitle,
        crs=crs,
        author=author,
        output_layer_ids=output_layer_ids,
        extent=extent,
        scale=scale,
    )
    spec = run_pipeline(req)
    return emit_qpt_xml(
        spec,
        template_name=template,
        title=title,
        layer_meta=layer_meta,
    )


def emit_qpt_xml(
    spec: LayoutSpec,
    *,
    template_name: str = "default",
    title: str = "",
    layer_meta: dict[str, dict[str, str]] | None = None,
) -> str:
    """Serialize a :class:`LayoutSpec` to a .qpt XML string.

    The emitter walks each dataclass in the spec and produces
    the corresponding QGIS element. Each stage's output is
    rendered in z-order: header (background) -> ancillaries
    (on top of the map) -> footer (legend overlay).
    """
    page = spec.page
    header = spec.header
    map_zone = spec.map
    ancillaries = spec.ancillaries
    footer = spec.footer

    layout = ET.Element(
        "Layout",
        {
            "name": (f"Model Forge: {title or template_name} [{template_name}]"),
            "paper": _paper_key(page),
            "width": f"{page.width_mm:.2f}",
            "height": f"{page.height_mm:.2f}",
            "page_orientation": "portrait" if page.height_mm >= page.width_mm else "landscape",
            "units": "mm",
        },
    )

    # Page-level grid: a single LayoutItemPageGrid child on the
    # root when the page_setup stage enabled it.
    if page.page_grid:
        layout.append(
            ET.Element(
                "LayoutItemPageGrid",
                {
                    "id": "page_grid",
                    "x": "0",
                    "y": "0",
                    "width": f"{page.width_mm:.2f}",
                    "height": f"{page.height_mm:.2f}",
                    "frame": "false",
                    "background": "false",
                },
            )
        )

    # Header band.
    for item in header.items:
        layout.append(_header_item_to_xml(item))

    # Map.
    layout.append(_map_to_xml(map_zone, page, layer_meta=layer_meta))

    # Ancillaries (scale bar / north arrow) - these sit on top
    # of the map; we emit them after so they render last.
    for item in ancillaries.items:
        layout.append(_ancillary_item_to_xml(item))

    # Footer (legend) - also overlays the map.
    for item in footer.items:
        layout.append(_footer_item_to_xml(item))

    ET.indent(layout, space="  ")
    body = ET.tostring(layout, encoding="unicode", xml_declaration=False)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"


def _paper_key(page: PageSpec) -> str:
    """Reverse-lookup the page-size key in PAGE_SIZES_MM.

    Falls back to ``A4_portrait`` (the most common default) if
    the size doesn't match any registered key.
    """
    for key, (w, h) in PAGE_SIZES_MM.items():
        if abs(w - page.width_mm) < 0.01 and abs(h - page.height_mm) < 0.01:
            return key
    return "A4_portrait"


# --- Per-template default item layout -----------------------------


def _default_items(
    t: PrintTemplate,
    *,
    title: str,
    subtitle: str,
    crs: str,
    author: str,
    output_layer_ids: list[str] | None,
    page_w: float,
    page_h: float,
) -> list[LayoutItem]:
    m = t.margin_mm
    items: list[LayoutItem] = []

    # Title block (top, full width inside margins)
    title_h = max(t.title_size * 1.4, 10.0)
    items.append(
        LayoutItem(
            item_id="title",
            item_type="label",
            x=m,
            y=m,
            width=page_w - 2 * m,
            height=title_h,
            text=title or t.name,
            extras={"size_mm": t.title_size, "color": t.primary_color, "weight": "bold"},
        )
    )

    # Subtitle (just below title)
    sub_y = m + title_h
    sub_h = max(t.subtitle_size * 1.4, 5.0)
    items.append(
        LayoutItem(
            item_id="subtitle",
            item_type="label",
            x=m,
            y=sub_y,
            width=page_w - 2 * m,
            height=sub_h,
            text=subtitle or "",
            extras={"size_mm": t.subtitle_size, "color": t.secondary_color},
        )
    )

    # Scientific metadata block (date / CRS / scale / author)
    if t.metadata_block:
        meta_y = sub_y + sub_h + 2
        meta_h = max(t.subtitle_size * 1.4, 4.0)
        meta_text = _format_metadata(crs=crs, author=author)
        items.append(
            LayoutItem(
                item_id="metadata",
                item_type="label",
                x=m,
                y=meta_y,
                width=page_w - 2 * m,
                height=meta_h,
                text=meta_text,
                extras={"size_mm": t.subtitle_size, "color": t.secondary_color},
            )
        )
        map_y = meta_y + meta_h + 2
    else:
        map_y = sub_y + sub_h + 2

    # Map item (the centerpiece). Takes the rest of the page.
    legend_h = 0.0
    if t.include_legend:
        legend_h = max(min(page_h * 0.20, 70.0), 30.0)
    scale_h = 0.0
    if t.include_scale_bar:
        scale_h = max(t.margin_mm * 1.0, 8.0)

    map_x = m
    map_y_offset = map_y
    map_w = page_w - 2 * m
    map_h = page_h - map_y_offset - m - legend_h - scale_h

    items.append(
        LayoutItem(
            item_id="map",
            item_type="map",
            x=map_x,
            y=map_y_offset,
            width=map_w,
            height=map_h,
            extras={
                "scale": "auto",  # QGIS resolves "auto" to fit
                "grid": t.include_grid,
            },
        )
    )

    # North arrow (top-right of map area, on top of map)
    if t.include_north_arrow:
        arrow_size = max(t.margin_mm * 1.5, 10.0)
        items.append(
            LayoutItem(
                item_id="north_arrow",
                item_type="north_arrow",
                x=map_x + map_w - arrow_size - 2,
                y=map_y_offset + 2,
                width=arrow_size,
                height=arrow_size,
            )
        )

    # Scale bar (bottom-left of map)
    if t.include_scale_bar:
        sb_w = max(min(map_w * 0.30, 60.0), 25.0)
        sb_h = max(15.0, t.margin_mm * 1.0)  # verifier minimum 15mm
        items.append(
            LayoutItem(
                item_id="scale_bar",
                item_type="scale_bar",
                x=map_x + 4,
                y=map_y_offset + map_h - sb_h - 4,
                width=sb_w,
                height=sb_h,
                extras={"style": "single_box", "units": "auto"},
            )
        )

    # Legend (positioned within the map's footprint so the
    # verifier's "legend_within_map_footprint" rule passes).
    if t.include_legend:
        items.append(
            LayoutItem(
                item_id="legend",
                item_type="legend",
                x=map_x,
                y=map_y_offset + map_h - legend_h,
                width=map_w,
                height=legend_h,
                extras={
                    "map_id": "map",
                    "layers": output_layer_ids or [],
                    "symbol_size_mm": max(t.subtitle_size * 0.7, 2.5),
                },
            )
        )

    return items


def _format_metadata(*, crs: str, author: str) -> str:
    import datetime

    today = datetime.date.today().isoformat()
    parts = [f"Date: {today}"]
    if crs:
        parts.append(f"CRS: {crs}")
    if author:
        parts.append(f"Author: {author}")
    return "  |  ".join(parts)


# --- XML serialization --------------------------------------------


def _header_item_to_xml(item: HeaderItem) -> ET.Element:
    """Render a HeaderItem as a LayoutItemLabel.

    The ``id`` is the role (``title`` / ``subtitle`` / ``metadata``)
    so the verifier's "find the title" rule works without a
    secondary lookup. The test suite and existing .qpt files
    both key off these names.
    """
    common = {
        "id": item.role,
        "x": f"{item.x:.2f}",
        "y": f"{item.y:.2f}",
        "width": f"{item.width:.2f}",
        "height": f"{item.height:.2f}",
        "position": "0,0",
        "zValue": "0",
        "frame": "false",
        "background": "false",
        "outlineWidth": "0",
        "text": item.text,
    }
    return ET.Element("LayoutItemLabel", common)


def _map_to_xml(
    map_zone: MapSpec,
    page: PageSpec,
    *,
    layer_meta: dict[str, dict[str, str]] | None = None,
) -> ET.Element:
    """Render the MapSpec as a LayoutItemMap.

    The map is emitted with a white frame + background so
    it's visually distinct from the page, and (when the
    pipeline supplied an ``extent``) a child ``<Extent>``
    element so QGIS pins the map to that bbox on load.

    ``layer_meta`` is an optional ``{layer_id: {name, source,
    provider}}`` dict. When provided, each ``<Layer>`` child
    carries full binding info so QGIS can reconnect the
    layer to its source on load - otherwise the layer
    references in the .qpt are dangling and the map shows
    nothing.
    """
    scale_attr = str(map_zone.scale) if map_zone.scale is not None else "auto"
    el = ET.Element(
        "LayoutItemMap",
        {
            "id": "map",
            "x": f"{map_zone.x:.2f}",
            "y": f"{map_zone.y:.2f}",
            "width": f"{map_zone.width_mm:.2f}",
            "height": f"{map_zone.height_mm:.2f}",
            "position": "0,0",
            "zValue": "0",
            "frame": "true",
            "background": "true",
            "outlineWidth": "0.4",
            "grid": "true" if map_zone.map_grid else "false",
            "annotationEnabled": "false",
            "scale": scale_attr,
            "crs": map_zone.crs or "",
        },
    )
    if map_zone.layers:
        layer_set = ET.SubElement(el, "LayerSet")
        for layer_id in map_zone.layers:
            attrs: dict[str, str] = {"id": str(layer_id)}
            if layer_meta and str(layer_id) in layer_meta:
                meta = layer_meta[str(layer_id)]
                for key in ("name", "source", "provider", "geometry"):
                    v = meta.get(key)
                    if v:
                        attrs[key] = str(v)
            ET.SubElement(layer_set, "Layer", attrs)
    if map_zone.extent is not None:
        xmin, ymin, xmax, ymax = map_zone.extent
        ET.SubElement(
            el,
            "Extent",
            {
                "xmin": f"{xmin:.6f}",
                "ymin": f"{ymin:.6f}",
                "xmax": f"{xmax:.6f}",
                "ymax": f"{ymax:.6f}",
            },
        )
    return el


def _ancillary_item_to_xml(item: AncillaryItem) -> ET.Element:
    """Render an AncillaryItem as a LayoutItemScaleBar or
    LayoutItemNorthArrow.

    The ``id`` is the item type itself (``scale_bar`` or
    ``north_arrow``) so existing rules and tests key off the
    natural name. ``frame`` and ``background`` default to
    True on the dataclass so ancillaries stay readable over
    busy map data.
    """
    common = {
        "id": item.item_type,
        "x": f"{item.x:.2f}",
        "y": f"{item.y:.2f}",
        "width": f"{item.width_mm:.2f}",
        "height": f"{item.height_mm:.2f}",
        "position": "0,0",
        "zValue": "0",
        "frame": "true" if item.frame else "false",
        "background": "true" if item.background else "false",
        "outlineWidth": "0.4" if item.frame else "0",
    }
    if item.item_type == "scale_bar":
        el = ET.Element(
            "LayoutItemScaleBar",
            {
                **common,
                "style": item.scale_bar_style,
                "units": item.scale_bar_units,
                "segmentSizeMode": "auto",
                "minWidth": "30",
            },
        )
        return el
    if item.item_type == "north_arrow":
        return ET.Element(
            "LayoutItemNorthArrow",
            {
                **common,
                "direction": "0",
                "rotation": str(item.rotation_deg),
            },
        )
    raise ValueError(f"Unknown ancillary item type: {item.item_type!r}")


def _footer_item_to_xml(item: FooterItem) -> ET.Element:
    """Render a FooterItem as a LayoutItemLegend.

    The ``id`` is ``legend`` (the only footer item type in v1)
    so existing rules and tests key off the natural name.
    The legend gets a white frame + background by default
    (configurable on the dataclass) so it sits cleanly in
    the footer band.
    """
    sym_w = item.symbol_size_mm if item.symbol_size_mm > 0 else 4.0
    sym_h = item.symbol_size_mm if item.symbol_size_mm > 0 else 4.0
    el = ET.Element(
        "LayoutItemLegend",
        {
            "id": "legend",
            "x": f"{item.x:.2f}",
            "y": f"{item.y:.2f}",
            "width": f"{item.width_mm:.2f}",
            "height": f"{item.height_mm:.2f}",
            "position": "0,0",
            "zValue": "0",
            "frame": "true" if item.frame else "false",
            "background": "true" if item.background else "false",
            "outlineWidth": "0.4" if item.frame else "0",
            "title": "",
            "symbolWidth": f"{sym_w:.2f}",
            "symbolHeight": f"{sym_h:.2f}",
            "map": item.legend_map_ref,
        },
    )
    if item.legend_layers:
        layer_set = ET.SubElement(el, "LayerSet")
        for layer in item.legend_layers:
            ET.SubElement(layer_set, "Layer", {"id": str(layer)})
    return el


def _emit_custom_items(t: PrintTemplate, items: list[LayoutItem]) -> str:
    """Re-try path: render caller's pre-positioned items verbatim.

    Used after the verifier rejects a layout and the LLM has
    re-positioned the items by hand. The caller passes a
    fully-built ``LayoutItem`` list; we just serialize.
    """
    page_w = next(
        (w for k, (w, h) in PAGE_SIZES_MM.items() if k == t.page),
        210.0,
    )
    page_h = next(
        (h for k, (w, h) in PAGE_SIZES_MM.items() if k == t.page),
        297.0,
    )
    layout = ET.Element(
        "Layout",
        {
            "name": f"Model Forge: {t.name} [{t.name}]",
            "paper": t.page,
            "width": f"{page_w:.2f}",
            "height": f"{page_h:.2f}",
            "page_orientation": "portrait" if page_h >= page_w else "landscape",
            "units": "mm",
        },
    )
    for item in items:
        layout.append(_item_to_xml(item, t))
    ET.indent(layout, space="  ")
    body = ET.tostring(layout, encoding="unicode", xml_declaration=False)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"


def _item_to_xml(item: LayoutItem, t: PrintTemplate) -> ET.Element:
    common = {
        "id": item.item_id,
        "x": f"{item.x:.2f}",
        "y": f"{item.y:.2f}",
        "width": f"{item.width:.2f}",
        "height": f"{item.height:.2f}",
        "position": "0,0",  # top-left origin; matches mm coords
        "zValue": "0",
        "frame": "false",
        "background": "false",
        "outlineWidth": "0",
    }
    if item.item_type == "label":
        el = ET.Element(
            "LayoutItemLabel",
            {
                **common,
                "text": item.text,
            },
        )
        return el
    if item.item_type == "map":
        el = ET.Element(
            "LayoutItemMap",
            {
                **common,
                "grid": "true" if item.extras.get("grid") else "false",
                "annotationEnabled": "false",
                "scale": str(item.extras.get("scale", "auto")),
            },
        )
        return el
    if item.item_type == "legend":
        layers = item.extras.get("layers") or []
        el = ET.Element(
            "LayoutItemLegend",
            {
                **common,
                "title": "",
                "symbolWidth": str(item.extras.get("symbol_size_mm", 4.0)),
                "symbolHeight": str(item.extras.get("symbol_size_mm", 4.0)),
                "map": str(item.extras.get("map_id", "map")),
            },
        )
        if layers:
            layer_set = ET.SubElement(el, "LayerSet")
            for layer in layers:
                ET.SubElement(layer_set, "Layer", {"id": str(layer)})
        return el
    if item.item_type == "scale_bar":
        el = ET.Element(
            "LayoutItemScaleBar",
            {
                **common,
                "style": str(item.extras.get("style", "single_box")),
                "units": str(item.extras.get("units", "auto")),
                "segmentSizeMode": "auto",
                "minWidth": "30",
            },
        )
        return el
    if item.item_type == "north_arrow":
        el = ET.Element(
            "LayoutItemNorthArrow",
            {
                **common,
                "direction": "0",
                "rotation": "0",
            },
        )
        return el
    if item.item_type == "shape":
        el = ET.Element(
            "LayoutItemShape",
            {
                **common,
                "shapeType": str(item.extras.get("shape", "rectangle")),
            },
        )
        return el
    raise ValueError(f"Unknown layout item type: {item.item_type!r}")


# --- Public helpers ----------------------------------------------


def list_templates() -> list[dict[str, Any]]:
    """Return the registered template metadata (name, page, sizes, ...)."""
    from .style_templates import DEFAULT_TEMPLATES, template_to_dict

    return [template_to_dict(t) for t in DEFAULT_TEMPLATES.values()]


def parse_qpt(xml: str) -> dict[str, Any]:
    """Parse a .qpt document into a flat dict for the verifier.

    The verifier doesn't need a full XML model - just enough to
    count items, find positions / sizes, and check for required
    pieces. Returns a plain dict so it's easy to test.

    Each map item also carries ``layers`` (list of layer ids
    from the ``<Layer>`` children) and ``extent`` (the bbox
    from the ``<Extent>`` child, or None). This lets the
    verifier check that the map is locked to the data.
    """
    root = ET.fromstring(xml)
    items: list[dict[str, Any]] = []
    for child in root:
        d = dict(child.attrib)
        d["type"] = child.tag
        # Surface child elements that the verifier needs.
        layers = [c.attrib.get("id") for c in child if c.tag == "Layer"]
        if not layers:
            # Some QGIS versions wrap layers in <LayerSet>.
            for c in child:
                if c.tag == "LayerSet":
                    layers = [l.attrib.get("id") for l in c if l.tag == "Layer"]
        if layers:
            d["layers"] = layers
        extent_el = next((c for c in child if c.tag == "Extent"), None)
        if extent_el is not None:
            try:
                d["extent"] = (
                    float(extent_el.attrib["xmin"]),
                    float(extent_el.attrib["ymin"]),
                    float(extent_el.attrib["xmax"]),
                    float(extent_el.attrib["ymax"]),
                )
            except (KeyError, ValueError):
                d["extent"] = None
        items.append(d)
    return {
        "name": root.attrib.get("name", ""),
        "page": root.attrib.get("paper", ""),
        "width_mm": float(root.attrib.get("width", 0)),
        "height_mm": float(root.attrib.get("height", 0)),
        "items": items,
    }


__all__ = [
    "LayoutItem",
    "build_qpt",
    "list_templates",
    "parse_qpt",
]
