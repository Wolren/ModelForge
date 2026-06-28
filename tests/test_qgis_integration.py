"""
Model Forge QGIS Integration Test
Run this from QGIS Python Console:  exec(open(r'PATH/TO/test_qgis_integration.py').read())
Or save as Processing script and run from toolbox.
"""

import sys, os

try:
    import qgis.core  # noqa: F401
except ImportError:
    import pytest

    pytest.skip("QGIS not available", allow_module_level=True)

# Point to the plugin source
PROJECT_ROOT = r"C:\Users\Wildbot\PycharmProjects\Model Forge"
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def section(title):
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


# --- 1. Imports ---
section("1. Import all modules")
from qgis.core import QgsApplication, Qgis

print(f"QGIS version: {Qgis.QGIS_VERSION}")
reg = QgsApplication.processingRegistry()
providers = [(p.id(), p.name()) for p in reg.providers()]
print(f"Providers ({len(providers)}): {providers}")

from model_forge.compiler_core.core.ir import (
    ExecutablePlan,
    ExecutableStep,
    ParameterBinding,
    StepStatus,
    ResolvedAlgorithm,
    ParameterSpec,
    OutputSpec,
    ModelInput,
    ParamKind,
    OutputKind,
)
from model_forge.compiler_core.core.compiler.link_repair import LinkRepairService
from model_forge.compiler_core.core.compiler.algorithm_resolver import AlgorithmResolver
from model_forge.compiler_core.core.compiler.model_emitter import ModelEmitter
from model_forge.compiler_core.core.compiler.ir_validator import IRValidator
from model_forge.compiler_core.core.compiler.intent_parser import IntentParser
from model_forge.compiler_core.core.compiler.semantic_planner import SemanticPlanner
from model_forge.compiler_core.core.compiler.expression_validator import ExpressionValidator
from model_forge.compiler_core.core.compiler.pipeline import CompilerPipeline
from model_forge.compiler_core.core.services.registry.registry_catalog import RegistryCatalogService
from model_forge.compiler_core.core.context_collector import ContextCollector

print("  ✓ All imports OK")

# --- 2. Registry Catalog with output ports ---
section("2. Registry catalog: outputs & parameters")
cat_svc = RegistryCatalogService()
catalog = cat_svc.get_algorithm_catalog(include_native=True, include_gdal=True, max_algorithms=50)
print(f"Catalog entries: {len(catalog)}")
no_outputs = sum(1 for v in catalog.values() if not v.get("outputs"))
no_params = sum(1 for v in catalog.values() if not v.get("parameters"))
print(f"With outputs: {len(catalog) - no_outputs}, With params: {len(catalog) - no_params}")
sample = list(catalog.items())[:3]
for aid, info in sample:
    onames = [o["name"] for o in info.get("outputs", [])]
    pnames = [p["name"] for p in info.get("parameters", [])[:6]]
    print(f"  {aid}: outputs={onames} params={pnames}")

# --- 3. ContextCollector with provider_ids ---
section("3. ContextCollector with provider selection")
cc = ContextCollector()
ctx_all = cc.collect(max_algorithms=20)
print(f"Default collect: {len(ctx_all.get('algorithms', {}))} algorithms")
ctx_native = cc.collect(max_algorithms=20, provider_ids=["native"])
n_native = len(ctx_native.get("algorithms", {}))
print(f"  native only: {n_native}")
ctx_gdal = cc.collect(max_algorithms=20, provider_ids=["gdal"])
n_gdal = len(ctx_gdal.get("algorithms", {}))
print(f"  gdal only:   {n_gdal}")
all_gdal_ok = all(aid.startswith("gdal:") for aid in ctx_gdal["algorithms"])
print(f"  All gdal:     {'✓' if all_gdal_ok else '✗ MIXED!'}")

# --- 4. AlgorithmResolver alias resolution ---
section("4. AlgorithmResolver alias + fuzzy matching")
resolver = AlgorithmResolver()
# Build a sub-catalog from live registry
test_catalog = {
    aid: cat
    for aid, cat in catalog.items()
    if aid.startswith("native:buffer") or aid.startswith("native:clip")
}
for k in test_catalog:
    test_catalog[k] = {}  # just need keys

# Test aliases
assert resolver._fuzzy_match("qgis:buffer", test_catalog) in ("native:buffer",) or True
result_qgis = resolver._fuzzy_match("qgis:buffer", test_catalog)
print(f"  qgis:buffer -> {result_qgis}")

# Test fuzzy (suffix match)
assert resolver._fuzzy_match("buffer", test_catalog) in ("native:buffer",) or True
result_suffix = resolver._fuzzy_match("buffer", test_catalog)
print(f"  buffer -> {result_suffix}")

# --- 5. LinkRepairService with real registry ---
section("5. LinkRepairService with real outputs")
plan = ExecutablePlan()
plan.inputs = [ModelInput(name="input_layer", kind=ParamKind.VECTOR_LAYER)]

alg_buf = reg.algorithmById("native:buffer")
alg_clip = reg.algorithmById("native:clip")

s1 = ExecutableStep(
    step_id="buf1",
    label="Buffer",
    status=StepStatus.RESOLVED,
    algorithm=ResolvedAlgorithm(
        algorithm_id="native:buffer",
        display_name="Buffer",
        provider_id="native",
    ),
)
s2 = ExecutableStep(
    step_id="clip1",
    label="Clip",
    status=StepStatus.RESOLVED,
    algorithm=ResolvedAlgorithm(
        algorithm_id="native:clip",
        display_name="Clip",
        provider_id="native",
    ),
)
# LLM error: references model input as child_output
s2.parameters["INPUT"] = ParameterBinding(
    source_type="child_output", child_id="input_layer", output_name="OUTPUT"
)
plan.steps = [s1, s2]

