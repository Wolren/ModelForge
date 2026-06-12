"""
ModelBuilderBridge - builds QGIS models from JSON.
"""

from __future__ import annotations

import copy
import logging
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from typing import Any

log = logging.getLogger(__name__)

# Defensive QGIS import - log each step
_HAS_QGIS = False
_import_error = None

try:
    from qgis.PyQt.QtCore import QPointF

    log.debug("QPointF OK")
except Exception as e:
    log.debug("QPointF fail: %s", e)
    _import_error = e

try:
    from qgis.PyQt.QtWidgets import QMessageBox
except Exception:
    QMessageBox = None

if _import_error is None:
    try:
        import qgis.core as qgis_core

        log.debug("qgis.core OK")
    except Exception as e:
        log.debug("qgis.core fail: %s", e)
        _import_error = e

if _import_error is None:
    try:
        QNameApplication = qgis_core.QgsApplication
        QNameProcessingModelAlgorithm = qgis_core.QgsProcessingModelAlgorithm
        QNameProcessingModelChildAlgorithm = qgis_core.QgsProcessingModelChildAlgorithm
        QNameProcessingModelChildParameterSource = qgis_core.QgsProcessingModelChildParameterSource
        QNameProcessingModelParameter = qgis_core.QgsProcessingModelParameter
        log.debug("Classes OK")
    except Exception as e:
        log.debug("Classes fail: %s", e)
        _import_error = e

if _import_error is None:
    try:
        from .xml_helpers import _inject_params_xml
        from .xml_helpers import _resolve_ids as _resolve_ids_xml

        log.debug("xml_helpers OK")
    except ImportError:
        try:
            from model_forge.compiler_core.ui.xml_helpers import (
                _inject_params_xml,
            )
            from model_forge.compiler_core.ui.xml_helpers import (
                _resolve_ids as _resolve_ids_xml,
            )

            log.debug("xml_helpers fallback OK")
        except ImportError as e:
            log.debug("xml_helpers fail: %s", e)
            _import_error = e

if _import_error is None:
    _HAS_QGIS = True
    log.debug("QGIS imports successful")


