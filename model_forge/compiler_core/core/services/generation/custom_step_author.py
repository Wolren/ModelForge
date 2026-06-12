"""
CustomStepAuthorService
========================
Manages the full lifecycle of custom algorithm steps:
  1. Stores and loads CustomStepSpec JSON
  2. Generates QgsProcessingAlgorithm Python code from specs
  3. Provides code validation (no-QGIS AST lint)

Generated algorithms are saved to model_forge/user_steps/ and registered
via ModelForgeProvider so they appear in the toolbox and can be inserted
into model graphs like any native algorithm.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

_STEPS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "user_steps")


def _sanitize_id(step_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", step_id)


def _to_qgis_type(kind: str) -> str:
    return {
        "vectorlayer": "QgsProcessingParameterVectorLayer",
        "rasterlayer": "QgsProcessingParameterRasterLayer",
        "number": "QgsProcessingParameterNumber",
        "string": "QgsProcessingParameterString",
        "boolean": "QgsProcessingParameterBoolean",
        "field": "QgsProcessingParameterField",
        "expression": "QgsProcessingParameterExpression",
        "crs": "QgsProcessingParameterCrs",
        "extent": "QgsProcessingParameterExtent",
        "sink": "QgsProcessingParameterFeatureSink",
        "featuresink": "QgsProcessingParameterFeatureSink",
    }.get(kind.lower(), "QgsProcessingParameterString")


def _to_output_type(kind: str) -> str:
    # QGIS 3.28 has no QgsProcessingOutputBoolean; use Number as closest compat
    if kind.lower() == "boolean":
        return "QgsProcessingOutputNumber"
    return {
        "vector": "QgsProcessingOutputVectorLayer",
        "raster": "QgsProcessingOutputRasterLayer",
        "number": "QgsProcessingOutputNumber",
        "string": "QgsProcessingOutputString",
    }.get(kind.lower(), "QgsProcessingOutputString")


# ─── Data model ──────────────────────────────────────────────────────────────


@dataclass
class ParamDef:
    name: str
    kind: str
    description: str = ""
    optional: bool = False
    default_value: Any = None


@dataclass
class OutputDef:
    name: str
    kind: str
    description: str = ""


@dataclass
class CustomStepSpec:
    step_id: str
    display_name: str
    group: str = "User Steps"
    group_id: str = "user_steps"
    help_text: str = ""
    parameters: list[ParamDef] = field(default_factory=list)
    outputs: list[OutputDef] = field(default_factory=list)
    code_body: str = "result = {}\nreturn result"
    version: int = 1

    def to_dict(self) -> dict:
        return {
            "step_id": self.step_id,
            "display_name": self.display_name,
            "group": self.group,
            "group_id": self.group_id,
            "help_text": self.help_text,
            "version": self.version,
            "parameters": [
                {
                    "name": p.name,
                    "kind": p.kind,
                    "description": p.description,
                    "optional": p.optional,
                    "default_value": p.default_value,
                }
                for p in self.parameters
            ],
            "outputs": [
                {"name": o.name, "kind": o.kind, "description": o.description} for o in self.outputs
            ],
            "code_body": self.code_body,
        }

    @classmethod
    def from_dict(cls, d: dict) -> CustomStepSpec:
        return cls(
            step_id=d["step_id"],
            display_name=d["display_name"],
            group=d.get("group", "User Steps"),
            group_id=d.get("group_id", "user_steps"),
            help_text=d.get("help_text", ""),
            version=d.get("version", 1),
            parameters=[
                ParamDef(
                    name=p["name"],
                    kind=p["kind"],
                    description=p.get("description", ""),
                    optional=p.get("optional", False),
                    default_value=p.get("default_value"),
                )
                for p in d.get("parameters", [])
            ],
            outputs=[
                OutputDef(name=o["name"], kind=o["kind"], description=o.get("description", ""))
                for o in d.get("outputs", [])
            ],
            code_body=d.get("code_body", "result = {}\nreturn result"),
        )


# ─── Service ─────────────────────────────────────────────────────────────────


class CustomStepAuthorService:
    def __init__(self, steps_dir: str | None = None):
        self.steps_dir = steps_dir or _STEPS_DIR
        os.makedirs(self.steps_dir, exist_ok=True)

    # ── CRUD ────────────────────────────────────────────────────────────

    def list_specs(self) -> list[CustomStepSpec]:
        specs = []
        for fname in sorted(os.listdir(self.steps_dir)):
            if fname.endswith(".mf_step.json"):
                try:
                    with open(os.path.join(self.steps_dir, fname), encoding="utf-8") as f:
                        specs.append(CustomStepSpec.from_dict(json.load(f)))
                except Exception:
                    log.debug("Skipping corrupt spec file: %s", fname)
        return specs

    def save_spec(self, spec: CustomStepSpec):
        path = os.path.join(self.steps_dir, f"{_sanitize_id(spec.step_id)}.mf_step.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(spec.to_dict(), f, indent=2, ensure_ascii=False)

    def generate_and_save(self, spec: CustomStepSpec) -> str:
        """Save spec JSON and emit a Python algorithm file. Returns the .py path."""
        self.save_spec(spec)
        py_path = os.path.join(self.steps_dir, f"{_sanitize_id(spec.step_id)}.py")
        code = self._generate_code(spec)
        with open(py_path, "w", encoding="utf-8") as f:
            f.write(code)
        return py_path

    # ── Code generation ────────────────────────────────────────────────

    def _generate_code(self, spec: CustomStepSpec) -> str:
        safe_id = _sanitize_id(spec.step_id)
        class_name = "".join(w.capitalize() for w in safe_id.split("_")) + "Algorithm"

        param_inits = []
        for p in spec.parameters:
            qtype = _to_qgis_type(p.kind)
            opt_flag = "QgsProcessingParameterDefinition.Flag.FlagOptional" if p.optional else "0"
            param_inits.append(
                f"        self.addParameter({qtype}(\n"
                f"            '{p.name}',\n"
                f"            self.tr('{p.description or p.name}'),\n"
                f"            flags=QgsProcessingParameterDefinition.Flags({opt_flag}),\n"
                f"        ))"
            )

        output_inits = []
        for o in spec.outputs:
            otype = _to_output_type(o.kind)
            output_inits.append(
                f"        self.addOutput({otype}(\n"
                f"            '{o.name}', self.tr('{o.description or o.name}')))\n"
            )

        param_retrievals = []
        for p in spec.parameters:
            if p.kind in ("vectorlayer",):
                method = "parameterAsVectorLayer"
                args = f"parameters, '{p.name}', context"
            elif p.kind in ("rasterlayer",):
                method = "parameterAsRasterLayer"
                args = f"parameters, '{p.name}', context"
            elif p.kind in ("number",):
                method = "parameterAsDouble"
                args = f"parameters, '{p.name}', context"
            elif p.kind in ("boolean",):
                method = "parameterAsBool"
                args = f"parameters, '{p.name}', context"
            elif p.kind in ("sink", "featuresink"):
                method = "parameterAsSink"
                args = f"parameters, '{p.name}', context, dest_id_{p.name}, fields, QgsWkbTypes.NoGeometry, QgsCoordinateReferenceSystem()"
            else:
                method = "parameterAsString"
                args = f"parameters, '{p.name}', context"
            param_retrievals.append(f"        {p.name.upper()} = self.{method}({args})")

        # Indent code_body
        body_lines = spec.code_body.splitlines()
        indented_body = "\n".join("        " + line for line in body_lines)

        param_init_block = "\n".join(param_inits) or "        pass"
        output_init_block = "\n".join(output_inits) or ""
        param_retr_block = "\n".join(param_retrievals) or ""

        return f'''# AUTO-GENERATED by ModelForge CustomStepAuthorService v{spec.version}
# Step ID: {spec.step_id}
# DO NOT EDIT - regenerate from the CustomStepSpec JSON instead.
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterDefinition,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterField,
    QgsProcessingParameterExpression,
    QgsProcessingParameterCrs,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFeatureSink,
    QgsProcessingOutputVectorLayer,
    QgsProcessingOutputRasterLayer,
    QgsProcessingOutputNumber,
    QgsProcessingOutputString,
    QgsProcessingException,
    QgsCoordinateReferenceSystem,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QCoreApplication


class {class_name}(QgsProcessingAlgorithm):
    """
    {spec.help_text}
    Auto-generated by ModelForge.
    """

    def tr(self, message):
        return QCoreApplication.translate('{class_name}', message)

    def name(self):
        return '{safe_id}'

    def displayName(self):
        return self.tr('{spec.display_name}')

    def group(self):
        return self.tr('{spec.group}')

    def groupId(self):
        return '{spec.group_id}'

    def shortHelpString(self):
        return self.tr('{spec.help_text}')

    def createInstance(self):
        return {class_name}()

    def initAlgorithm(self, config=None):
{param_init_block}
{output_init_block}

    def processAlgorithm(self, parameters, context, feedback):
        if feedback.isCanceled():
            return {{}}
{param_retr_block}

        # ── User code body ──────────────────────────────────────────────
{indented_body}
        # ── End user code ───────────────────────────────────────────────
'''

    # ── Validation ─────────────────────────────────────────────────────

    def validate_code_body(self, code: str) -> list[str]:
        """AST-based lint that does NOT require QGIS to be present."""
        errors = []

        # Syntax check
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return [f"SyntaxError at line {e.lineno}: {e.msg}"]

        # Banned names
        BANNED: dict[str, str] = {
            "iface": "Direct iface access is not allowed in Processing algorithms.",
            "QMessageBox": "GUI dialogs must not be called from Processing algorithms.",
            "subprocess": "subprocess is not allowed; use QgsProcessingException for errors.",
            "os.system": "os.system is not allowed.",
            "eval": "eval() is not allowed for security reasons.",
            "exec": "exec() is not allowed for security reasons.",
            "compile": "compile() is not allowed for security reasons.",
            "__import__": "__import__() is not allowed for security reasons.",
            "getattr": "getattr() is not allowed — dynamic attribute access bypasses validation.",
            "setattr": "setattr() is not allowed for security reasons.",
            "delattr": "delattr() is not allowed for security reasons.",
        }
        BANNED_ATTR: dict[str, str] = {
            "os.system": "os.system is not allowed.",
            "os.popen": "os.popen is not allowed.",
            "subprocess.Popen": "subprocess is not allowed.",
            "subprocess.call": "subprocess is not allowed.",
            "subprocess.run": "subprocess is not allowed.",
            "ctypes.CDLL": "ctypes.CDLL is not allowed for security reasons.",
            "ctypes.WinDLL": "ctypes is not allowed for security reasons.",
            "ctypes.CLibrary": "ctypes is not allowed for security reasons.",
            "socket.socket": "socket is not allowed.",
            "socket.create_connection": "socket is not allowed.",
            "requests.get": "requests is not allowed.",
            "requests.post": "requests is not allowed.",
            "requests.request": "requests is not allowed.",
            "urllib.request.urlopen": "urllib is not allowed.",
            "urllib.request.Request": "urllib is not allowed.",
            "pickle.load": "pickle is not allowed.",
            "pickle.dumps": "pickle is not allowed.",
            "shutil.copyfile": "shutil is not allowed.",
            "shutil.move": "shutil is not allowed.",
        }

        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in BANNED:
                errors.append(f"Line {node.lineno}: {BANNED[node.id]}")
            if isinstance(node, ast.Attribute):
                val_id = getattr(node.value, "id", "") if isinstance(node.value, ast.Name) else ""
                full = f"{val_id}.{node.attr}" if val_id else ""
                if full in BANNED_ATTR:
                    errors.append(f"Line {node.lineno}: {BANNED_ATTR[full]}")

        return errors
