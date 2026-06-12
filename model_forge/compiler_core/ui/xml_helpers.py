"""
XML helpers for model builder bridge - pure Python XML manipulation.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any


def _opt(name: str | None, typ: str, value: str | None = None) -> ET.Element:
    """Create an XML Option element."""
    el = ET.Element("Option")
    if name is not None:
        el.set("name", name)
    el.set("type", typ)
    if value is not None:
        el.set("value", value)
    return el


def _source_element(pbind: dict[str, Any]) -> ET.Element:
    """Create parameter source binding XML element."""
    src_map = ET.Element("Option")
    src_map.set("type", "Map")
    src_type = pbind.get("type", "static")

    if src_type == "model_input":
        src_map.append(_opt("source", "int", "0"))
        src_map.append(_opt("parameter_name", "QString", pbind.get("input_name", "")))
    elif src_type == "child_output":
        src_map.append(_opt("source", "int", "1"))
        src_map.append(_opt("child_id", "QString", pbind.get("child_id", "")))
        src_map.append(_opt("output_name", "QString", pbind.get("output_name", "OUTPUT")))
    else:
        val = pbind.get("value")
        src_map.append(_opt("source", "int", "2"))
        if val is None:
            src_map.append(_opt("static_value", "invalid"))
        elif isinstance(val, bool):
            src_map.append(_opt("static_value", "bool", str(val).lower()))
        elif isinstance(val, int):
            src_map.append(_opt("static_value", "int", str(val)))
        elif isinstance(val, float):
            src_map.append(_opt("static_value", "double", str(val)))
        else:
            src_map.append(_opt("static_value", "QString", str(val)))

    return src_map


def _find_child_node(root: ET.Element, actual_child_id: str) -> ET.Element | None:
    """Find child algorithm node in model XML."""
    for el in root.iter("Option"):
        if el.get("name") in ("children", "algs"):
            for child in list(el):
                if child.get("name") == actual_child_id and child.tag == "Option":
                    return child

    for el in root.iter("Option"):
        if el.get("name") == actual_child_id and el.get("type") == "Map":
            return el
    return None


def _inject_params_xml(
    tree: ET.ElementTree,
    actual_child_id: str,
    bindings: dict[str, Any],
) -> bool:
    """Inject parameter bindings into child algorithm XML node."""
    child_node = _find_child_node(tree.getroot(), actual_child_id)
    if child_node is None:
        return False

    params_key = "params"
    for el in list(child_node):
        if el.get("name") in ("params", "parameters"):
            params_key = el.get("name") or "params"
            child_node.remove(el)
            break

    params_el = ET.SubElement(child_node, "Option")
    params_el.set("type", "Map")
    params_el.set("name", params_key)

    for pname, pbind in bindings.items():
        list_el = ET.SubElement(params_el, "Option")
        list_el.set("type", "List")
        list_el.set("name", pname)
        list_el.append(_source_element(pbind))

    return True


def _resolve_ids(
    bindings: dict[str, Any],
    id_map: dict[str, str],
) -> dict[str, Any]:
    """Resolve user IDs to actual QGIS IDs."""
    resolved = {}
    for pname, pbind in bindings.items():
        if pbind.get("type") == "child_output":
            cid = pbind.get("child_id", "")
            resolved[pname] = {**pbind, "child_id": id_map.get(cid, cid)}
        else:
            resolved[pname] = pbind
    return resolved
