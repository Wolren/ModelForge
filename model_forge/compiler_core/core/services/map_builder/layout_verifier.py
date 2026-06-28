"""
layout_verifier - ruleset for .qpt print layout documents.

The verifier runs a battery of structural and semantic rules
against a ``.qpt`` document and returns a list of violations
plus an overall pass/fail verdict. The LLM consumes this in
its re-try loop: ``generate_print_layout`` → ``verify_layout``
→ if violations, re-emit with the violations as constraints.

The rules are intentionally **narrow and high-signal** - they
catch real problems (legend references unknown layer, scale
bar outside margins, missing title), not stylistic preferences
("I would have made the title 8mm instead of 7mm"). The LLM
is the aesthetic engine; the verifier is the structural
correctness oracle.

Each rule is a function ``(qpt: dict) -> list[Violation]`` that
returns zero or more violations. A violation has a stable
``code`` (so the LLM can refer to it by name) and a human
``message``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .pipeline import LayoutSpec
from .qpt_builder import parse_qpt
from .style_templates import (
    VERIFIER_LIMITS,
    get_template,
)


@dataclass
class Violation:
    code: str
    message: str
    severity: str = "error"  # error | warning
    item_id: str | None = None
    rule: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }
        if self.item_id is not None:
            out["item_id"] = self.item_id
        if self.rule is not None:
            out["rule"] = self.rule
        return out


@dataclass
class VerificationReport:
    template: str
    page_size_mm: tuple[float, float]
    total_rules: int
    rules_passed: int
    rules_failed: int
    violations: list[Violation] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(v.severity != "error" for v in self.violations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "template": self.template,
            "page_size_mm": list(self.page_size_mm),
            "total_rules": self.total_rules,
            "rules_passed": self.rules_passed,
            "rules_failed": self.rules_failed,
            "passed": self.passed,
            "violations": [v.to_dict() for v in self.violations],
        }


# --- Rule registry -----------------------------------------------


def _rule(name: str) -> Callable:
    def deco(fn: Callable[..., list[Violation]]) -> Callable[..., list[Violation]]:
        fn._rule_name = name  # type: ignore[attr-defined]
        return fn

    return deco


@_rule("has_title")
def _has_title(qpt: dict[str, Any]) -> list[Violation]:
    items = qpt.get("items", [])
    titles = [i for i in items if i.get("id") == "title"]
    if not titles:
        return [
            Violation(
                code="E_NO_TITLE",
                message="Layout has no title item.",
                rule="has_title",
            )
        ]
    text = titles[0].get("text", "").strip()
    if not text:
        return [
            Violation(
                code="E_EMPTY_TITLE",
                message="Title item has empty text.",
                rule="has_title",
                item_id="title",
            )
        ]
    return []


@_rule("title_in_margins")
def _title_in_margins(qpt: dict[str, Any]) -> list[Violation]:
    items = qpt.get("items", [])
    title = next((i for i in items if i.get("id") == "title"), None)
    if title is None:
        return []
    page_w = qpt.get("width_mm", 0)
    page_h = qpt.get("height_mm", 0)
    if page_w <= 0 or page_h <= 0:
        return []
    t = get_template(_infer_template(qpt))
    m = t.margin_mm
    x = float(title.get("x", 0))
    y = float(title.get("y", 0))
    w = float(title.get("width", 0))
    h = float(title.get("height", 0))
    if x < m - 0.5 or y < m - 0.5 or x + w > page_w - m + 0.5 or y + h > page_h - m + 0.5:
        return [
            Violation(
                code="E_TITLE_OUT_OF_MARGINS",
                message=(
                    f"Title is outside the {m}mm margin "
                    f"(x={x:.1f}, y={y:.1f}, w={w:.1f}, h={h:.1f})."
                ),
                rule="title_in_margins",
                item_id="title",
            )
        ]
    return []


@_rule("title_size_in_range")
def _title_size_in_range(qpt: dict[str, Any]) -> list[Violation]:
    items = qpt.get("items", [])
    title = next((i for i in items if i.get("id") == "title"), None)
    if title is None:
        return []
    size = _size_mm(title)
    if size is None:
        return []
    lo, hi = VERIFIER_LIMITS.min_title_size_mm, VERIFIER_LIMITS.max_title_size_mm
    if size < lo or size > hi:
        return [
            Violation(
                code="E_TITLE_SIZE",
                message=f"Title size {size}mm is outside the recommended range [{lo}, {hi}]mm.",
                severity="warning",
                rule="title_size_in_range",
                item_id="title",
            )
        ]
    return []


@_rule("scale_bar_size_in_range")
def _scale_bar_size_in_range(qpt: dict[str, Any]) -> list[Violation]:
    items = qpt.get("items", [])
    sb = next((i for i in items if i.get("id") == "scale_bar"), None)
    if sb is None:
        return []
    h = float(sb.get("height", 0))
    if h < VERIFIER_LIMITS.min_scale_bar_length_mm:
        return [
            Violation(
                code="E_SCALE_BAR_TOO_SHORT",
                message=f"Scale bar height {h}mm is below the minimum {VERIFIER_LIMITS.min_scale_bar_length_mm}mm.",
                rule="scale_bar_size_in_range",
                item_id="scale_bar",
            )
        ]
    return []


@_rule("north_arrow_size_in_range")
def _north_arrow_size_in_range(qpt: dict[str, Any]) -> list[Violation]:
    items = qpt.get("items", [])
    arrow = next((i for i in items if i.get("id") == "north_arrow"), None)
    if arrow is None:
        return []
    w = float(arrow.get("width", 0))
    if w < VERIFIER_LIMITS.min_north_arrow_size_mm:
        return [
            Violation(
                code="E_NORTH_ARROW_TOO_SMALL",
                message=f"North arrow width {w}mm is below the minimum {VERIFIER_LIMITS.min_north_arrow_size_mm}mm.",
                rule="north_arrow_size_in_range",
                item_id="north_arrow",
            )
        ]
    return []


@_rule("legend_size_in_range")
def _legend_size_in_range(qpt: dict[str, Any]) -> list[Violation]:
    items = qpt.get("items", [])
    legend = next((i for i in items if i.get("id") == "legend"), None)
    if legend is None:
        return []
    h = float(legend.get("height", 0))
    if h < VERIFIER_LIMITS.min_legend_size_mm:
        return [
            Violation(
                code="E_LEGEND_TOO_SMALL",
                message=f"Legend height {h}mm is below the minimum {VERIFIER_LIMITS.min_legend_size_mm}mm.",
                rule="legend_size_in_range",
                item_id="legend",
            )
        ]
    return []


@_rule("legend_outside_map_footprint")
def _legend_outside_map_footprint(qpt: dict[str, Any]) -> list[Violation]:
    """The legend must sit *outside* the map's data area.

    Proper cartographic convention (ICA / Imhof / Ordnance
    Survey): the legend lives in a dedicated band below the
    map, not overlaying the data. An overlay obscures the
    bottom of the map and degrades the printed artifact.
    We allow a 1mm tolerance for rounding.
    """
    items = qpt.get("items", [])
    legend = next((i for i in items if i.get("id") == "legend"), None)
    map_item = next((i for i in items if i.get("id") == "map"), None)
    if legend is None or map_item is None:
        return []
    lx, ly = float(legend.get("x", 0)), float(legend.get("y", 0))
    lw, lh = float(legend.get("width", 0)), float(legend.get("height", 0))
    mx, my = float(map_item.get("x", 0)), float(map_item.get("y", 0))
    mw, mh = float(map_item.get("width", 0)), float(map_item.get("height", 0))
    # Overlap test (1mm tolerance).
    tol = 1.0
    overlaps = not (
        lx + lw <= mx + tol or lx >= mx + mw - tol or ly + lh <= my + tol or ly >= my + mh - tol
    )
    if overlaps:
        return [
            Violation(
                code="E_LEGEND_OVERLAPS_MAP",
                message=(
                    "Legend overlays the map's data area. Cartographic "
                    "guidelines require the legend in a dedicated band "
                    "below the map."
                ),
                severity="error",
                rule="legend_outside_map_footprint",
                item_id="legend",
            )
        ]
    return []


@_rule("north_arrow_in_upper_left")
def _north_arrow_in_upper_left(qpt: dict[str, Any]) -> list[Violation]:
    """North arrow must sit in the **upper-left** quadrant of the map.

    The traditional cartographic placement: top-left, away
    from the title (top) and the legend / scale bar (bottom).
    An arrow in the upper-right is a frequent convention
    violation.
    """
    items = qpt.get("items", [])
    arrow = next((i for i in items if i.get("id") == "north_arrow"), None)
    map_item = next((i for i in items if i.get("id") == "map"), None)
    if arrow is None or map_item is None:
        return []
    ax = float(arrow.get("x", 0))
    aw = float(arrow.get("width", 0))
    mx = float(map_item.get("x", 0))
    mw = float(map_item.get("width", 0))
    # Arrow's center should be in the map's left quarter.
    arrow_center = ax + aw / 2.0
    if arrow_center > mx + mw * 0.30:
        return [
            Violation(
                code="E_NORTH_ARROW_NOT_UPPER_LEFT",
                message=(
                    "North arrow should sit in the upper-left quadrant of "
                    "the map (cartographic convention)."
                ),
                severity="warning",
                rule="north_arrow_in_upper_left",
                item_id="north_arrow",
            )
        ]
    return []


@_rule("ancillary_has_background")
def _ancillary_has_background(qpt: dict[str, Any]) -> list[Violation]:
    """Scale bar and north arrow must have a white background
    and a frame, so they stay readable over busy map data.
    """
    items = qpt.get("items", [])
    violations: list[Violation] = []
    for item in items:
        iid = item.get("id")
        if iid not in {"scale_bar", "north_arrow"}:
            continue
        if item.get("background") != "true" or item.get("frame") != "true":
            violations.append(
                Violation(
                    code="E_ANCILLARY_NO_BACKGROUND",
                    message=(
                        f"{iid} must have frame+background enabled for readability over the map."
                    ),
                    severity="warning",
                    rule="ancillary_has_background",
                    item_id=iid,
                )
            )
    return violations


@_rule("legend_has_background")
def _legend_has_background(qpt: dict[str, Any]) -> list[Violation]:
    """The legend must have a frame + background so the
    items it lists read clearly in the footer band.
    """
    items = qpt.get("items", [])
    legend = next((i for i in items if i.get("id") == "legend"), None)
    if legend is None:
        return []
    if legend.get("background") != "true" or legend.get("frame") != "true":
        return [
            Violation(
                code="E_LEGEND_NO_BACKGROUND",
                message="Legend must have frame+background enabled.",
                severity="warning",
                rule="legend_has_background",
                item_id="legend",
            )
        ]
    return []


@_rule("map_has_extent_when_layers_known")
def _map_has_extent_when_layers_known(qpt: dict[str, Any]) -> list[Violation]:
    """When the LLM supplied an output layer list, the map
    should have an explicit ``<Extent>`` child so QGIS pins
    the view to the data on load. Without an extent, the map
    shows whatever the project's canvas extent happens to
    be, which is fragile (often world or blank).
    """
    map_item = next((i for i in qpt.get("items", []) if i.get("id") == "map"), None)
    if map_item is None:
        return []
    layers = map_item.get("layers", [])
    if not layers:
        return []
    if map_item.get("extent") is None:
        return [
            Violation(
                code="E_MAP_NO_EXTENT",
                message=(
                    "Map references output layers but has no <Extent> "
                    "element; the layout will show whatever the canvas "
                    "extent happens to be at load time."
                ),
                severity="warning",
                rule="map_has_extent_when_layers_known",
                item_id="map",
            )
        ]
    return []


def items_iter(qpt: dict[str, Any]):
    """Yield items from a parsed-qpt dict (helper for tests)."""
    if "items" in qpt and isinstance(qpt["items"], list):
        for it in qpt["items"]:
            yield it
        return
    if "elements" in qpt and isinstance(qpt["elements"], list):
        for it in qpt["elements"]:
            yield it


@_rule("metadata_block_in_scientific")
def _metadata_block_in_scientific(qpt: dict[str, Any]) -> list[Violation]:
    template = _infer_template(qpt)
    if template not in VERIFIER_LIMITS.require_metadata_in:
        return []
    items = qpt.get("items", [])
    if not any(i.get("id") == "metadata" for i in items):
        return [
            Violation(
                code="E_NO_METADATA",
                message=f"Template '{template}' requires a metadata block (date / CRS / scale).",
                rule="metadata_block_in_scientific",
            )
        ]
    return []


@_rule("no_overlapping_critical_items")
def _no_overlapping_critical_items(qpt: dict[str, Any]) -> list[Violation]:
    """Title, scale bar, north arrow, and the map should not
    overlap with each other. We treat overlaps as warnings
    rather than errors because the north arrow and scale bar
    are intentionally placed *on* the map (overlapping by
    design); the LLM should see the warning and decide whether
    the placement looks good in the rendered output."""
    items = qpt.get("items", [])
    crit = [i for i in items if i.get("id") in {"title", "map", "scale_bar", "north_arrow"}]
    violations: list[Violation] = []
    for i in range(len(crit)):
        for j in range(i + 1, len(crit)):
            a, b = crit[i], crit[j]
            if _rects_overlap(a, b):
                violations.append(
                    Violation(
                        code="E_OVERLAP",
                        message=(f"Items {a.get('id')!r} and {b.get('id')!r} overlap."),
                        severity="warning",
                        rule="no_overlapping_critical_items",
                    )
                )
    return violations


# --- Top-level verify() ------------------------------------------


RULES: list[Callable] = [
    _has_title,
    _title_in_margins,
    _title_size_in_range,
    _scale_bar_size_in_range,
    _north_arrow_size_in_range,
    _legend_size_in_range,
    _legend_outside_map_footprint,
    _north_arrow_in_upper_left,
    _ancillary_has_background,
    _legend_has_background,
    _map_has_extent_when_layers_known,
    _metadata_block_in_scientific,
    _no_overlapping_critical_items,
]


def verify_qpt(qpt_xml: str) -> VerificationReport:
    """Run all rules against a .qpt XML document."""
    try:
        qpt = parse_qpt(qpt_xml)
    except Exception as e:  # noqa: BLE001
        return VerificationReport(
            template="unknown",
            page_size_mm=(0.0, 0.0),
            total_rules=len(RULES),
            rules_passed=0,
            rules_failed=len(RULES),
            violations=[
                Violation(
                    code="E_PARSE_ERROR",
                    message=f"Failed to parse .qpt: {e}",
                    rule="parse",
                )
            ],
        )
    template = _infer_template(qpt)
    page_size = (qpt.get("width_mm", 0.0), qpt.get("height_mm", 0.0))
    report = VerificationReport(
        template=template,
        page_size_mm=page_size,
        total_rules=len(RULES),
        rules_passed=0,
        rules_failed=0,
    )
    for rule in RULES:
        try:
            vs = rule(qpt)
        except Exception as e:  # noqa: BLE001
            vs = [
                Violation(
                    code="E_RULE_ERROR",
                    message=f"Rule {rule._rule_name!r} raised: {e}",  # type: ignore[attr-defined]
                    rule=getattr(rule, "_rule_name", "unknown"),
                )
            ]
        if vs:
            report.rules_failed += 1
            report.violations.extend(vs)
        else:
            report.rules_passed += 1
    return report


# --- Helpers ----------------------------------------------------


def _infer_template(qpt: dict[str, Any]) -> str:
    """Best-effort template name from the layout's name or page size.

    Templates embed their own name in the layout's name field
    (we use the convention ``"Model Forge: <title> [<template>]"``).
    Fall back to page-size inference: 16:9 landscape implies
    the ``presentation`` template; Letter portrait implies
    ``scientific``; everything else is ``default``.

    Returns the canonical template name so the verifier uses
    the right margin / metadata / size rules for that template.
    """
    name = qpt.get("name", "")
    for tpl in ("scientific", "presentation", "minimal", "default"):
        if tpl in name.lower():
            return tpl
    # Page-size fallback.
    w = qpt.get("width_mm", 0)
    h = qpt.get("height_mm", 0)
    if w > 0 and h > 0:
        # 16:9 landscape is the presentation signature.
        if w > h and abs(w / h - 16 / 9) < 0.05:
            return "presentation"
        # Letter portrait (~8.5 x 11 in) is the scientific
        # signature; A4 portrait (~210 x 297) is the default.
        if abs(w - 215.9) < 1 and abs(h - 279.4) < 1:
            return "scientific"
    return "default"


def _size_mm(item: dict[str, Any]) -> float | None:
    try:
        return float(item.get("extras", {}).get("size_mm") or item.get("height", 0))
    except Exception:
        return None


def _rects_overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
    ax, ay = float(a.get("x", 0)), float(a.get("y", 0))
    aw, ah = float(a.get("width", 0)), float(a.get("height", 0))
    bx, by = float(b.get("x", 0)), float(b.get("y", 0))
    bw, bh = float(b.get("width", 0)), float(b.get("height", 0))
    return not (ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay)


# --- Spec-level rules (post-pipeline) ------------------------


SpecRule = Callable[["LayoutSpec"], list["Violation"]]
SPEC_RULES: list[SpecRule] = []


def _spec_rule(name: str) -> Callable:
    def deco(fn: SpecRule) -> SpecRule:
        fn._rule_name = name  # type: ignore[attr-defined]
        SPEC_RULES.append(fn)
        return fn

    return deco


@_spec_rule("page_setup.aspect_ratio")
def _page_aspect_ratio(spec: LayoutSpec) -> list[Violation]:
    """Page size should be one of the registered keys.

    The emitter falls back to A4_portrait on miss; we surface
    that as a warning so the LLM knows the layout is using a
    non-standard size.
    """
    from .pipeline import PAGE_SIZES_MM as _  # noqa: F401  (avoid circular)
    from .style_templates import PAGE_SIZES_MM

    for key, (w, h) in PAGE_SIZES_MM.items():
        if abs(w - spec.page.width_mm) < 0.01 and abs(h - spec.page.height_mm) < 0.01:
            return []
    return [
        Violation(
            code="E_PAGE_SIZE",
            message=(
                f"Page size {spec.page.width_mm}x{spec.page.height_mm}mm is not a "
                "registered template key; renderer will fall back to A4_portrait."
            ),
            severity="warning",
            rule="page_setup.aspect_ratio",
        )
    ]


@_spec_rule("page_setup.bleed_with_bleed_marks")
def _bleed_in_margins(spec: LayoutSpec) -> list[Violation]:
    """When bleed > 0, the printable area must be smaller than
    the inner (margin) area, otherwise the bleed crops into the
    margin.
    """
    if spec.page.bleed_mm <= 0:
        return []
    if spec.page.bleed_mm >= spec.page.margin_mm:
        return [
            Violation(
                code="E_BLEED_LARGER_THAN_MARGIN",
                message=(
                    f"Bleed {spec.page.bleed_mm}mm is >= margin {spec.page.margin_mm}mm. "
                    "Increase the margin or reduce the bleed."
                ),
                rule="page_setup.bleed_with_bleed_marks",
            )
        ]
    return []


@_spec_rule("header_band.visual_weight_hierarchy")
def _header_hierarchy(spec: LayoutSpec) -> list[Violation]:
    """Title should be at least 1.5x the subtitle font size, and
    subtitle at least 1.2x the metadata font size. This is the
    classic visual-weight hierarchy: each tier is clearly
    dominant over the next.
    """
    items = {it.role: it for it in spec.header.items}
    title = items.get("title")
    sub = items.get("subtitle")
    meta = items.get("metadata")
    if title and sub and sub.font_size_mm > 0 and title.font_size_mm < 1.5 * sub.font_size_mm:
        return [
            Violation(
                code="E_HEADER_HIERARCHY",
                message=(
                    f"Title size {title.font_size_mm}mm is less than 1.5x subtitle "
                    f"size {sub.font_size_mm}mm. Visual hierarchy unclear."
                ),
                severity="warning",
                rule="header_band.visual_weight_hierarchy",
            )
        ]
    if sub and meta and meta.font_size_mm > 0 and sub.font_size_mm < 1.2 * meta.font_size_mm:
        return [
            Violation(
                code="E_HEADER_HIERARCHY",
                message=(
                    f"Subtitle size {sub.font_size_mm}mm is less than 1.2x metadata "
                    f"size {meta.font_size_mm}mm."
                ),
                severity="warning",
                rule="header_band.visual_weight_hierarchy",
            )
        ]
    return []


@_spec_rule("map_zone.scale_consistent_with_extent")
def _map_scale_consistency(spec: LayoutSpec) -> list[Violation]:
    """If both extent and scale are given, the map's actual
    scale is fixed: scale = extent_diagonal_mm / map_diagonal_mm.
    If the user-supplied scale disagrees by more than 20%, the
    rendered map will be visibly different from intent.
    """
    req = spec.map
    if not req.layers or req.width_mm <= 0 or req.height_mm <= 0 or req.scale is None:
        return []
    # Without the extent the LLM passed we can't compute. The
    # request object is not stored on the spec; the verifier
    # consumer can pre-compute this and attach as
    # ``spec.map._extent`` if they want a stronger check.
    extent = getattr(spec.map, "_extent", None)
    if extent is None:
        return []
    xmin, ymin, xmax, ymax = extent
    extent_w = xmax - xmin
    extent_h = ymax - ymin
    if extent_w <= 0 or extent_h <= 0:
        return []
    # Crude: assume degrees -> metres using 111km/deg for the
    # latitude band. (Production would use the CRS's units.)
    extent_w_mm = extent_w * 111_000_000 * 1000
    extent_h_mm = extent_h * 111_000_000 * 1000
    actual_scale = max(extent_w_mm / req.width_mm, extent_h_mm / req.height_mm)
    if actual_scale <= 0:
        return []
    ratio = max(actual_scale, req.scale) / min(actual_scale, req.scale)
    if ratio > 1.20:
        return [
            Violation(
                code="E_MAP_SCALE_MISMATCH",
                message=(
                    f"Map scale {req.scale} disagrees with the geometry-derived "
                    f"scale {actual_scale:.0f} by more than 20%."
                ),
                severity="warning",
                rule="map_zone.scale_consistent_with_extent",
            )
        ]
    return []


@_spec_rule("map_zone.map_within_margins")
def _map_within_margins(spec: LayoutSpec) -> list[Violation]:
    """The map must fit within the page's margin rectangle."""
    page = spec.page
    map_zone = spec.map
    if map_zone.x < page.margin_mm - 0.5:
        return [
            Violation(
                code="E_MAP_OUT_OF_MARGIN_LEFT",
                message=f"Map x={map_zone.x}mm is outside the {page.margin_mm}mm left margin.",
                rule="map_zone.map_within_margins",
            )
        ]
    if map_zone.y < page.margin_mm - 0.5:
        return [
            Violation(
                code="E_MAP_OUT_OF_MARGIN_TOP",
                message=f"Map y={map_zone.y}mm is outside the {page.margin_mm}mm top margin.",
                rule="map_zone.map_within_margins",
            )
        ]
    if map_zone.x + map_zone.width_mm > page.width_mm - page.margin_mm + 0.5:
        return [
            Violation(
                code="E_MAP_OUT_OF_MARGIN_RIGHT",
                message=f"Map right edge exceeds the right margin.",
                rule="map_zone.map_within_margins",
            )
        ]
    if map_zone.y + map_zone.height_mm > page.height_mm - page.margin_mm + 0.5:
        return [
            Violation(
                code="E_MAP_OUT_OF_MARGIN_BOTTOM",
                message=f"Map bottom edge exceeds the bottom margin.",
                rule="map_zone.map_within_margins",
            )
        ]
    return []


