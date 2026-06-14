"""Tests for the six-stage cartographic pipeline.

Each stage gets its own focused test. Then a per-profile
happy-path test that runs the whole pipeline end-to-end
through ``run_pipeline`` and asserts the produced ``LayoutSpec``
passes the spec-level verifier.
"""

from __future__ import annotations


from model_forge.compiler_core.core.services.map_builder import (
    layout_verifier,
    pipeline,
)


# --- Profiles ----------------------------------------------------


def test_profiles_cover_all_template_names():
    assert set(pipeline._PROFILES.keys()) == {
        "scientific",
        "internal",
        "presentation",
        "minimal",
    }


def test_get_profile_falls_back_to_internal():
    p = pipeline.get_profile("nonexistent")
    assert p.name == "internal"


def test_get_profile_default_maps_to_internal():
    assert pipeline.get_profile("default").name == "internal"


def test_scientific_profile_has_metadata_and_bleed():
    p = pipeline.get_profile("scientific")
    assert p.include_metadata_block is True
    assert p.bleed_mm == 3.0
    assert p.page_grid is True
    assert p.include_graticule is True


def test_presentation_profile_omits_scale_bar():
    p = pipeline.get_profile("presentation")
    assert p.include_scale_bar is False
    assert p.title_size_mm == 12.0


def test_minimal_profile_omits_legend():
    p = pipeline.get_profile("minimal")
    assert p.include_legend is False


# --- Stage 1: page_setup --------------------------------------


def test_page_setup_uses_registered_size():
    req = pipeline.LayoutRequest(template="scientific")
    page = pipeline.stage_page_setup(req)
    # Letter portrait is 215.9 x 279.4mm.
    assert abs(page.width_mm - 215.9) < 0.01
    assert abs(page.height_mm - 279.4) < 0.01
    assert page.margin_mm == 15.0
    assert page.bleed_mm == 3.0
    assert page.page_grid is True


def test_page_setup_inner_rect_inside_margins():
    page = pipeline.stage_page_setup(pipeline.LayoutRequest(template="internal"))
    assert page.inner_x >= page.margin_mm
    assert page.inner_y >= page.margin_mm
    assert page.inner_x + page.inner_w <= page.width_mm - page.margin_mm + 0.01
    assert page.inner_y + page.inner_h <= page.height_mm - page.margin_mm + 0.01


# --- Stage 2: header_band ------------------------------------


def test_header_band_emits_title_only_when_no_subtitle():
    page = pipeline.stage_page_setup(pipeline.LayoutRequest(template="internal"))
    req = pipeline.LayoutRequest(template="internal", title="My Map")
    header = pipeline.stage_header_band(page, req)
    assert len(header.items) == 1
    assert header.items[0].role == "title"
    assert header.items[0].text == "My Map"


def test_header_band_stacks_title_subtitle_metadata():
    page = pipeline.stage_page_setup(pipeline.LayoutRequest(template="scientific"))
    req = pipeline.LayoutRequest(
        template="scientific",
        title="My Map",
        subtitle="A Subtitle",
        crs="EPSG:4326",
        author="J. Doe",
    )
    header = pipeline.stage_header_band(page, req)
    roles = [it.role for it in header.items]
    assert roles == ["title", "subtitle", "metadata"]


def test_header_band_scientific_visual_hierarchy():
    """Scientific title should be at least 1.5 times the metadata font size."""
    page = pipeline.stage_page_setup(pipeline.LayoutRequest(template="scientific"))
    header = pipeline.stage_header_band(
        page,
        pipeline.LayoutRequest(
            template="scientific",
            title="A",
            subtitle="B",
            crs="EPSG:4326",
            author="A",
        ),
    )
    items = {it.role: it for it in header.items}
    assert items["title"].font_size_mm >= 1.5 * items["subtitle"].font_size_mm
    assert items["subtitle"].font_size_mm >= 1.0 * items["metadata"].font_size_mm


# --- Stage 3: map_zone --------------------------------------


def test_map_zone_fills_remaining_space_after_header():
    page = pipeline.stage_page_setup(pipeline.LayoutRequest(template="internal"))
    header = pipeline.stage_header_band(
        page, pipeline.LayoutRequest(template="internal", title="My Map")
    )
    map_zone = pipeline.stage_map_zone(page, header, pipeline.LayoutRequest())
    assert map_zone.y > header.total_height_mm
    assert map_zone.x == page.inner_x
    assert abs(map_zone.width_mm - page.inner_w) < 0.01


