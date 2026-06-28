"""Tests for style_templates, qml_builder, qpt_builder, layout_verifier."""

from __future__ import annotations


import pytest

from model_forge.compiler_core.core.services.map_builder import (
    layout_verifier,
    qml_builder,
    qpt_builder,
    style_templates,
)


# --- style_templates ------------------------------------------------


def test_page_sizes_have_eight_orientations():
    assert "A4_portrait" in style_templates.PAGE_SIZES_MM
    assert "Letter_landscape" in style_templates.PAGE_SIZES_MM
    a4 = style_templates.PAGE_SIZES_MM["A4_portrait"]
    assert a4[0] < a4[1]  # portrait: width < height


def test_default_templates_have_eight_entries():
    assert set(style_templates.DEFAULT_TEMPLATES.keys()) == {
        "default",
        "scientific",
        "presentation",
        "minimal",
        "screen_fullhd",
        "instagram_square",
        "index_a4",
        "drawing_a1",
    }


def test_get_template_falls_back_to_default():
    t = style_templates.get_template("nonexistent")
    assert t.name == "default"


def test_scientific_template_requires_metadata_block():
    t = style_templates.get_template("scientific")
    assert t.metadata_block is True


def test_presentation_template_disables_scale_bar():
    t = style_templates.get_template("presentation")
    assert t.include_scale_bar is False


def test_default_symbols_per_geometry_kind():
    for kind in ("polygon", "line", "point", "raster"):
        s = style_templates.get_symbol_defaults(kind)
        assert s.fill_color
    # Unknown kind falls back to polygon defaults.
    fallback = style_templates.get_symbol_defaults("unknown")
    polygon = style_templates.get_symbol_defaults("polygon")
    assert fallback.fill_color == polygon.fill_color


def test_template_to_dict_is_json_safe():
    d = style_templates.template_to_dict(style_templates.get_template("presentation"))
    import json

    json.dumps(d)  # must not raise


# --- qml_builder --------------------------------------------------


def test_qml_polygon_single_symbol_minimal():
    qml = qml_builder.build_qml("polygon", "my_layer")
    assert qml.startswith("<?xml") or qml.startswith("<!DOCTYPE")
    assert "<qgis" in qml
    assert "singleSymbol" in qml
    assert "SimpleFill" in qml


def test_qml_line_emits_simpleline():
    qml = qml_builder.build_qml("line")
    assert "SimpleLine" in qml


def test_qml_point_emits_simplemarker():
    qml = qml_builder.build_qml("point")
    assert "SimpleMarker" in qml
    assert "circle" in qml


def test_qml_raster_multiband():
    qml = qml_builder.build_qml("raster", raster_type="multiband", raster_bands=[1, 2, 3])
    assert "multibandcolor" in qml
    assert "redBand" in qml
    assert "greenBand" in qml
    assert "blueBand" in qml


def test_qml_raster_singleband():
    qml = qml_builder.build_qml("raster", raster_type="singleband", raster_bands=[4])
    assert "singlebandgray" in qml
    assert "grayBand" in qml


def test_qml_categorized_emits_categories():
    qml = qml_builder.build_qml(
        "polygon",
        renderer="categorized",
        field_name="landuse",
        categories=[
            {"value": "forest", "color": "#1f77b4"},
            {"value": "urban", "color": "#ff7f0e"},
        ],
    )
    assert "categorizedSymbol" in qml
    assert "forest" in qml
    assert "urban" in qml


def test_qml_graduated_emits_colorramp():
    qml = qml_builder.build_qml(
        "polygon", renderer="graduated", field_name="pop", classes=5, classification_mode="quantile"
    )
    assert "graduatedSymbol" in qml
    assert "colorramp" in qml
    assert "quantile" in qml