@_spec_rule("ancillaries.scale_bar_size")
def _scale_bar_size(spec: LayoutSpec) -> list[Violation]:
    """Scale bar should be 20-30% of map width; minimum 15mm."""
    for item in spec.ancillaries.items:
        if item.item_type != "scale_bar":
            continue
        if item.width_mm < 15.0:
            return [
                Violation(
                    code="E_SCALE_BAR_TOO_SHORT",
                    message=f"Scale bar width {item.width_mm}mm is below the minimum 15mm.",
                    rule="ancillaries.scale_bar_size",
                )
            ]
        if item.width_mm > 0.5 * spec.map.width_mm:
            return [
                Violation(
                    code="E_SCALE_BAR_TOO_LONG",
                    message=(
                        f"Scale bar width {item.width_mm}mm is > 50% of the map "
                        f"width ({spec.map.width_mm}mm)."
                    ),
                    severity="warning",
                    rule="ancillaries.scale_bar_size",
                )
            ]
    return []


@_spec_rule("ancillaries.north_arrow_size")
def _north_arrow_size(spec: LayoutSpec) -> list[Violation]:
    """North arrow should be 1.0-2.0x the margin (visible but
    not dominant)."""
    for item in spec.ancillaries.items:
        if item.item_type != "north_arrow":
            continue
        m = spec.page.margin_mm
        if item.width_mm < m:
            return [
                Violation(
                    code="E_NORTH_ARROW_TOO_SMALL",
                    message=(f"North arrow {item.width_mm}mm is smaller than the margin ({m}mm)."),
                    rule="ancillaries.north_arrow_size",
                )
            ]
    return []