def test_map_zone_aspect_ratio_matches_extent_when_extent_provided():
    page = pipeline.stage_page_setup(pipeline.LayoutRequest(template="internal"))
    header = pipeline.stage_header_band(
        page, pipeline.LayoutRequest(template="internal", title="X")
    )
    req = pipeline.LayoutRequest(
        template="internal",
        extent=(0.0, 0.0, 10.0, 5.0),  # 2:1 aspect
    )
    map_zone = pipeline.stage_map_zone(page, header, req)
    expected_aspect = 10.0 / 5.0
    actual_aspect = map_zone.width_mm / map_zone.height_mm
    assert abs(actual_aspect - expected_aspect) < 0.01


def test_map_zone_keeps_provided_scale():
    page = pipeline.stage_page_setup(pipeline.LayoutRequest(template="internal"))
    header = pipeline.stage_header_band(
        page, pipeline.LayoutRequest(template="internal", title="X")
    )
    req = pipeline.LayoutRequest(template="internal", scale=25000)
    map_zone = pipeline.stage_map_zone(page, header, req)
    assert map_zone.scale == 25000


# --- Stage 4: ancillaries ------------------------------------


def test_ancillaries_scientific_has_scale_bar_and_north_arrow():
    page = pipeline.stage_page_setup(pipeline.LayoutRequest(template="scientific"))
    header = pipeline.stage_header_band(
        page, pipeline.LayoutRequest(template="scientific", title="X")
    )
    map_zone = pipeline.stage_map_zone(page, header, pipeline.LayoutRequest())
    ancillaries = pipeline.stage_ancillaries(
        page, header, map_zone, pipeline.LayoutRequest(template="scientific")
    )
    types = {it.item_type for it in ancillaries.items}
    assert "scale_bar" in types
    assert "north_arrow" in types


def test_ancillaries_presentation_has_no_scale_bar():
    page = pipeline.stage_page_setup(pipeline.LayoutRequest(template="presentation"))
    header = pipeline.stage_header_band(
        page, pipeline.LayoutRequest(template="presentation", title="X")
    )
    map_zone = pipeline.stage_map_zone(page, header, pipeline.LayoutRequest())
    ancillaries = pipeline.stage_ancillaries(
        page, header, map_zone, pipeline.LayoutRequest(template="presentation")
    )
    types = {it.item_type for it in ancillaries.items}
    assert "scale_bar" not in types
    assert "north_arrow" in types


def test_ancillaries_on_map_footprint():
    page = pipeline.stage_page_setup(pipeline.LayoutRequest(template="internal"))
    header = pipeline.stage_header_band(
        page, pipeline.LayoutRequest(template="internal", title="X")
    )
    map_zone = pipeline.stage_map_zone(page, header, pipeline.LayoutRequest())
    ancillaries = pipeline.stage_ancillaries(page, header, map_zone, pipeline.LayoutRequest())
    for item in ancillaries.items:
        assert map_zone.x <= item.x
        assert map_zone.y <= item.y
        assert item.x + item.width_mm <= map_zone.x + map_zone.width_mm + 0.01
        assert item.y + item.height_mm <= map_zone.y + map_zone.height_mm + 0.01


# --- Stage 5: footer_band ------------------------------------


def test_footer_band_includes_legend_by_default():
    page = pipeline.stage_page_setup(pipeline.LayoutRequest(template="internal"))
    header = pipeline.stage_header_band(
        page, pipeline.LayoutRequest(template="internal", title="X")
    )
    map_zone = pipeline.stage_map_zone(page, header, pipeline.LayoutRequest())
    ancillaries = pipeline.stage_ancillaries(page, header, map_zone, pipeline.LayoutRequest())
    footer = pipeline.stage_footer_band(
        page, header, map_zone, ancillaries, pipeline.LayoutRequest(output_layer_ids=["a", "b"])
    )
    assert any(it.item_type == "legend" for it in footer.items)


