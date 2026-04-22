"""
ModelBuilderBridge
==================
Translates a compiled model_json dict into a QgsProcessingModelAlgorithm
and opens it in the QGIS Model Designer.

Key design decisions
--------------------
1. addChildAlgorithm() returns the ACTUAL assigned childId — QGIS may
   silently rename the child (algorithmId_1, algorithmId_2 …) if the
   user-supplied id is empty or collides.  We always use the returned id,
   never the one from model_json["id"].

2. Parameter sources are injected via pure-Python XML patching of the
   .model3 file (QDomDocument / QgsXmlUtils schema).  We never hold a
   QgsProcessingModelChildParameterSource in Python — SIP drops the
   C++ value-type before QGIS copies it, causing silent nulls or AVs.

3. We do ONE toFile → patch → fromFile round-trip per child so the
   accumulating model always has the correct childIds before the next
   addChildAlgorithm() call.  That prevents id-collision renames from
   silently breaking cross-step links.

4. ModelerDialog.create(model) is the only correct API to open the
   designer.  iface.openProcessingModelDesigner() does not exist.
   create() appends the dialog to ModelerDialog.dlgs, preventing
   premature GC via the SIP/deleteonclose interaction.

.model3 XML parameter-source schema
-------------------------------------
Each param key maps to a List of source Maps:

  <Option type="List" name="PARAM_NAME">
    <Option type="Map">
      <Option type="int"     name="source"         value="0"/>   <!-- ModelParameter -->
      <Option type="QString" name="parameter_name" value="…"/>
    </Option>
  </Option>

  source=0  ModelParameter  → parameter_name
  source=1  ChildOutput     → child_id + output_name
  source=2  StaticValue     → static_value (typed leaf)
"""
from __future__ import annotations

import os
import tempfile
import xml.etree.ElementTree as ET
from typing import Any, Dict

try:
    from qgis.PyQt.QtCore import QPointF
    from qgis.core import (
        QgsApplication,
        QgsProcessingModelAlgorithm,
        QgsProcessingModelChildAlgorithm,
        QgsProcessingModelParameter,
    )
    _HAS_QGIS = True
except ImportError:
    _HAS_QGIS = False


# ---------------------------------------------------------------------------
# XML helpers — pure Python, zero C++ object contact
# ---------------------------------------------------------------------------

def _opt(name: str | None, typ: str, value: str | None = None) -> ET.Element:
    el = ET.Element("Option")
    if name is not None:
        el.set("name", name)
    el.set("type", typ)
    if value is not None:
        el.set("value", value)
    return el


def _source_element(pbind: Dict[str, Any]) -> ET.Element:
    src_map = ET.Element("Option")
    src_map.set("type", "Map")
    src_type = pbind.get("type", "static")

    if src_type == "model_input":
        src_map.append(_opt("source", "int", "0"))
        src_map.append(_opt("parameter_name", "QString",
                            pbind.get("input_name", "")))
    elif src_type == "child_output":
        src_map.append(_opt("source", "int", "1"))
        src_map.append(_opt("child_id",    "QString", pbind.get("child_id", "")))
        src_map.append(_opt("output_name", "QString",
                            pbind.get("output_name", "OUTPUT")))
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
    """
    Locate the <Option type="Map" name=ACTUAL_CHILD_ID> node inside
    <Option name="children" type="Map">.

    We match on the *actual* id (returned by addChildAlgorithm), which
    may differ from the user-supplied id in model_json.  The node's
    name attribute equals the assigned childId, so a direct name-match
    is both correct and fast.
    """
    for el in root.iter("Option"):
        if el.get("name") == "children" and el.get("type") == "Map":
            for child in el:
                if child.get("name") == actual_child_id:
                    return child
    return None


def _inject_params_xml(
    tree: ET.ElementTree,
    actual_child_id: str,
    bindings: Dict[str, Any],
) -> bool:
    """
    Replace <Option name="params" type="Map"> inside the child node
    (identified by ACTUAL_CHILD_ID) with one built from bindings.
    Returns True on success.
    """
    child_node = _find_child_node(tree.getroot(), actual_child_id)
    if child_node is None:
        return False

    for el in list(child_node):
        if el.get("name") == "params":
            child_node.remove(el)
            break

    params_el = ET.SubElement(child_node, "Option")
    params_el.set("type", "Map")
    params_el.set("name", "params")

    for pname, pbind in bindings.items():
        list_el = ET.SubElement(params_el, "Option")
        list_el.set("type", "List")
        list_el.set("name", pname)
        list_el.append(_source_element(pbind))

    return True


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------


