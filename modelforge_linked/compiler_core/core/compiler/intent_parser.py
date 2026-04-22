"""
Stage 1 - IntentParser
Cleans and normalises the raw user text before sending to the LLM planner.
"""
from __future__ import annotations
import re
from dataclasses import dataclass


@dataclass
class RawIntent:
    original_text: str
    cleaned_text: str
    inferred_hints: dict  # e.g. {"likely_crs": "EPSG:2180"}


class IntentParser:
    """
    Lightweight pre-processor. Does NOT call the LLM; just sanitises the input
    and extracts shallow hints that help the planner.
    """
    _CRS_PATTERN = re.compile(
        r"\b(EPSG:\d+|WGS\s*84|ETRS\s*89|PUWG\s*(?:92|2000)|UTM\s*zone\s*\d+\w*)\b",
        re.IGNORECASE,
    )
    _DISTANCE_PATTERN = re.compile(r"\b(\d+(?:\.\d+)?)\s*(m|km|meters?|kilometres?)\b", re.IGNORECASE)

    def parse(self, raw_text: str) -> RawIntent:
        cleaned = raw_text.strip()
        cleaned = re.sub(r"\s+", " ", cleaned)

        hints = {}
        crs_match = self._CRS_PATTERN.search(cleaned)
        if crs_match:
            hints["inferred_crs"] = crs_match.group(0)

        dist_matches = self._DISTANCE_PATTERN.findall(cleaned)
        if dist_matches:
            hints["distances"] = [{"value": v, "unit": u} for v, u in dist_matches]

        return RawIntent(
            original_text=raw_text,
            cleaned_text=cleaned,
            inferred_hints=hints,
        )