def test_footer_band_legend_below_map():
    """The legend must sit in a dedicated band BELOW the map,
    not overlaying the data (cartographic convention)."""
    page = pipeline.stage_page_setup(pipeline.LayoutRequest(template="internal"))
    header = pipeline.stage_header_band(
        page, pipeline.LayoutRequest(template="internal", title="X")
    )
    map_zone = pipeline.stage_map_zone(page, header, pipeline.LayoutRequest())
    ancillaries = pipeline.stage_ancillaries(page, header, map_zone, pipeline.LayoutRequest())
    footer = pipeline.stage_footer_band(
        page, header, map_zone, ancillaries, pipeline.LayoutRequest(output_layer_ids=["a", "b"])
    )
    for item in footer.items:
        if item.item_type != "legend":
            continue
        # Legend's top edge must be at or below the map's
        # bottom edge (with a small gap).
        assert item.y >= map_zone.y + map_zone.height_mm - 1.0


def test_legend_height_scales_with_layer_count():
    """Larger layer lists should produce a taller legend."""
    from model_forge.compiler_core.core.services.map_builder.pipeline import (
        compute_legend_height,
    )

    profile = pipeline.get_profile("internal")
    req1 = pipeline.LayoutRequest(output_layer_ids=["a"])
    req10 = pipeline.LayoutRequest(output_layer_ids=[f"layer_{i}" for i in range(10)])
    h1 = compute_legend_height(req1, profile)
    h10 = compute_legend_height(req10, profile)
    assert h10 > h1
    # Cap of 80mm is honoured.
    req100 = pipeline.LayoutRequest(output_layer_ids=[f"layer_{i}" for i in range(100)])
    h100 = compute_legend_height(req100, profile)
    assert h100 <= 80.0


def test_north_arrow_is_in_upper_left():
    """The north arrow must sit in the map's upper-left
    quadrant (cartographic convention)."""
    page = pipeline.stage_page_setup(pipeline.LayoutRequest(template="internal"))
    header = pipeline.stage_header_band(
        page, pipeline.LayoutRequest(template="internal", title="X")
    )
    map_zone = pipeline.stage_map_zone(page, header, pipeline.LayoutRequest())
    ancillaries = pipeline.stage_ancillaries(page, header, map_zone, pipeline.LayoutRequest())
    for item in ancillaries.items:
        if item.item_type != "north_arrow":
            continue
        # Center of the arrow should be in the map's left third.
        center_x = item.x + item.width_mm / 2.0
        assert center_x <= map_zone.x + map_zone.width_mm * 0.30 + 0.01


def test_ancillaries_have_frame_and_background():
    """Scale bar and north arrow must have frame+background
    enabled so they stay readable over busy map data."""
    page = pipeline.stage_page_setup(pipeline.LayoutRequest(template="scientific"))
    header = pipeline.stage_header_band(
        page, pipeline.LayoutRequest(template="scientific", title="X")
    )
    map_zone = pipeline.stage_map_zone(page, header, pipeline.LayoutRequest())
    ancillaries = pipeline.stage_ancillaries(
        page, header, map_zone, pipeline.LayoutRequest(template="scientific")
    )
    for item in ancillaries.items:
        assert item.frame is True
        assert item.background is True


def test_legend_has_frame_and_background():
    """The legend must have frame+background enabled so it
    reads cleanly in the footer band."""
    page = pipeline.stage_page_setup(pipeline.LayoutRequest(template="internal"))
    header = pipeline.stage_header_band(
        page, pipeline.LayoutRequest(template="internal", title="X")
    )
    map_zone = pipeline.stage_map_zone(page, header, pipeline.LayoutRequest())
    ancillaries = pipeline.stage_ancillaries(page, header, map_zone, pipeline.LayoutRequest())
    footer = pipeline.stage_footer_band(
        page, header, map_zone, ancillaries, pipeline.LayoutRequest(output_layer_ids=["a", "b"])
    )
    for item in footer.items:
        if item.item_type != "legend":
            continue
        assert item.frame is True
        assert item.background is True


def test_map_zone_subtracts_footer_height():
    """When a footer is reserved, the map's height must shrink
    to make room (so the legend doesn't overlap)."""
    page = pipeline.stage_page_setup(pipeline.LayoutRequest(template="internal"))
    header = pipeline.stage_header_band(
        page, pipeline.LayoutRequest(template="internal", title="X")
    )
    # Without footer reservation: map fills everything.
    m_full = pipeline.stage_map_zone(page, header, pipeline.LayoutRequest())
    # With footer reservation: map should be shorter.
    m_reserved = pipeline.stage_map_zone(
        page, header, pipeline.LayoutRequest(), reserved_footer_h=40.0
    )
    assert m_reserved.height_mm < m_full.height_mm
    # Bottom of the reserved map + 40mm gap = where the legend
    # will sit, within the page.
    assert m_reserved.y + m_reserved.height_mm + 40.0 <= page.inner_y + page.inner_h + 1.0