@_spec_rule("ancillaries.scale_bar_on_map")
def _ancillaries_on_map(spec: LayoutSpec) -> list[Violation]:
    """Scale bar and north arrow should sit *on* the map
    footprint, not floating in the margin.
    """
    map_zone = spec.map
    violations: list[Violation] = []
    for item in spec.ancillaries.items:
        ix, iy = item.x, item.y
        iw, ih = item.width_mm, item.height_mm
        on_map = (
            ix >= map_zone.x
            and iy >= map_zone.y
            and ix + iw <= map_zone.x + map_zone.width_mm
            and iy + ih <= map_zone.y + map_zone.height_mm
        )
        if not on_map:
            violations.append(
                Violation(
                    code="E_ANCILLARY_OFF_MAP",
                    message=(f"{item.item_type} is not positioned on the map footprint."),
                    severity="warning",
                    rule="ancillaries.scale_bar_on_map",
                )
            )
    return violations


@_spec_rule("footer_band.legend_below_map")
def _legend_below_map(spec: LayoutSpec) -> list[Violation]:
    """Legend must sit *below* the map in a dedicated footer
    band, not overlaying the data. Cartographic convention
    (ICA / Imhof / Ordnance Survey): the legend is an
    annotation of the map, not part of its content area.
    """
    if not spec.footer.items:
        return []
    map_zone = spec.map
    for item in spec.footer.items:
        if item.item_type != "legend":
            continue
        # Overlap test (1mm tolerance).
        tol = 1.0
        overlaps = not (
            item.x + item.width_mm <= map_zone.x + tol
            or item.x >= map_zone.x + map_zone.width_mm - tol
            or item.y + item.height_mm <= map_zone.y + tol
            or item.y >= map_zone.y + map_zone.height_mm - tol
        )
        if overlaps:
            return [
                Violation(
                    code="E_LEGEND_OVERLAPS_MAP",
                    message=(
                        "Legend overlays the map's data area. The legend "
                        "should sit in a dedicated band below the map."
                    ),
                    severity="error",
                    rule="footer_band.legend_below_map",
                    item_id="legend",
                )
            ]
    return []