if _HAS_QGIS:

    class ModelBuilderBridge:
        """Translates model JSON to QGIS Processing model."""

        def __init__(self, iface=None):
            self.iface = iface

        def load_model_json(
            self,
            model_json: dict[str, Any],
            open_designer: bool = True,
            auto_wire_missing: bool = True,
            prefer_project_outputs: bool = True,
            renaming_strategy: str = "preserve",
        ) -> QNameProcessingModelAlgorithm:
            if auto_wire_missing:
                model_json = self.auto_wire_model_json(
                    model_json,
                    prefer_project_outputs=prefer_project_outputs,
                    renaming_strategy=renaming_strategy,
                )

            model_name = model_json.get("model_name", "ModelForge Workflow")
            model_group = model_json.get("model_group", "ModelForge")

            model = QNameProcessingModelAlgorithm(model_name, model_group)

            for inp_def in model_json.get("inputs", []):
                mp = QNameProcessingModelParameter(inp_def["name"])
                mp.setDescription(self._wrap_label(inp_def.get("label", inp_def["name"]), width=28))
                mp.setPosition(
                    QPointF(
                        float(inp_def.get("pos_x", 40.0)),
                        float(inp_def.get("pos_y", 40.0)),
                    )
                )
                model.addModelParameter(
                    self._create_qgs_parameter(inp_def),
                    mp,
                )

            id_map: dict[str, str] = {}

            failed_steps: list[dict[str, str]] = []
            for alg_dict in model_json.get("algorithms", []):
                try:
                    model, id_map = self._add_child(model, alg_dict, id_map)
                except Exception as exc:
                    step_id = alg_dict.get("id", alg_dict.get("algorithm_id", "?"))
                    log.warning("Skipping corrupt step %r: %s", step_id, exc)
                    failed_steps.append({"id": step_id, "error": str(exc)})
                    continue

            model.updateDestinationParameters()

            if failed_steps:
                summary = "\n".join(f"  - {s['id']}: {s['error']}" for s in failed_steps)
                log.warning(
                    "%d step(s) were skipped due to errors:\n%s",
                    len(failed_steps),
                    summary,
                )
                if QMessageBox is not None:
                    QMessageBox.warning(
                        None,
                        "Model Forge",
                        f"{len(failed_steps)} step(s) were skipped due to errors:\n{summary}",
                    )

            if open_designer:
                self._open_in_designer(model)

            return model

        def auto_wire_model_json(
            self,
            model_json: dict[str, Any],
            prefer_project_outputs: bool = True,
            renaming_strategy: str = "preserve",
        ) -> dict[str, Any]:
            """Auto-wire missing parameter bindings - simplified robust version."""
            result = copy.deepcopy(model_json or {})
            algorithms = result.get("algorithms", [])
            inputs = result.get("inputs", [])
            self._apply_step_renaming(result, strategy=renaming_strategy)

            # Build map of input names
            input_names = {
                inp.get("name", ""): inp.get("name", "") for inp in inputs if inp.get("name")
            }
            normalized_input_map = {self._normalize_token(name): name for name in input_names}

            # Track producer steps for linking
            producers = []

            for alg in algorithms:
                step_id = str(alg.get("id", "") or "")

                # Get existing parameters
                params = alg.setdefault("parameters", {})

                # Get algorithm's expected INPUT parameter names (heuristic: common QGIS input names)
                expected_inputs = self._get_expected_inputs(alg.get("algorithm_id", ""))

                for inp_name in expected_inputs:
                    if inp_name in params:
                        continue

                    # Try to match with model input
                    matched = normalized_input_map.get(self._normalize_token(inp_name))
                    if matched:
                        params[inp_name] = {"type": "model_input", "input_name": matched}
                        continue

                    # Try to link from previous producer
                    if producers:
                        prev_step_id = producers[-1]
                        prev_alg = next(
                            (a for a in algorithms if a.get("id") == prev_step_id),
                            None,
                        )
                        output_name = "OUTPUT"
                        if prev_alg:
                            prev_alg_id = prev_alg.get("algorithm_id", "")
                            svc = ModelBuilderBridge if _HAS_QGIS else None
                            if svc and prev_alg_id:
                                registry = qgis_core.QgsApplication.processingRegistry()
                                qgs_alg = registry.algorithmById(prev_alg_id)
                                if qgs_alg:
                                    output_name = svc._preferred_output_name(qgs_alg)
                        params[inp_name] = {
                            "type": "child_output",
                            "child_id": prev_step_id,
                            "output_name": output_name,
                        }

                # Handle OUTPUT parameter
                if prefer_project_outputs:
                    output_param = self._get_output_param(alg.get("algorithm_id", ""))
                    if output_param and output_param not in params:
                        params[output_param] = {
                            "type": "static",
                            "value": "TEMPORARY_OUTPUT",
                        }

                # Register as producer for next steps
                if step_id:
                    producers.append(step_id)

            return result

        def _get_expected_inputs(self, algorithm_id: str) -> list[str]:
            """Get expected input parameter names for common QGIS algorithms."""
            id_lower = (algorithm_id or "").lower()

            # Common vector algorithms and their inputs
            alg_inputs = {
                "extractbyexpression": ["INPUT", "EXPRESSION"],
                "intersection": ["INPUT", "OVERLAY", "OUTPUT"],
                "difference": ["INPUT", "OVERLAY", "OUTPUT"],
                "clipvectorbypolygon": ["INPUT", "OVERLAY", "OUTPUT"],
                "buffervectors": ["INPUT", "DISTANCE", "OUTPUT"],
                "multiparttosingleparts": ["INPUT", "OUTPUT"],
            }

            # Check algorithm base name
            base = id_lower.split(":")[-1] if ":" in id_lower else id_lower

            return alg_inputs.get(base, ["INPUT", "OUTPUT"])

        def _get_output_param(self, algorithm_id: str) -> str:
            """Get output parameter name for algorithm."""
            id_lower = (algorithm_id or "").lower()

            if "extract" in id_lower:
                return "OUTPUT"
            if "buffer" in id_lower:
                return "OUTPUT"
            if "intersect" in id_lower:
                return "OUTPUT"
            if "difference" in id_lower:
                return "OUTPUT"
            if "clip" in id_lower:
                return "OUTPUT"
            if "multipart" in id_lower:
                return "OUTPUT"

            return "OUTPUT"

        def _open_in_designer(self, model: QNameProcessingModelAlgorithm) -> None:
            from processing.modeler.ModelerDialog import ModelerDialog

            dlg = ModelerDialog.create(model)
            dlg.show()

        def _add_child(
            self,
            model: QNameProcessingModelAlgorithm,
            alg_dict: dict[str, Any],
            id_map: dict[str, str],
        ) -> tuple[QNameProcessingModelAlgorithm, dict[str, str]]:
            alg_id = alg_dict.get("algorithm_id", "")
            user_id = alg_dict.get("id", "")

            if not alg_id:
                return model, id_map

            registry = QNameApplication.processingRegistry()
            qgs_alg = registry.algorithmById(alg_id)
            if qgs_alg is None and alg_id:
                log.debug("_add_child looking for: %s", alg_id)
                base = alg_id
                if ":" in alg_id:
                    base = alg_id.split(":", 1)[1]
                for prefix in ("native:", "qgis:", "gdal:", "grass:", "saga:"):
                    qgs_alg = registry.algorithmById(prefix + base)
                    if qgs_alg:
                        log.debug("_add_child found: %s", prefix + base)
                        alg_dict["algorithm_id"] = prefix + base
                        alg_id = prefix + base
                        break

            if qgs_alg is None:
                log.warning("Algorithm %r not found in registry, skipping", alg_id)
                return model, id_map

            child = QNameProcessingModelChildAlgorithm(alg_id)
            if user_id:
                child.setChildId(user_id)
            child.setDescription(self._wrap_label(alg_dict.get("description", alg_id), width=32))
            child.setPosition(
                QPointF(
                    float(alg_dict.get("pos_x", 100.0)),
                    float(alg_dict.get("pos_y", 80.0)),
                )
            )

            actual_id = model.addChildAlgorithm(child)
            if user_id:
                id_map[user_id] = actual_id

            raw_bindings = alg_dict.get("parameters", {})
            if not raw_bindings:
                return model, id_map

            bindings = self._resolve_ids(raw_bindings, id_map)
            tmp = self._tmp_path(actual_id)

            try:
                if not model.toFile(tmp):
                    self._apply_bindings_direct(model, actual_id, bindings)
                    return model, id_map

                tree = ET.parse(tmp)
                ok = _inject_params_xml(tree, actual_id, bindings)
                if not ok:
                    self._apply_bindings_direct(model, actual_id, bindings)
                    return model, id_map

                tree.write(tmp, encoding="unicode", xml_declaration=False)
                fresh = QNameProcessingModelAlgorithm()
                if fresh.fromFile(tmp):
                    return fresh, id_map

                self._apply_bindings_direct(model, actual_id, bindings)
                return model, id_map
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

        def _create_qgs_parameter(self, inp_def: dict[str, Any]):
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

            name = inp_def["name"]
            label = inp_def.get("label", name)
            kind = inp_def.get("type", "string").lower()
            _map = {
                "source": lambda: QgsProcessingParameterFeatureSource(name, label),
                "vectorlayer": lambda: QgsProcessingParameterVectorLayer(name, label),
                "raster": lambda: QgsProcessingParameterRasterLayer(name, label),
                "rasterlayer": lambda: QgsProcessingParameterRasterLayer(name, label),
                "number": lambda: QgsProcessingParameterNumber(name, label),
                "boolean": lambda: QgsProcessingParameterBoolean(name, label),
                "field": lambda: QgsProcessingParameterField(name, label),
                "expression": lambda: QgsProcessingParameterExpression(name, label),
                "crs": lambda: QgsProcessingParameterCrs(name, label),
                "extent": lambda: QgsProcessingParameterExtent(name, label),
                "string": lambda: QgsProcessingParameterString(name, label),
            }
            return _map.get(kind, lambda: QgsProcessingParameterString(name, label))()

        def _resolve_ids(self, bindings: dict[str, Any], id_map: dict[str, str]) -> dict[str, Any]:
            return _resolve_ids_xml(bindings, id_map)

        def _apply_bindings_direct(
            self, model: QNameProcessingModelAlgorithm, child_id: str, bindings: dict[str, Any]
        ) -> None:
            try:
                child = model.childAlgorithm(child_id)
            except Exception:
                log.warning("childAlgorithm(%s) not found", child_id)
                child = None
            if child is None:
                return
            for pname, pbind in bindings.items():
                if not isinstance(pbind, dict):
                    continue
                try:
                    sources = self._binding_sources(pbind)
                    if sources:
                        child.addParameterSources(pname, sources)
                except Exception:
                    log.warning("addParameterSources(%s) failed for binding %r", pname, pbind)
                    continue

        @staticmethod
        def _tmp_path(stem: str) -> str:
            safe = "".join(c if c.isalnum() else "_" for c in stem)[:32]
            fd, path = tempfile.mkstemp(suffix=".model3", prefix=f"mf_{safe}_")
            os.close(fd)
            return path

        @staticmethod
        def _normalize_token(value: str) -> str:
            return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())

        @classmethod
        def _match_input_name(
            cls, param_name: str, normalized_input_map: dict[str, str]
        ) -> str | None:
            pnorm = cls._normalize_token(param_name)
            if pnorm in normalized_input_map:
                return normalized_input_map[pnorm]
            for key, original in normalized_input_map.items():
                if pnorm and (pnorm in key or key in pnorm):
                    return original
            return None

        @staticmethod
        def _is_destination_param(pdef) -> bool:
            cname = pdef.__class__.__name__.lower()
            return "destination" in cname or "sink" in cname

        @staticmethod
        def _destination_default_value(pdef, is_last_step: bool) -> str:
            if not is_last_step:
                return "TEMPORARY_OUTPUT"
            cname = pdef.__class__.__name__.lower()
            if "raster" in cname:
                return "TEMPORARY_OUTPUT"
            if any(h in cname for h in ("sink", "vector", "feature", "destination")):
                return "memory:"
            return "TEMPORARY_OUTPUT"

        @staticmethod
        def _should_replace_destination_binding(binding: Any, is_last_step: bool) -> bool:
            if not isinstance(binding, dict):
                return True
            if binding.get("type") != "static":
                return False
            current = str(binding.get("value", "") or "").strip().lower()
            if not current:
                return True
            if current == "temporary_output":
                return True
            if is_last_step and current in ("output", "result"):
                return True
            return False

        @staticmethod
        def _expects_layer_like_input(pdef) -> bool:
            cname = pdef.__class__.__name__.lower()
            hints = ("source", "vector", "raster", "layer", "feature", "mesh", "pointcloud")
            return any(h in cname for h in hints)

        @staticmethod
        def _binding_sources(binding: dict[str, Any]):
            btype = binding.get("type", "static")
            if btype == "model_input":
                name = str(binding.get("input_name", "") or "")
                if not name:
                    return []
                return [QNameProcessingModelChildParameterSource.fromModelParameter(name)]
            if btype == "child_output":
                child_id = str(binding.get("child_id", "") or "")
                output_name = str(binding.get("output_name", "") or "OUTPUT")
                if not child_id:
                    return []
                return [
                    QNameProcessingModelChildParameterSource.fromChildOutput(child_id, output_name)
                ]
            return [QNameProcessingModelChildParameterSource.fromStaticValue(binding.get("value"))]

        @staticmethod
        def _wrap_label(text: str, width: int = 36) -> str:
            raw = str(text or "").strip()
            if not raw or width <= 0:
                return raw
            words = raw.split()
            if not words:
                return raw
            lines = []
            line = words[0]
            for word in words[1:]:
                if len(line) + 1 + len(word) <= width:
                    line += f" {word}"
                else:
                    lines.append(line)
                    line = word
            lines.append(line)
            return "\n".join(lines)

        @staticmethod
        def _is_layer_like_output(odef) -> bool:
            cname = odef.__class__.__name__.lower()
            layer_hints = (
                "vector",
                "raster",
                "feature",
                "sink",
                "layer",
                "mesh",
                "pointcloud",
                "table",
            )
            scalar_hints = ("number", "string", "boolean", "html", "folder", "file")
            if any(h in cname for h in scalar_hints):
                return False
            return any(h in cname for h in layer_hints)

        @classmethod
        def _preferred_output_name(cls, qgs_alg, layer_like_only: bool = False) -> str:
            outputs = []
            try:
                outputs = list(qgs_alg.outputDefinitions())
            except Exception:
                log.warning("outputDefinitions() failed for %s", qgs_alg)
                outputs = []

            if not outputs:
                return "OUTPUT"

            if layer_like_only:
                for odef in outputs:
                    if cls._is_layer_like_output(odef):
                        return str(odef.name() or "OUTPUT")
                for odef in outputs:
                    oname = str(odef.name() or "").lower()
                    if any(
                        kw in oname for kw in ("output", "result", "layer", "table", "features")
                    ):
                        return str(odef.name() or "OUTPUT")
                return ""

            preferred = ("OUTPUT", "RESULT", "OUTPUT_LAYER", "OUTPUT_TABLE", "OUTPUT_VECTOR")
            by_name = {str(odef.name() or "").upper(): str(odef.name() or "") for odef in outputs}
            for key in preferred:
                if key in by_name and by_name[key]:
                    return by_name[key]

            first = str(outputs[0].name() or "")
            return first or "OUTPUT"

        @classmethod
        def _pick_upstream_output(
            cls,
            alg: dict[str, Any],
            producer_outputs: dict[str, dict[str, str]],
            previous_layer_producers: list[str],
            previous_any_producers: list[str],
        ) -> tuple[str, str]:
            deps = alg.get("depends_on", [])
            if isinstance(deps, str):
                deps = [deps]
            if not isinstance(deps, list):
                deps = []

            for step_id in reversed([str(d) for d in deps if d]):
                out = producer_outputs.get(step_id)
                if out and out.get("layer"):
                    return step_id, out["layer"]
                if out and out.get("any"):
                    return step_id, out["any"]

            all_producers = list(reversed(previous_any_producers + previous_layer_producers))
            seen = set()
            for step_id in all_producers:
                if step_id in seen:
                    continue
                seen.add(step_id)
                out = producer_outputs.get(step_id)
                if out:
                    if out.get("layer"):
                        return step_id, out["layer"]
                    if out.get("any"):
                        return step_id, out["any"]
            return "", ""

        @staticmethod
        def _to_json_scalar(value):
            if value is None:
                return None
            if isinstance(value, (bool, int, float, str)):
                return value
            return str(value)

        @classmethod
        def _apply_step_renaming(
            cls, model_json: dict[str, Any], strategy: str = "preserve"
        ) -> None:
            algorithms = model_json.get("algorithms", [])
            if not isinstance(algorithms, list):
                return
            strategy = (strategy or "preserve").lower()
            used: set[str] = set()
            id_map: dict[str, str] = {}

            for idx, alg in enumerate(algorithms, start=1):
                old_id = str(alg.get("id", "") or "")
                new_id = cls._compute_step_id(alg, idx, strategy, used)
                alg["id"] = new_id
                if old_id:
                    id_map[old_id] = new_id

            for alg in algorithms:
                params = alg.get("parameters", {})
                if not isinstance(params, dict):
                    continue
                for pbind in params.values():
                    if isinstance(pbind, dict) and pbind.get("type") == "child_output":
                        child_id = str(pbind.get("child_id", "") or "")
                        if child_id in id_map:
                            pbind["child_id"] = id_map[child_id]

        @classmethod
        def _compute_step_id(
            cls, alg: dict[str, Any], idx: int, strategy: str, used: set[str]
        ) -> str:
            raw_id = str(alg.get("id", "") or "")
            label = str(alg.get("description", "") or "")
            alg_id = str(alg.get("algorithm_id", "") or "")

            if strategy == "suffix_counter":
                base = cls._slug(raw_id) or "step"
                return cls._unique_id(f"{base}_{idx}", used)

            if strategy == "label_slug":
                base = cls._slug(label) or cls._slug(alg_id.rsplit(":", maxsplit=1)[-1]) or "step"
                return cls._unique_id(f"{base}_{idx}", used)

            base = cls._slug(raw_id) or "step"
            candidate = raw_id if raw_id and raw_id not in used else base
            return cls._unique_id(cls._slug(candidate) or "step", used)

        @staticmethod
        def _slug(value: str) -> str:
            value = str(value or "").strip().lower()
            return re.sub(r"[^a-z0-9]+", "_", value).strip("_")

        @staticmethod
        def _unique_id(base: str, used: set[str]) -> str:
            if base not in used:
                used.add(base)
                return base
            i = 2
            while f"{base}_{i}" in used:
                i += 1
            result = f"{base}_{i}"
            used.add(result)
            return result


else:

    class ModelBuilderBridge:
        """Fallback when QGIS is not available."""

        def __init__(self, iface=None):
            self.iface = iface

        def load_model_json(self, model_json, open_designer=True):
            raise RuntimeError("ModelBuilderBridge requires QGIS runtime.")

        def auto_wire_model_json(
            self, model_json, prefer_project_outputs=True, renaming_strategy="preserve"
        ):
            raise RuntimeError("ModelBuilderBridge requires QGIS runtime.")
