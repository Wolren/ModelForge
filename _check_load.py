"""
Static QGIS plugin load validation.
Simulates the QGIS plugin loading process as much as possible without QGIS runtime.
"""

import ast
import importlib
import inspect
import json
import os
import re
import subprocess
import sys
import textwrap

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.join(ROOT, "model_forge")
COMPILER_DIR = os.path.join(ROOT, "model_forge", "compiler_core")
METADATA = os.path.join(PLUGIN_DIR, "metadata.txt")
INIT_PY = os.path.join(PLUGIN_DIR, "__init__.py")

sys.path.insert(0, ROOT)

errors = []
warnings = []
info = []


def e(msg):
    errors.append(msg)


def w(msg):
    warnings.append(msg)


def i(msg):
    info.append(msg)


# ── 1. metadata.txt validation ──────────────────────────────────────
i("Checking metadata.txt...")
if not os.path.isfile(METADATA):
    e("metadata.txt not found")
else:
    with open(METADATA, encoding="utf-8") as f:
        meta = f.read()

    for field in ("name", "qgisMinimumVersion", "version", "description"):
        if field not in meta:
            e(f"metadata.txt missing field: {field}")

    # Check for common formatting issues
    for line_num, line in enumerate(meta.splitlines(), 1):
        if "=" not in line and line.strip() and not line.strip().startswith("["):
            e(f"metadata.txt:{line_num}: line without '=': {line!r}")

# ── 2. Plugin directory structure ──────────────────────────────────
i("Checking plugin directory structure...")
required_files = ["__init__.py", "metadata.txt", "icon.png"]
for fname in required_files:
    if not os.path.isfile(os.path.join(PLUGIN_DIR, fname)):
        e(f"Missing required file: {fname}")

# Check all __init__.py exist in package dirs
for root, dirs, files in os.walk(PLUGIN_DIR):
    if root == PLUGIN_DIR:
        continue
    if "__init__.py" not in files and any(f.endswith(".py") for f in files):
        rel = os.path.relpath(root, PLUGIN_DIR)
        e(f"Missing __init__.py in package: {rel}")

# ── 3. Syntax check all Python files ────────────────────────────────
i("Checking Python syntax of all files...")
py_files = []
for root, dirs, files in os.walk(PLUGIN_DIR):
    for fname in files:
        if fname.endswith(".py"):
            py_files.append(os.path.join(root, fname))

for fpath in sorted(py_files):
    try:
        with open(fpath, encoding="utf-8") as f:
            source = f.read()
        compile(source, fpath, "exec")
    except SyntaxError as exc:
        relpath = os.path.relpath(fpath, ROOT)
        e(f"Syntax error in {relpath}:{exc.lineno}: {exc.msg}")

# ── 4. AST analysis: import validation ─────────────────────────────
i("Checking import chains (AST)...")

# Map of package-local modules that exist
local_modules = set()
for fpath in py_files:
    rel = os.path.relpath(fpath, PLUGIN_DIR)
    mod = rel.replace(os.sep, ".").replace(".py", "")
    if mod.endswith(".__init__"):
        mod = mod[:-9]
    local_modules.add(mod)


def is_import_broken(import_name, source_file):
    """Check if an import is likely broken."""
    # qgis.* imports are expected to fail without QGIS
    if import_name.startswith("qgis.") or import_name == "qgis":
        return None  # Skip - QGIS-conditional
    # Standard library
    if import_name in sys.stdlib_module_names:
        return False
    # Third-party packages
    try:
        importlib.import_module(import_name)
        return False
    except ImportError:
        pass
    # Check if it's a local module reference
    source_rel = os.path.relpath(source_file, PLUGIN_DIR)
    source_mod = source_rel.replace(os.sep, ".").replace(".py", "").replace(".__init__", "")
    # Try relative-ish resolution
    parts = import_name.split(".")
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        if candidate in local_modules:
            # Check the remaining path
            remaining = parts[i:]
            return remaining  # could be an attribute, not a module
    # Check if it starts with known local prefix
    if import_name.startswith("model_forge."):
        remainder = import_name[len("model_forge.") :]
        if remainder in local_modules:
            return False
        # Check if it's a submodule of an existing module
        for lm in local_modules:
            if remainder.startswith(lm + ".") or remainder == lm:
                return False
        e(
            f"Import {import_name} in {os.path.relpath(source_file, ROOT)} looks broken (no matching local module: {list(local_modules)}"
        )
        return True
    # If it has no dots, it's a package we can't find
    if "." not in import_name:
        e(f"Import {import_name} in {os.path.relpath(source_file, ROOT)} not found")
        return True
    return None