@_spec_rule("map_zone.grid_interval_reasonable")
def _grid_interval_reasonable(spec: LayoutSpec) -> list[Violation]:
    """When a grid is enabled with explicit intervals, check they
    are reasonable for the map extent. Intervals wider than the
    extent produce a single grid line -- useless.
    """
    grid = spec.map.grid_spec
    if grid is None or not grid.enabled:
        return []
    if grid.interval_x is None and grid.interval_y is None:
        return []
    extent = spec.map.extent
    if extent is None:
        return []
    xmin, ymin, xmax, ymax = extent
    extent_w = xmax - xmin
    extent_h = ymax - ymin
    if extent_w <= 0 or extent_h <= 0:
        return []
    violations: list[Violation] = []
    if grid.interval_x is not None and grid.interval_x > extent_w:
        violations.append(
            Violation(
                code="E_GRID_INTERVAL_TOO_LARGE",
                message=(
                    f"Grid interval X ({grid.interval_x}) is larger than the "
                    f"extent width ({extent_w:.2f}). Grid will have 0-1 lines."
                ),
                severity="warning",
                rule="map_zone.grid_interval_reasonable",
                item_id="map",
            )
        )
    if grid.interval_y is not None and grid.interval_y > extent_h:
        violations.append(
            Violation(
                code="E_GRID_INTERVAL_TOO_LARGE",
                message=(
                    f"Grid interval Y ({grid.interval_y}) is larger than the "
                    f"extent height ({extent_h:.2f}). Grid will have 0-1 lines."
                ),
                severity="warning",
                rule="map_zone.grid_interval_reasonable",
                item_id="map",
            )
        )
    return violations


