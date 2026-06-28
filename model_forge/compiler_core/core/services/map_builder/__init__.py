"""Map building: print layout + symbology + execution.

This subpackage owns the generative map pipeline:

- :mod:`.pipeline` - the six cartographic stages (page_setup,
  header_band, map_zone, ancillaries, footer_band,
  final_assembly) and the profile system.
- :mod:`.qpt_builder` - the XML emitter for QGIS Print Layout
  templates. The default path runs the pipeline; the
  ``custom_items`` re-try path renders caller-positioned
  items verbatim.
- :mod:`.qml_builder` - per-layer-type default QML (single
  symbol / categorized / graduated / rule-based for vectors;
  multiband / singleband for rasters).
- :mod:`.layout_verifier` - ruleset for both XML (.qpt) and
  spec-level layouts. Per-stage rules catch the cartographic
  best-practice violations the LLM is most likely to produce.
- :mod:`.style_templates` - the print templates (default,
  scientific, presentation, minimal), the cartographic
  profile system, per-geometry-type symbol defaults, and
  verifier thresholds.

The MCP server imports from this subpackage. The QGIS
plugin (when we wire Phase 14) also imports from this
subpackage directly, so the server and the local
generation path share the *same* engine.

Public surface (re-exported below for ``from
model_forge.compiler_core.core.services.map_builder
import ...``):

  Templates and profiles:
    PAGE_SIZES_MM, DEFAULT_TEMPLATES, DEFAULT_SYMBOLS,
    PrintTemplate, SymbolDefaults, VerifierLimits,
    CartographicProfile, get_template, get_symbol_defaults,
    get_profile, template_to_dict

  Pipeline:
    LayoutRequest, PageSpec, HeaderSpec, HeaderItem,
    MapSpec, AncillarySpec, AncillaryItem, FooterSpec,
    FooterItem, LayoutSpec,
    stage_page_setup, stage_header_band, stage_map_zone,
    stage_ancillaries, stage_footer_band,
    stage_final_assembly, run_pipeline

  QPT:
    build_qpt, emit_qpt_xml, parse_qpt, list_templates,
    LayoutItem

  QML:
    build_qml

  Verifier:
    Violation, VerificationReport, verify_qpt,
    verify_layout_spec, RULES, SPEC_RULES
"""

from __future__ import annotations

from .layout_verifier import (
    RULES,
    SPEC_RULES,
    VerificationReport,
    Violation,
    verify_layout_spec,
    verify_qpt,
)
from .pipeline import (
    AncillaryItem,
    AncillarySpec,
    CartographicProfile,
    FooterItem,
    FooterSpec,
    GridSpec,
    HeaderItem,
    HeaderSpec,
    LayoutRequest,
    LayoutSpec,
    MapSpec,
    PageSpec,
    get_profile,
    run_pipeline,
    stage_ancillaries,
    stage_final_assembly,
    stage_footer_band,
    stage_header_band,
    stage_map_zone,
    stage_page_setup,
)
from .qml_builder import build_qml
from .qpt_builder import LayoutItem, build_qpt, emit_qpt_xml, list_templates, parse_qpt
from .style_templates import (
    DEFAULT_SYMBOLS,
    DEFAULT_TEMPLATES,
    DEFAULT_VECTOR_PALETTE,
    NORTH_ARROW_SVGS,
    PAGE_SIZES_MM,
    VERIFIER_LIMITS,
    PrintTemplate,
    SymbolDefaults,
    VerifierLimits,
    get_symbol_defaults,
    get_template,
    template_to_dict,
)

__all__ = [
    # Pipeline
    "LayoutRequest",
    "PageSpec",
    "HeaderSpec",
    "HeaderItem",
    "MapSpec",
    "GridSpec",
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
    # QPT
    "build_qpt",
    "emit_qpt_xml",
    "parse_qpt",
    "list_templates",
    "LayoutItem",
    # QML
    "build_qml",
    # Verifier
    "Violation",
    "VerificationReport",
    "verify_qpt",
    "verify_layout_spec",
    "RULES",
    "SPEC_RULES",
    # Templates
    "NORTH_ARROW_SVGS",
    "PAGE_SIZES_MM",
    "DEFAULT_TEMPLATES",
    "DEFAULT_SYMBOLS",
    "DEFAULT_VECTOR_PALETTE",
    "PrintTemplate",
    "SymbolDefaults",
    "VerifierLimits",
    "VERIFIER_LIMITS",
    "get_template",
    "get_symbol_defaults",
    "template_to_dict",
]