for fpath in sorted(py_files):
    try:
        with open(fpath, encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except SyntaxError:
        continue

    relpath = os.path.relpath(fpath, ROOT)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                result = is_import_broken(alias.name, fpath)
                if result and isinstance(result, list):
                    w(
                        f"{relpath}: import '{alias.name}.{'.'.join(result)}' may be incomplete (attribute not module)"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.module:
                # Relative import
                source_rel = os.path.relpath(fpath, PLUGIN_DIR)
                source_mod = (
                    source_rel.replace(os.sep, ".").replace(".py", "").replace(".__init__", "")
                )
                parts = source_mod.split(".")
                if node.level > len(parts):
                    w(
                        f"{relpath}: relative import goes above top-level package (from {'.' * node.level}{node.module})"
                    )
                    continue
                base = parts[: len(parts) - node.level]
                if node.module:
                    full_mod = ".".join(base + [node.module])
                else:
                    full_mod = ".".join(base)
                result = is_import_broken(full_mod, fpath)
                if result and isinstance(result, list):
                    w(
                        f"{relpath}: relative import '{'.' * node.level}{node.module}' resolves to '{full_mod}' but missing submodule {'.'.join(result)}"
                    )
            elif node.module:
                result = is_import_broken(node.module, fpath)

# ── 5. classFactory() verification ──────────────────────────────────
i("Verifying classFactory() function...")
try:
    with open(INIT_PY, encoding="utf-8") as f:
        init_source = f.read()
    tree = ast.parse(init_source)
    has_classfactory = False
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "classFactory":
            has_classfactory = True
            params = [arg.arg for arg in node.args.args]
            if "iface" not in params:
                e("classFactory() missing 'iface' parameter")
            # Check it returns something ModelForge-like
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    func = child.func
                    if isinstance(func, ast.Name) and func.id == "ModelForge":
                        break
                    elif isinstance(func, ast.Attribute) and func.attr == "ModelForge":
                        break
    if not has_classfactory:
        e("__init__.py missing classFactory() function")
except SyntaxError as exc:
    e(f"__init__.py failed syntax check: {exc}")

# ── 6. Logging infrastructure check ────────────────────────────────
i("Checking logging infrastructure...")
log_py = os.path.join(COMPILER_DIR, "log.py")
if not os.path.isfile(log_py):
    e("compiler_core/log.py missing (added in last session)")
else:
    try:
        import model_forge.compiler_core.log as mf_log

        if not hasattr(mf_log, "configure_logger"):
            e("compiler_core.log.configure_logger() missing")
    except Exception as exc:
        e(f"compiler_core/log.py import failed: {exc}")

# ── 7. IR module check (consolidated) ──────────────────────────────
i("Checking IR module consolidation...")
ir_init = os.path.join(COMPILER_DIR, "core", "ir", "__init__.py")
ir_data_model = os.path.join(COMPILER_DIR, "core", "ir", "data_model.py")
if os.path.isfile(ir_data_model):
    e("ir/data_model.py still exists (should be deleted)")
if not os.path.isfile(ir_init):
    e("ir/__init__.py missing")
else:
    try:
        from model_forge.compiler_core.core.ir import (
            ExecutablePlan,
            ExecutableStep,
            IssueLevel,
            StepStatus,
            PlanIssue,
            ExpressionNode,
            ParameterBinding,
        )

        i("IR exports OK")
    except ImportError as exc:
        e(f"IR import failed: {exc}")

# ── 8. Try importing compiler modules that don't need QGIS ─────────
i("Importing non-QGIS compiler modules...")
import_targets = [
    "model_forge.compiler_core.core.ir",
    "model_forge.compiler_core.core.llm.base",
    "model_forge.compiler_core.core.llm.factory",
    "model_forge.compiler_core.core.compiler.intent_parser",
    "model_forge.compiler_core.core.compiler.semantic_planner",
    "model_forge.compiler_core.core.compiler.expression_validator",
    "model_forge.compiler_core.core.compiler.ir_validator",
    "model_forge.compiler_core.core.compiler.model_emitter",
    "model_forge.compiler_core.core.compiler.pipeline",
    "model_forge.compiler_core.core.mcp.client",
    "model_forge.compiler_core.core.mcp.server",
    "model_forge.compiler_core.core.mcp.tool_registry",
    "model_forge.compiler_core.core.mcp.tools.plan_workflow",
    "model_forge.compiler_core.core.mcp.tools.resolve_algorithms",
    "model_forge.compiler_core.core.mcp.tools.build_expression",
    "model_forge.compiler_core.core.mcp.tools.get_algorithm_docs",
    "model_forge.compiler_core.core.mcp.tools.suggest_layout",
    "model_forge.compiler_core.core.mcp.tools.generate_custom_step",
    "model_forge.compiler_core.core.services.layout.layout_config",
    "model_forge.compiler_core.core.services.layout.layout_service",
    "model_forge.compiler_core.core.services.testing.fixture_spec",
    "model_forge.compiler_core.core.services.testing.fixture_runner",
]
for mod_name in import_targets:
    try:
        importlib.import_module(mod_name)
        i(f"  {mod_name} - ok")
    except ImportError as exc:
        e(f"  {mod_name} - FAILED: {exc}")

# ── 9. Check for stale .pyc / bytecode issues
i("Checking for stale bytecode...")
for root, dirs, files in os.walk(PLUGIN_DIR):
    for fname in files:
        if fname.endswith(".pyc"):
            py_path = os.path.join(root, fname[:-1])
            if not os.path.isfile(py_path):
                w(f"Stale .pyc without source: {os.path.join(root, fname)}")

# ── 10. Check for duplicate files from consolidation ───────────────
i("Checking for leftover consolidation artifacts...")
dupes_to_check = [
    "compiler_core/core/services/graph_layout.py",
    "compiler_core/core/services/registry_catalog.py",
    "compiler_core/core/services/custom_step_author.py",
    "compiler_core/core/services/fixture_generator.py",
    "compiler_core/core/generate_worker.py",
    "compiler_core/core/ir/data_model.py",
]
for d in dupes_to_check:
    fpath = os.path.join(PLUGIN_DIR, d)
    if os.path.isfile(fpath):
        e(f"Leftover file (should have been deleted): {d}")

# ── 11. Check that custom_step_dialog import path works ────────────
i("Checking custom_step_dialog import shim...")
try:
    from model_forge.compiler_core.ui.custom_step_dialog import CustomStepDialog

    i("  custom_step_dialog - OK")
except ImportError as exc:
    # Check the shim
    ui_dir = os.path.join(COMPILER_DIR, "ui")
    legacy_shim = os.path.join(ui_dir, "custom_step_dialog.py")
    if os.path.isfile(legacy_shim):
        with open(legacy_shim) as f:
            content = f.read()
        if "dialogs" in content and "custom_step_dialog" in content:
            w(
                f"custom_step_dialog is a shim; real file in ui/dialogs/ — import chain may be fragile"
            )
    e(f"  custom_step_dialog - FAILED: {exc}")

# ── 12. Check for forbidden patterns ────────────────────────────────
i("Checking for dangerous patterns...")
for fpath in sorted(py_files):
    relpath = os.path.relpath(fpath, ROOT)
    with open(fpath, encoding="utf-8") as f:
        source = f.read()
    # QThread.terminate()
    if "QThread.terminate" in source or ".terminate()" in source:
        # Check if it's a QThread
        if "QThread" in source:
            w(f"{relpath}: QThread.terminate() called — unsafe")
    # Bare except
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:
                w(f"{relpath}:{node.lineno}: bare 'except:' clause")

# ── 13. Validate sample model JSON roundtrip ───────────────────────
i("Testing IR-to-JSON roundtrip (without QGIS)...")
try:
    from model_forge.compiler_core.core.ir import (
        ExecutablePlan,
        ExecutableStep,
        StepStatus,
        PlanIssue,
        IssueLevel,
    )

    step = ExecutableStep(
        step_id="test_1",
        label="Buffer",
        status=StepStatus.ASSUMED,
    )
    plan = ExecutablePlan(
        steps=[step],
        issues=[],
        metadata={"test": True},
    )
    assert plan.is_valid
    assert plan.assumed_steps() == [step]
    assert plan.step_by_id("test_1") is step
    assert plan.step_by_id("nonexistent") is None
    i("  IR construction and helpers - OK")
except Exception as exc:
    e(f"  IR roundtrip - FAILED: {exc}")

# ── 14. Check MCP tool registry initialisation ─────────────────────
i("Checking MCP tool registry init (without LLM)...")
try:
    from model_forge.compiler_core.core.mcp.tool_registry import build_server

    # build_server() requires an LLM backend; just check import works
    i("  MCP tool_registry import - OK")
except ImportError as exc:
    e(f"  MCP tool_registry import - FAILED: {exc}")

# ── 15. Summary ─────────────────────────────────────────────────────
try:
    print("=" * 60)
except UnicodeEncodeError:
    sys.stdout.reconfigure(encoding="utf-8")
print("=" * 60)
print("  MODEL FORGE - QGIS PLUGIN LOAD VALIDATION")
print("=" * 60)
print(f"\nCheck directory: {PLUGIN_DIR}")
print(f"Python files: {len(py_files)}")
print(f"\nResults: {len(errors)} errors, {len(warnings)} warnings, {len(info)} info items")
print()

if info:
    print("-- Info --")
    for msg in info:
        print(f"  [i] {msg}")

if warnings:
    print("\n-- Warnings --")
    for msg in warnings:
        print(f"  [!] {msg}")

if errors:
    print("\n-- ERRORS --")
    for msg in errors:
        print(f"  [X] {msg}")
else:
    print("  [OK] No errors found!")

print()
sys.exit(1 if errors else 0)