@_spec_rule("ancillaries.north_arrow_upper_left")
def _north_arrow_upper_left(spec: LayoutSpec) -> list[Violation]:
    """North arrow should sit in the map's upper-LEFT quadrant
    (cartographic convention).
    """
    map_zone = spec.map
    for item in spec.ancillaries.items:
        if item.item_type != "north_arrow":
            continue
        center_x = item.x + item.width_mm / 2.0
        if center_x > map_zone.x + map_zone.width_mm * 0.30:
            return [
                Violation(
                    code="E_NORTH_ARROW_NOT_UPPER_LEFT",
                    message=("North arrow should sit in the upper-left quadrant of the map."),
                    severity="warning",
                    rule="ancillaries.north_arrow_upper_left",
                    item_id="north_arrow",
                )
            ]
    return []


@_spec_rule("ancillaries.scale_bar_has_background")
def _scale_bar_has_background(spec: LayoutSpec) -> list[Violation]:
    """Scale bar should have frame + background enabled for
    readability over busy map data.
    """
    for item in spec.ancillaries.items:
        if item.item_type != "scale_bar":
            continue
        if not (item.frame and item.background):
            return [
                Violation(
                    code="E_SCALE_BAR_NO_BACKGROUND",
                    message=(
                        "Scale bar must have frame+background enabled for readability over the map."
                    ),
                    severity="warning",
                    rule="ancillaries.scale_bar_has_background",
                    item_id="scale_bar",
                )
            ]
    return []