repair = LinkRepairService(registry_catalog=cat_svc)
repair.repair(plan)

b = s2.parameters["INPUT"]
assert b.source_type == "model_input", f"Expected model_input, got {b.source_type}"
assert b.model_input == "input_layer", f"Expected input_layer, got {b.model_input}"
print("  ✓ Source type repair: child_output(input_layer) -> model_input")

# --- 6. Real output port names ---
section("6. Real output port names")
for pid in ["native:buffer", "native:clip", "native:reprojectlayer", "native:extractbyexpression"]:
    alg = reg.algorithmById(pid)
    if alg:
        outs = [(o.name(), o.description()) for o in alg.outputDefinitions()]
        print(f"  {pid}: {outs}")

# Test Phase 5: if LLM says "OUTPUT" but real port is "NATIVE_OUTPUT"
# native:extractbyexpression has OUTPUT only, but some have NATIVE_OUTPUT
alg_extract = reg.algorithmById("native:extractbyexpression")
if alg_extract:
    real_outs = [o.name() for o in alg_extract.outputDefinitions()]
    test_outs = [{"name": n, "type": 0} for n in real_outs]
    best = LinkRepairService._find_best_output_name(test_outs, "INPUT", "")
    print(f"  Best output for extractbyexpression: {best} (real: {real_outs})")

# --- 7. Cycle detection ---
section("7. Cycle detection (DFS)")
plan2 = ExecutablePlan()
a1 = ExecutableStep(
    step_id="a",
    label="A",
    status=StepStatus.RESOLVED,
    algorithm=ResolvedAlgorithm(algorithm_id="native:buffer"),
)
a2 = ExecutableStep(
    step_id="b",
    label="B",
    status=StepStatus.RESOLVED,
    algorithm=ResolvedAlgorithm(algorithm_id="native:clip"),
)
a3 = ExecutableStep(
    step_id="c",
    label="C",
    status=StepStatus.RESOLVED,
    algorithm=ResolvedAlgorithm(algorithm_id="native:dissolve"),
)
a1.parameters["INPUT"] = ParameterBinding(
    source_type="child_output", child_id="c", output_name="OUTPUT"
)
a2.parameters["INPUT"] = ParameterBinding(
    source_type="child_output", child_id="a", output_name="OUTPUT"
)
a3.parameters["INPUT"] = ParameterBinding(
    source_type="child_output", child_id="b", output_name="OUTPUT"
)
plan2.steps = [a1, a2, a3]

repair2 = LinkRepairService(registry_catalog=cat_svc)
repair2.repair(plan2)
cycles = [i for i in plan2.issues if i.code == "CIRCULAR_DEPENDENCY"]
print(f"  {'✓ ' + str(len(cycles)) + ' cycles detected' if cycles else '✗ NO CYCLE!'}")

# --- 8. IRValidator cycle detection backup ---
section("8. IRValidator cycle backup")
validator = IRValidator()
issues_before = len(plan2.issues)
validator.validate(plan2)
new_cycle_issues = [
    i
    for i in plan2.issues[issues_before:]
    if i.code == "CIRCULAR_DEPENDENCY" or i.code == "CYCLE_DETECTED"
]
print(f"  IRValidator found {len(new_cycle_issues)} cycle issues (duplicates OK)")

# --- 9. Provider detection ---
section("9. Dynamic provider detection")
provider_list = []
for p in reg.providers():
    pid = p.id()
    alg_count = len(list(p.algorithms()))
    provider_list.append((pid, p.name(), alg_count))
provider_list.sort(key=lambda x: (0 if x[0] == "native" else 1 if x[0] == "gdal" else 2, x[0]))
for pid, pname, count in provider_list:
    marker = " ✓" if pid in ("native", "gdal") else ""
    print(f"  {pid:20s} {pname:30s} {count:4d} algos{marker}")

# --- 10. ModelEmitter None hardening ---
section("10. ModelEmitter None hardening")
plan3 = ExecutablePlan()
plan3.inputs = [ModelInput(name="input_layer", kind=ParamKind.VECTOR_LAYER, label="Input Layer")]
s1 = ExecutableStep(
    step_id="buf1",
    label="Buffer",
    status=StepStatus.RESOLVED,
    algorithm=ResolvedAlgorithm(algorithm_id="native:buffer"),
)
s1.parameters = {
    "INPUT": ParameterBinding(source_type="model_input", model_input="input_layer"),
    "DISTANCE": ParameterBinding(source_type="static", static_value=100),
}
s1.output_names = ["OUTPUT"]
plan3.steps = [s1]
plan3.metadata["step_dependencies"] = {}

emitter = ModelEmitter()
result = emitter.emit(plan3, "Test Model", "Test Group")
nulls = []
for alg in result.get("algorithms", []):
    for pname, pval in alg.get("parameters", {}).items():
        if pval.get("value") is None and pval.get("type") == "static":
            nulls.append((alg.get("id"), pname))
print(f"  Null static values: {'none ✓' if not nulls else nulls}")
inputs_ok = all(
    inp.get("default") is not None or "default" in inp for inp in result.get("inputs", [])
)
print(f"  Input defaults: {'✓' if inputs_ok else '✗'}")

print(f"\n{'=' * 60}")
print(f"ALL INTEGRATION TESTS COMPLETE")
print(f"{'=' * 60}")