def test_map_spec_propagates_extent():
    """LayoutRequest.extent must flow into MapSpec.extent so
    the emitter can serialize the <Extent> child."""
    page = pipeline.stage_page_setup(pipeline.LayoutRequest())
    header = pipeline.stage_header_band(page, pipeline.LayoutRequest(title="X"))
    extent = (0.0, 0.0, 1000.0, 1000.0)
    map_zone = pipeline.stage_map_zone(page, header, pipeline.LayoutRequest(extent=extent))
    assert map_zone.extent == extent


# --- Stage 6: final_assembly ----------------------------------


def test_final_assembly_bundles_all_stages():
    page = pipeline.stage_page_setup(pipeline.LayoutRequest(template="internal"))
    header = pipeline.stage_header_band(
        page, pipeline.LayoutRequest(template="internal", title="X")
    )
    map_zone = pipeline.stage_map_zone(page, header, pipeline.LayoutRequest())
    ancillaries = pipeline.stage_ancillaries(page, header, map_zone, pipeline.LayoutRequest())
    footer = pipeline.stage_footer_band(
        page, header, map_zone, ancillaries, pipeline.LayoutRequest()
    )
    spec = pipeline.stage_final_assembly(page, header, map_zone, ancillaries, footer)
    assert spec.page is page
    assert spec.header is header
    assert spec.map is map_zone
    assert spec.ancillaries is ancillaries
    assert spec.footer is footer


# --- End-to-end via run_pipeline ----------------------------


def test_run_pipeline_internal_default_passes_verifier():
    req = pipeline.LayoutRequest(
        template="internal",
        title="Run pipeline test",
        subtitle="Verifying end-to-end",
        output_layer_ids=["layer_a", "layer_b"],
    )
    spec = pipeline.run_pipeline(req)
    # Every spec-level rule should pass on a default
    # request against the internal profile.
    report = layout_verifier.verify_layout_spec(spec, template_name="internal")
    failures = [v for v in report.violations if v.severity == "error"]
    assert not failures, f"unexpected verifier errors: {[v.to_dict() for v in failures]}"


def test_run_pipeline_scientific_with_extent_passes():
    req = pipeline.LayoutRequest(
        template="scientific",
        title="Buffer vs Roads 50m",
        subtitle="EPSG:4326 area of interest",
        crs="EPSG:4326",
        author="J. Doe",
        output_layer_ids=["buffer_out"],
        extent=(0.0, 0.0, 10.0, 5.0),
    )
    spec = pipeline.run_pipeline(req)
    report = layout_verifier.verify_layout_spec(spec, template_name="scientific")
    errors = [v for v in report.violations if v.severity == "error"]
    assert not errors


def test_run_pipeline_presentation_minimal_omits_scale_bar():
    req = pipeline.LayoutRequest(
        template="presentation",
        title="Client deck",
    )
    spec = pipeline.run_pipeline(req)
    types = {it.item_type for it in spec.ancillaries.items}
    assert "scale_bar" not in types


def test_run_pipeline_minimal_omits_legend():
    req = pipeline.LayoutRequest(
        template="minimal",
        title="Quick map",
    )
    spec = pipeline.run_pipeline(req)
    assert not spec.footer.items


# --- Spec-level verifier rules -----------------------------


def test_verifier_rejects_bleed_larger_than_margin():
    """Construct a spec with bleed_mm >= margin_mm and assert
    the verifier emits E_BLEED_LARGER_THAN_MARGIN.
    """
    from dataclasses import replace

    req = pipeline.LayoutRequest(template="internal", title="X")
    spec = pipeline.run_pipeline(req)
    bad_page = replace(spec.page, bleed_mm=spec.page.margin_mm + 1.0)
    bad_spec = replace(spec, page=bad_page)
    report = layout_verifier.verify_layout_spec(bad_spec, template_name="internal")
    codes = {v.code for v in report.violations}
    assert "E_BLEED_LARGER_THAN_MARGIN" in codes