@_spec_rule("footer_band.legend_has_background")
def _spec_legend_has_background(spec: LayoutSpec) -> list[Violation]:
    """Legend should have frame + background so the items it
    lists read cleanly in the footer band.
    """
    for item in spec.footer.items:
        if item.item_type != "legend":
            continue
        if not (item.frame and item.background):
            return [
                Violation(
                    code="E_LEGEND_NO_BACKGROUND",
                    message=("Legend must have frame+background enabled."),
                    severity="warning",
                    rule="footer_band.legend_has_background",
                    item_id="legend",
                )
            ]
    return []


@_spec_rule("map_zone.extent_specified_when_layers_known")
def _map_extent_specified_when_layers_known(
    spec: LayoutSpec,
) -> list[Violation]:
    """When the LLM supplied an output layer list, the map
    should also carry an ``extent`` so QGIS pins the view
    to the data. Without an extent, the layout shows
    whatever the project's canvas extent happens to be -
    fragile and usually wrong.
    """
    if not spec.map.layers:
        return []
    if spec.map.extent is None:
        return [
            Violation(
                code="E_MAP_NO_EXTENT",
                message=(
                    "Map references output layers but has no extent; the "
                    "layout will show whatever the canvas extent is at "
                    "load time."
                ),
                severity="warning",
                rule="map_zone.extent_specified_when_layers_known",
                item_id="map",
            )
        ]
    return []


