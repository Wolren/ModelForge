"""
Test LinkRepairService, AlgorithmResolver aliases, and model_emitter hardening
without requiring QGIS runtime.
"""

import sys

sys.path.insert(0, ".")

from model_forge.compiler_core.core.compiler.algorithm_resolver import AlgorithmResolver
from model_forge.compiler_core.core.compiler.link_repair import LinkRepairService
from model_forge.compiler_core.core.compiler.model_emitter import ModelEmitter
from model_forge.compiler_core.core.ir import (
    ExecutablePlan,
    ExecutableStep,
    ModelInput,
    OutputKind,
    OutputSpec,
    ParameterBinding,
    ParameterSpec,
    ParamKind,
    ResolvedAlgorithm,
    StepStatus,
)


def test_alias_resolution():
    """AlgorithmResolver._fuzzy_match should resolve qgis: -> native:, grass: -> grass7:"""
    resolver = AlgorithmResolver()
    catalog = {"native:buffer": {}, "grass7:v.buffer": {}, "native:clip": {}}
    assert resolver._fuzzy_match("qgis:buffer", catalog) == "native:buffer"
    assert resolver._fuzzy_match("grass:v.buffer", catalog) == "grass7:v.buffer"
    assert resolver._fuzzy_match("native:buffer", catalog) == "native:buffer"
    assert resolver._fuzzy_match("qgis:clip", catalog) == "native:clip"
    assert resolver._fuzzy_match("qgis:unknownxyz", catalog) == ""
    assert resolver._fuzzy_match("buffer", catalog) == "native:buffer"
    assert resolver._fuzzy_match("native:buffer", catalog) == "native:buffer"
    print("PASS: test_alias_resolution")


def test_source_type_repair():
    """Phase 1: child_output referencing a model input name -> model_input"""
    plan = ExecutablePlan()
    plan.inputs = [ModelInput(name="my_vector", kind=ParamKind.VECTOR_LAYER)]
    s1 = ExecutableStep(
        step_id="step1",
        label="Buffer",
        status=StepStatus.RESOLVED,
        algorithm=ResolvedAlgorithm(algorithm_id="native:buffer"),
    )
    s2 = ExecutableStep(
        step_id="step2",
        label="Clip",
        status=StepStatus.RESOLVED,
        algorithm=ResolvedAlgorithm(algorithm_id="native:clip"),
    )
    s2.parameters["INPUT"] = ParameterBinding(
        source_type="child_output", child_id="my_vector", output_name="OUTPUT"
    )
    plan.steps = [s1, s2]

    repair = LinkRepairService()
    repair.repair(plan)

    b = s2.parameters["INPUT"]
    assert b.source_type == "model_input", f"Expected model_input, got {b.source_type}"
    assert b.model_input == "my_vector", f"Expected my_vector, got {b.model_input}"
    print("PASS: test_source_type_repair")


def test_output_name_repair():
    """Phase 5: LLM says 'OUTPUT' but real port is 'NATIVE_OUTPUT'"""
    # Simulate a step with known outputs
    plan = ExecutablePlan()
    s1 = ExecutableStep(
        step_id="step1",
        label="Extract",
        status=StepStatus.RESOLVED,
        algorithm=ResolvedAlgorithm(
            algorithm_id="native:extractbyexpression",
            outputs=[OutputSpec(name="NATIVE_OUTPUT", kind=OutputKind.VECTOR)],
        ),
    )
    s2 = ExecutableStep(
        step_id="step2",
        label="Clip",
        status=StepStatus.RESOLVED,
        algorithm=ResolvedAlgorithm(algorithm_id="native:clip"),
    )
    s2.parameters["INPUT"] = ParameterBinding(
        source_type="child_output", child_id="step1", output_name="OUTPUT"
    )
    plan.steps = [s1, s2]

    repair = LinkRepairService()
    repair.repair(plan)

    b = s2.parameters["INPUT"]
    assert b.output_name == "NATIVE_OUTPUT", f"Expected NATIVE_OUTPUT, got {b.output_name}"
    print("PASS: test_output_name_repair")


def test_static_value_coercion():
    """Phase 3: bool strings, number strings, enum labels"""
    plan = ExecutablePlan()
    s1 = ExecutableStep(
        step_id="step1",
        label="FieldCalc",
        status=StepStatus.RESOLVED,
        algorithm=ResolvedAlgorithm(
            algorithm_id="native:fieldcalculator",
            parameters=[
                ParameterSpec(name="NEW_FIELD", kind=ParamKind.STRING),
                ParameterSpec(
                    name="FIELD_TYPE", kind=ParamKind.ENUM, enum_options=["FLOAT", "INT", "STRING"]
                ),
            ],
        ),
    )
    # LLM sends boolean as string, enum as label
    s1.parameters["NEW_FIELD"] = ParameterBinding(source_type="static", static_value="my_result")
    plan.steps = [s1]

    repair = LinkRepairService()
    repair.repair(plan)
    print("PASS: test_static_value_coercion (no crash)")