def test_qml_rule_based_emits_rules():
    qml = qml_builder.build_qml(
        "polygon",
        renderer="rule_based",
        rules=[{"label": "small", "filter": "area < 100"}],
    )
    assert "RuleRenderer" in qml
    assert "small" in qml
    # The filter is XML-escaped; "<" becomes "&lt;".
    assert "area &lt; 100" in qml


def test_qml_with_labels():
    qml = qml_builder.build_qml(
        "polygon",
        labels=[{"text": "name"}],
    )
    assert "labeling" in qml


def test_qml_unknown_renderer_raises():
    with pytest.raises(ValueError) as ei:
        qml_builder.build_qml("polygon", renderer="bogus")
    assert "Unknown renderer" in str(ei.value)


def test_qml_unknown_geometry_raises():
    with pytest.raises(ValueError):
        qml_builder.build_qml("antimeridian")


# --- qpt_builder --------------------------------------------------


def test_qpt_default_template_has_required_items():
    xml = qpt_builder.build_qpt("default", title="My Map", output_layer_ids=["out1"])
    parsed = qpt_builder.parse_qpt(xml)
    ids = {i["id"] for i in parsed["items"]}
    assert "title" in ids
    assert "map" in ids
    assert "scale_bar" in ids
    assert "north_arrow" in ids
    assert "legend" in ids


def test_qpt_minimal_template_drops_legend():
    xml = qpt_builder.build_qpt("minimal", title="Quick")
    parsed = qpt_builder.parse_qpt(xml)
    ids = {i["id"] for i in parsed["items"]}
    assert "legend" not in ids
    assert "map" in ids


def test_qpt_scientific_template_has_metadata_block():
    xml = qpt_builder.build_qpt("scientific", title="Paper", crs="EPSG:4326", author="J. Doe")
    parsed = qpt_builder.parse_qpt(xml)
    ids = {i["id"] for i in parsed["items"]}
    assert "metadata" in ids


def test_qpt_presentation_drops_scale_bar():
    xml = qpt_builder.build_qpt("presentation", title="Slides")
    parsed = qpt_builder.parse_qpt(xml)
    ids = {i["id"] for i in parsed["items"]}
    assert "scale_bar" not in ids


def test_qpt_title_carries_user_text():
    xml = qpt_builder.build_qpt("default", title="Layered Analysis")
    parsed = qpt_builder.parse_qpt(xml)
    title_item = next(i for i in parsed["items"] if i["id"] == "title")
    assert title_item["text"] == "Layered Analysis"


def test_qpt_parse_handles_empty_items():
    """A layout with no items still parses (returns an empty list)."""
    xml = """<?xml version='1.0'?>
<Layout name='empty' paper='A4_portrait' width='210' height='297'/>
"""
    parsed = qpt_builder.parse_qpt(xml)
    assert parsed["items"] == []


def test_qpt_list_templates():
    templates = qpt_builder.list_templates()
    names = {t["name"] for t in templates}
    assert {
        "default",
        "scientific",
        "presentation",
        "minimal",
        "screen_fullhd",
        "instagram_square",
        "index_a4",
        "drawing_a1",
    } <= names


# --- layout_verifier ----------------------------------------------


def test_verifier_passes_on_a_well_built_layout():
    # When output_layer_ids is supplied, the LLM is also expected
    # to supply an extent (so QGIS pins the map to the data on
    # load). The pipeline respects ``extent`` from LayoutRequest.
    xml = qpt_builder.build_qpt(
        "default",
        title="OK",
        output_layer_ids=["out1"],
        extent=(0.0, 0.0, 1000.0, 1000.0),
    )
    report = layout_verifier.verify_qpt(xml)
    assert report.passed, f"violations: {[v.to_dict() for v in report.violations]}"


def test_verifier_flags_missing_title():
    """Force a missing title by emitting an empty Layout."""
    xml = """<?xml version='1.0'?>
<Layout name='empty' paper='A4_portrait' width='210' height='297'/>
"""
    report = layout_verifier.verify_qpt(xml)
    assert not report.passed
    codes = {v.code for v in report.violations}
    assert "E_NO_TITLE" in codes