def _resolve_ids(
    bindings: Dict[str, Any],
    id_map: Dict[str, str],
) -> Dict[str, Any]:
    """
    For every child_output binding whose child_id is a user-supplied id,
    replace it with the actual QGIS-assigned childId from id_map.
    Unknown ids are kept verbatim. Other binding types pass through unchanged.
    """
    resolved = {}
    for pname, pbind in bindings.items():
        if pbind.get("type") == "child_output":
            cid = pbind.get("child_id", "")
            resolved[pname] = {**pbind, "child_id": id_map.get(cid, cid)}
        else:
            resolved[pname] = pbind
    return resolved


if _HAS_QGIS:

    class ModelBuilderBridge:
        """
        Translates a compiled model_json dict into a QgsProcessingModelAlgorithm
        and opens it in the Processing Model Designer.
        """

        def __init__(self, iface=None):
            self.iface = iface

        # ------------------------------------------------------------------
        # Public API
        # ------------------------------------------------------------------

        def load_model_json(
            self,
            model_json: Dict[str, Any],
            open_designer: bool = True,
        ) -> QgsProcessingModelAlgorithm:
            model_name  = model_json.get("model_name",  "ModelForge Workflow")
            model_group = model_json.get("model_group", "ModelForge")

            model = QgsProcessingModelAlgorithm(model_name, model_group)

            for inp_def in model_json.get("inputs", []):
                mp = QgsProcessingModelParameter(inp_def["name"])
                mp.setDescription(inp_def.get("label", inp_def["name"]))
                mp.setPosition(QPointF(
                    float(inp_def.get("pos_x", 20.0)),
                    float(inp_def.get("pos_y", 20.0)),
                ))
                model.addModelParameter(
                    self._create_qgs_parameter(inp_def),
                    mp,
                )

            # id_map: user-supplied id → actual QGIS-assigned childId
            # Built incrementally so cross-step child_output refs are
            # always resolved against the real assigned ids.
            id_map: Dict[str, str] = {}

            for alg_dict in model_json.get("algorithms", []):
                model, id_map = self._add_child(model, alg_dict, id_map)

            if open_designer:
                self._open_in_designer(model)

            return model

        # ------------------------------------------------------------------
        # Designer
        # ------------------------------------------------------------------

        @staticmethod
        def _open_in_designer(model: QgsProcessingModelAlgorithm) -> None:
            """
            Open in the Processing Model Designer.

            ModelerDialog.create(model) is the only correct API:
              - Accepts QgsProcessingModelAlgorithm directly.
              - Calls model.create() + setSourceFilePath() internally.
              - Appends dialog to ModelerDialog.dlgs (SIP GC workaround).
            iface.openProcessingModelDesigner() does not exist.
            """
            from processing.modeler.ModelerDialog import ModelerDialog
            dlg = ModelerDialog.create(model)
            dlg.show()

        # ------------------------------------------------------------------
        # Core: one child per round-trip
        # ------------------------------------------------------------------

        def _add_child(
            self,
            model: QgsProcessingModelAlgorithm,
            alg_dict: Dict[str, Any],
            id_map: Dict[str, str],
        ) -> tuple[QgsProcessingModelAlgorithm, Dict[str, str]]:
            """
            Add one child algorithm to the model.

            Flow
            ----
            1. Build QgsProcessingModelChildAlgorithm (no param sources).
            2. Call addChildAlgorithm() and capture the ACTUAL assigned id.
            3. Record user_id → actual_id in id_map.
            4. Rewrite any child_output bindings that reference a user id
               with the corresponding actual id.
            5. If there are bindings, do toFile → patch XML → fromFile.
            """
            alg_id   = alg_dict.get("algorithm_id", "")
            user_id  = alg_dict.get("id", "")

            if not alg_id:
                return model, id_map
            if QgsApplication.processingRegistry().algorithmById(alg_id) is None:
                return model, id_map

            # Step 1 — build child without parameter sources
            child = QgsProcessingModelChildAlgorithm(alg_id)
            if user_id:
                child.setChildId(user_id)
            child.setDescription(alg_dict.get("description", alg_id))
            child.setPosition(QPointF(
                float(alg_dict.get("pos_x", 100.0)),
                float(alg_dict.get("pos_y", 100.0)),
            ))

            # Step 2 — add to model; capture ACTUAL assigned childId
            actual_id = model.addChildAlgorithm(child)

            # Step 3 — record mapping
            if user_id:
                id_map[user_id] = actual_id

            # Step 4 — rewrite cross-step references
            raw_bindings = alg_dict.get("parameters", {})
            if not raw_bindings:
                return model, id_map

            bindings = self._resolve_ids(raw_bindings, id_map)

            # Step 5 — toFile → patch → fromFile
            tmp = self._tmp_path(actual_id)
            try:
                if not model.toFile(tmp):
                    return model, id_map

                tree = ET.parse(tmp)
                ok = _inject_params_xml(tree, actual_id, bindings)
                if not ok:
                    return model, id_map

                tree.write(tmp, encoding="unicode", xml_declaration=False)

                fresh = QgsProcessingModelAlgorithm()
                if fresh.fromFile(tmp):
                    return fresh, id_map

                return model, id_map
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

        # ------------------------------------------------------------------
        # Resolve user-supplied ids → actual QGIS-assigned ids
        # ------------------------------------------------------------------

        @staticmethod
        def _resolve_ids(
            bindings: Dict[str, Any],
            id_map: Dict[str, str],
        ) -> Dict[str, Any]:
            return _resolve_ids(bindings, id_map)

        # ------------------------------------------------------------------
        # Parameter factory
        # ------------------------------------------------------------------

        @staticmethod
        def _create_qgs_parameter(inp_def: Dict[str, Any]):
            from qgis.core import (
                QgsProcessingParameterBoolean,
                QgsProcessingParameterCrs,
                QgsProcessingParameterExpression,
                QgsProcessingParameterExtent,
                QgsProcessingParameterFeatureSource,
                QgsProcessingParameterField,
                QgsProcessingParameterNumber,
                QgsProcessingParameterRasterLayer,
                QgsProcessingParameterString,
                QgsProcessingParameterVectorLayer,
            )
            name  = inp_def["name"]
            label = inp_def.get("label", name)
            kind  = inp_def.get("type", "string").lower()
            _map  = {
                "source":      lambda: QgsProcessingParameterFeatureSource(name, label),
                "vectorlayer": lambda: QgsProcessingParameterVectorLayer(name, label),
                "raster":      lambda: QgsProcessingParameterRasterLayer(name, label),
                "rasterlayer": lambda: QgsProcessingParameterRasterLayer(name, label),
                "number":      lambda: QgsProcessingParameterNumber(name, label),
                "boolean":     lambda: QgsProcessingParameterBoolean(name, label),
                "field":       lambda: QgsProcessingParameterField(name, label),
                "expression":  lambda: QgsProcessingParameterExpression(name, label),
                "crs":         lambda: QgsProcessingParameterCrs(name, label),
                "extent":      lambda: QgsProcessingParameterExtent(name, label),
                "string":      lambda: QgsProcessingParameterString(name, label),
            }
            return _map.get(kind, lambda: QgsProcessingParameterString(name, label))()

        # ------------------------------------------------------------------
        # Helpers
        # ------------------------------------------------------------------

        @staticmethod
        def _tmp_path(stem: str) -> str:
            safe = "".join(c if c.isalnum() else "_" for c in stem)[:32]
            fd, path = tempfile.mkstemp(
                suffix=".model3",
                prefix=f"mf_{safe}_",
            )
            os.close(fd)
            return path


else:

    class ModelBuilderBridge:
        """Fallback when QGIS is not available."""

        def __init__(self, iface=None):
            self.iface = iface

        def load_model_json(self, model_json, open_designer=True):
            raise RuntimeError(
                "ModelBuilderBridge requires a QGIS runtime environment."
            )

        @staticmethod
        def _resolve_ids(bindings, id_map):
            return _resolve_ids(bindings, id_map)