def test_verifier_rejects_visual_hierarchy_violation():
    """Set a subtitle that is too big relative to the title and
    expect the E_HEADER_HIERARCHY warning.
    """
    from dataclasses import replace

    req = pipeline.LayoutRequest(template="internal", title="Big", subtitle="Sub")
    spec = pipeline.run_pipeline(req)
    # Force the subtitle to be 80% the size of the title.
    title = next(it for it in spec.header.items if it.role == "title")
    sub = next(it for it in spec.header.items if it.role == "subtitle")
    bad_sub = replace(sub, font_size_mm=title.font_size_mm * 0.8)
    new_items = [bad_sub if it.role == "subtitle" else it for it in spec.header.items]
    bad_spec = replace(spec, header=replace(spec.header, items=new_items))
    report = layout_verifier.verify_layout_spec(bad_spec, template_name="internal")
    codes = {v.code for v in report.violations}
    assert "E_HEADER_HIERARCHY" in codes


def test_verifier_rejects_legend_referencing_unknown_layer():
    from dataclasses import replace

    req = pipeline.LayoutRequest(
        template="internal",
        title="X",
        output_layer_ids=["a", "b"],
    )
    spec = pipeline.run_pipeline(req)
    # Add a "ghost" layer to the legend that's not in the map.
    bad_legend = next(it for it in spec.footer.items if it.item_type == "legend")
    bad_legend_items = list(bad_legend.legend_layers) + ["ghost_layer"]
    bad_legend = replace(bad_legend, legend_layers=bad_legend_items)
    new_footer = replace(
        spec.footer,
        items=[bad_legend if it.item_type == "legend" else it for it in spec.footer.items],
    )
    bad_spec = replace(spec, footer=new_footer)
    report = layout_verifier.verify_layout_spec(bad_spec, template_name="internal")
    codes = {v.code for v in report.violations}
    assert "E_LEGEND_REFERENCES_UNKNOWN_LAYER" in codes


def test_verifier_rejects_scale_bar_too_long():
    from dataclasses import replace

    req = pipeline.LayoutRequest(template="internal", title="X")
    spec = pipeline.run_pipeline(req)
    # Stretch the scale bar to > 50% of map width.
    bad_sb = next(it for it in spec.ancillaries.items if it.item_type == "scale_bar")
    bad_sb = replace(bad_sb, width_mm=spec.map.width_mm * 0.6)
    new_items = [bad_sb if it.item_type == "scale_bar" else it for it in spec.ancillaries.items]
    bad_spec = replace(spec, ancillaries=replace(spec.ancillaries, items=new_items))
    report = layout_verifier.verify_layout_spec(bad_spec, template_name="internal")
    codes = {v.code for v in report.violations}
    assert "E_SCALE_BAR_TOO_LONG" in codes


def test_verifier_rejects_ancillary_off_map():
    from dataclasses import replace

    req = pipeline.LayoutRequest(template="internal", title="X")
    spec = pipeline.run_pipeline(req)
    # Push the scale bar off the map.
    bad_sb = next(it for it in spec.ancillaries.items if it.item_type == "scale_bar")
    bad_sb = replace(bad_sb, x=spec.map.x - 20.0)
    new_items = [bad_sb if it.item_type == "scale_bar" else it for it in spec.ancillaries.items]
    bad_spec = replace(spec, ancillaries=replace(spec.ancillaries, items=new_items))
    report = layout_verifier.verify_layout_spec(bad_spec, template_name="internal")
    codes = {v.code for v in report.violations}
    assert "E_ANCILLARY_OFF_MAP" in codes


# --- qpt_builder integration ---------------------------------


def test_qpt_builder_uses_pipeline_for_default_path():
    from model_forge.compiler_core.core.services.map_builder import qpt_builder

    xml = qpt_builder.build_qpt("internal", title="Pipeline integration", output_layer_ids=["a"])
    assert "<Layout" in xml
    # The new emitter uses id="title" / "subtitle" / "map" /
    # "scale_bar" / "north_arrow" / "legend".
    parsed = qpt_builder.parse_qpt(xml)
    ids = {it["id"] for it in parsed["items"]}
    assert "title" in ids
    assert "map" in ids
    assert "scale_bar" in ids
    assert "north_arrow" in ids
    assert "legend" in ids