def test_verifier_flags_title_out_of_margins():
    """Manually craft a Layout with a title outside the default 10mm margin."""
    xml = """<?xml version='1.0'?>
<Layout name='bad' paper='A4_portrait' width='210' height='297'>
  <LayoutItemLabel id='title' x='0' y='0' width='50' height='5' text='X'/>
  <LayoutItemMap id='map' x='20' y='40' width='170' height='200'/>
</Layout>
"""
    report = layout_verifier.verify_qpt(xml)
    codes = {v.code for v in report.violations}
    assert "E_TITLE_OUT_OF_MARGINS" in codes


def test_verifier_scientific_requires_metadata():
    xml = """<?xml version='1.0'?>
<Layout name='scientific' paper='Letter_portrait' width='215.9' height='279.4'>
  <LayoutItemLabel id='title' x='15' y='15' width='180' height='7' text='Paper'/>
  <LayoutItemMap id='map' x='15' y='30' width='185' height='220'/>
</Layout>
"""
    report = layout_verifier.verify_qpt(xml)
    codes = {v.code for v in report.violations}
    assert "E_NO_METADATA" in codes


def test_verifier_flags_scale_bar_too_short():
    xml = """<?xml version='1.0'?>
<Layout name='x' paper='A4_portrait' width='210' height='297'>
  <LayoutItemLabel id='title' x='10' y='10' width='190' height='8' text='X'/>
  <LayoutItemMap id='map' x='10' y='30' width='190' height='220'/>
  <LayoutItemScaleBar id='scale_bar' x='20' y='245' width='30' height='2'/>
</Layout>
"""
    report = layout_verifier.verify_qpt(xml)
    codes = {v.code for v in report.violations}
    assert "E_SCALE_BAR_TOO_SHORT" in codes


def test_verifier_flags_legend_overlapping_map():
    """Cartographic convention: the legend must sit in a band
    BELOW the map, not overlaying the data. A legend that
    overlaps the map's footprint is rejected."""
    xml = """<?xml version='1.0'?>
<Layout name='x' paper='A4_portrait' width='210' height='297'>
  <LayoutItemLabel id='title' x='10' y='10' width='190' height='8' text='X'/>
  <LayoutItemMap id='map' x='10' y='30' width='190' height='220'/>
  <LayoutItemLegend id='legend' x='10' y='100' width='190' height='40' frame='true' background='true'/>
</Layout>
"""
    report = layout_verifier.verify_qpt(xml)
    codes = {v.code for v in report.violations}
    assert "E_LEGEND_OVERLAPS_MAP" in codes


def test_verifier_accepts_legend_below_map():
    """The new default layout places the legend in a dedicated
    band below the map. That should pass the layout-overlap
    rule."""
    xml = """<?xml version='1.0'?>
<Layout name='x' paper='A4_portrait' width='210' height='297'>
  <LayoutItemLabel id='title' x='10' y='10' width='190' height='8' text='X'/>
  <LayoutItemMap id='map' x='10' y='30' width='190' height='220'/>
  <LayoutItemLegend id='legend' x='10' y='252' width='190' height='35' frame='true' background='true'/>
</Layout>
"""
    report = layout_verifier.verify_qpt(xml)
    codes = {v.code for v in report.violations}
    assert "E_LEGEND_OVERLAPS_MAP" not in codes


def test_verifier_handles_unparseable_xml():
    report = layout_verifier.verify_qpt("not valid xml at all <<<")
    assert not report.passed
    assert report.violations[0].code == "E_PARSE_ERROR"


def test_verifier_to_dict_round_trip():
    xml = qpt_builder.build_qpt("default", title="OK")
    report = layout_verifier.verify_qpt(xml)
    import json

    json.dumps(report.to_dict())  # must not raise