@_spec_rule("footer_band.legend_layers_subset_of_map")
def _legend_layers_subset(spec: LayoutSpec) -> list[Violation]:
    """Every layer the legend shows must also be in the map.

    The inverse is allowed: the map can show layers the legend
    doesn't (the LLM might be hiding noise)."""
    if not spec.footer.items:
        return []
    map_layers = set(spec.map.layers)
    if not map_layers:
        # Map has no explicit layer list (all project layers) - skip.
        return []
    violations: list[Violation] = []
    for item in spec.footer.items:
        if item.item_type != "legend":
            continue
        for layer in item.legend_layers:
            if layer not in map_layers:
                violations.append(
                    Violation(
                        code="E_LEGEND_REFERENCES_UNKNOWN_LAYER",
                        message=(
                            f"Legend references layer {layer!r} which is not in the "
                            "map's layer list."
                        ),
                        severity="warning",
                        rule="footer_band.legend_layers_subset_of_map",
                    )
                )
    return violations


def verify_layout_spec(
    spec: LayoutSpec,
    template_name: str = "default",
) -> VerificationReport:
    """Run the per-stage spec rules against a :class:`LayoutSpec`.

    Same shape as :func:`verify_qpt` but operates on the
    pre-emit spec, so the LLM can re-try against structured
    failures without round-tripping through XML.
    """
    page = spec.page
    page_size = (page.width_mm, page.height_mm)
    report = VerificationReport(
        template=template_name,
        page_size_mm=page_size,
        total_rules=len(SPEC_RULES),
        rules_passed=0,
        rules_failed=0,
    )
    for rule in SPEC_RULES:
        try:
            vs = rule(spec)
        except Exception as e:  # noqa: BLE001
            vs = [
                Violation(
                    code="E_RULE_ERROR",
                    message=f"Rule {getattr(rule, '_rule_name', 'unknown')!r} raised: {e}",
                    rule=getattr(rule, "_rule_name", "unknown"),
                )
            ]
        if vs:
            report.rules_failed += 1
            report.violations.extend(vs)
        else:
            report.rules_passed += 1
    return report


__all__ = [
    "Violation",
    "VerificationReport",
    "verify_qpt",
    "verify_layout_spec",
    "RULES",
    "SPEC_RULES",
]