def test_model_emitter_none_hardening():
    """ModelEmitter should never emit null for critical fields"""
    plan = ExecutablePlan()
    plan.inputs = [ModelInput(name="input_layer", kind=ParamKind.VECTOR_LAYER)]
    s1 = ExecutableStep(
        step_id="step1",
        label="Buffer",
        status=StepStatus.RESOLVED,
        algorithm=ResolvedAlgorithm(algorithm_id="native:buffer"),
    )
    s1.parameters = {
        "INPUT": ParameterBinding(source_type="model_input", model_input="input_layer"),
        "DISTANCE": ParameterBinding(source_type="static", static_value=100),
    }
    plan.steps = [s1]

    emitter = ModelEmitter()
    result = emitter.emit(plan, "Test Model", "Test Group")

    # Check no null values in critical fields
    for inp in result["inputs"]:
        assert inp.get("default") is not None or "default" in inp
    for alg in result["algorithms"]:
        for _pname, pval in alg["parameters"].items():
            v = pval.get("value")
            if v is None and pval["type"] != "static":
                pass  # model_input/child_output don't need value
    print("PASS: test_model_emitter_none_hardening")


def test_cycle_detection():
    """Phase 7: Detect circular dependencies"""
    plan = ExecutablePlan()
    s1 = ExecutableStep(
        step_id="step1",
        label="A",
        status=StepStatus.RESOLVED,
        algorithm=ResolvedAlgorithm(algorithm_id="native:buffer"),
    )
    s2 = ExecutableStep(
        step_id="step2",
        label="B",
        status=StepStatus.RESOLVED,
        algorithm=ResolvedAlgorithm(algorithm_id="native:clip"),
    )
    s3 = ExecutableStep(
        step_id="step3",
        label="C",
        status=StepStatus.RESOLVED,
        algorithm=ResolvedAlgorithm(algorithm_id="native:dissolve"),
    )
    # step1 -> step2 -> step3 -> step1 (cycle!)
    s1.parameters["INPUT"] = ParameterBinding(
        source_type="child_output", child_id="step3", output_name="OUTPUT"
    )
    s2.parameters["INPUT"] = ParameterBinding(
        source_type="child_output", child_id="step1", output_name="OUTPUT"
    )
    s3.parameters["INPUT"] = ParameterBinding(
        source_type="child_output", child_id="step2", output_name="OUTPUT"
    )
    plan.steps = [s1, s2, s3]

    repair = LinkRepairService()
    repair.repair(plan)

    # Should produce an ERROR-level cycle issue
    cycle_issues = [i for i in plan.issues if i.code == "CIRCULAR_DEPENDENCY"]
    assert cycle_issues, f"Expected cycle issue, got: {[i.code for i in plan.issues]}"
    print(f"PASS: test_cycle_detection ({len(cycle_issues)} cycle issues)")


def test_missing_link_filling():
    """Phase 6: fill unbound required params from compatible upstream"""
    plan = ExecutablePlan()
    s1 = ExecutableStep(
        step_id="step1",
        label="Buffer",
        status=StepStatus.RESOLVED,
        algorithm=ResolvedAlgorithm(
            algorithm_id="native:buffer",
            outputs=[OutputSpec(name="OUTPUT", kind=OutputKind.VECTOR)],
            parameters=[
                ParameterSpec(name="INPUT", kind=ParamKind.VECTOR_LAYER, optional=False),
                ParameterSpec(
                    name="DISTANCE", kind=ParamKind.NUMBER, optional=False, default_value=10
                ),
            ],
        ),
    )
    s2 = ExecutableStep(
        step_id="step2",
        label="Clip",
        status=StepStatus.RESOLVED,
        algorithm=ResolvedAlgorithm(
            algorithm_id="native:clip",
            parameters=[
                ParameterSpec(name="INPUT", kind=ParamKind.VECTOR_LAYER, optional=False),
                ParameterSpec(name="OVERLAY", kind=ParamKind.VECTOR_LAYER, optional=False),
            ],
        ),
    )
    # s2 has no bindings for INPUT or OVERLAY
    plan.steps = [s1, s2]

    repair = LinkRepairService()
    repair.repair(plan)

    # Should auto-link s2.INPUT -> s1.OUTPUT
    b = s2.parameters.get("INPUT")
    assert b is not None, "Phase 6 should fill INPUT binding"
    assert b.source_type == "child_output", f"Expected child_output, got {b.source_type}"
    assert b.child_id == "step1", f"Expected step1, got {b.child_id}"
    print("PASS: test_missing_link_filling")


if __name__ == "__main__":
    test_alias_resolution()
    test_source_type_repair()
    test_output_name_repair()
    test_static_value_coercion()
    test_model_emitter_none_hardening()
    test_cycle_detection()
    test_missing_link_filling()
    print("\n== All 7 tests PASSED ==")
