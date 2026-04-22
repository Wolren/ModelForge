"""
ContractTestAlgorithm
=====================
Runs FixtureGeneratorService against a plan and reports test results
via the Processing feedback channel.
"""
from __future__ import annotations

try:
    from qgis.core import (
        QgsProcessingAlgorithm,
        QgsProcessingParameterString,
        QgsProcessingParameterEnum,
        QgsProcessingOutputString,
        QgsProcessingException,
    )
    from qgis.PyQt.QtCore import QCoreApplication
    _HAS_QGIS = True
except ImportError:
    _HAS_QGIS = False

if _HAS_QGIS:
    import json

    class ContractTestAlgorithm(QgsProcessingAlgorithm):

        def tr(self, message: str) -> str:
            return QCoreApplication.translate("ContractTestAlgorithm", message)

        INPUT_JSON  = "INPUT_JSON"
        MODES       = "MODES"
        OUTPUT_REPORT = "OUTPUT_REPORT"

        def name(self)        -> str: return "mcp_contract_test"
        def displayName(self) -> str: return self.tr("Run Contract Tests (MCP)")
        def group(self)       -> str: return self.tr("ModelForge")
        def groupId(self)     -> str: return "model_forge"

        def createInstance(self): return ContractTestAlgorithm()

        def initAlgorithm(self, config=None):
            self.addParameter(QgsProcessingParameterString(
                self.INPUT_JSON, self.tr("Compiled model JSON"), multiLine=True))
            self.addParameter(QgsProcessingParameterEnum(
                self.MODES, self.tr("Test modes to run"),
                options=["happy only", "adversarial only", "all"],
                defaultValue=2, allowMultiple=False,
            ))
            self.addOutput(QgsProcessingOutputString(
                self.OUTPUT_REPORT, self.tr("Test report JSON")))

        def processAlgorithm(self, parameters, context, feedback):
            raw_json  = self.parameterAsString(parameters, self.INPUT_JSON, context)
            modes_idx = self.parameterAsEnum  (parameters, self.MODES,      context)
            modes_map = {
                0: ("happy",),
                1: ("adversarial",),
                2: ("happy", "boundary", "adversarial"),
            }
            modes = modes_map.get(modes_idx, ("happy", "boundary", "adversarial"))

            try:
                model_json = json.loads(raw_json)
            except json.JSONDecodeError as e:
                raise QgsProcessingException(f"Invalid JSON: {e}") from e

            from ...core.services.fixture_generator import FixtureGeneratorService
            from ...core.ir import (
                ExecutableStep, StepStatus, ResolvedAlgorithm, ParameterBinding
            )

            svc = FixtureGeneratorService()
            report = {"steps": [], "summary": {"total": 0, "passed": 0, "failed": 0}}

            for alg_dict in model_json.get("algorithms", []):
                step = ExecutableStep(
                    step_id=alg_dict.get("id", "unknown"),
                    label=alg_dict.get("description", ""),
                    status=StepStatus.RESOLVED,
                )
                alg_id = alg_dict.get("algorithm_id", "")
                if alg_id:
                    step.algorithm = ResolvedAlgorithm(
                        algorithm_id=alg_id,
                        display_name=alg_dict.get("description", alg_id),
                        provider_id=alg_id.split(":", 1)[0] if ":" in alg_id else "model_forge",
                    )

                for pname, pval in (alg_dict.get("parameters", {}) or {}).items():
                    if isinstance(pval, dict):
                        ptype = pval.get("type", "static")
                        if ptype == "model_input":
                            step.parameters[pname] = ParameterBinding(
                                source_type="model_input",
                                model_input=pval.get("input_name"),
                            )
                        elif ptype == "child_output":
                            step.parameters[pname] = ParameterBinding(
                                source_type="child_output",
                                child_id=pval.get("child_id"),
                                output_name=pval.get("output_name"),
                            )
                        elif ptype == "enum_index":
                            step.parameters[pname] = ParameterBinding(
                                source_type="enum_index",
                                enum_index=pval.get("value"),
                            )
                        else:
                            step.parameters[pname] = ParameterBinding(
                                source_type="static",
                                static_value=pval.get("value"),
                            )
                    else:
                        step.parameters[pname] = ParameterBinding(
                            source_type="static",
                            static_value=pval,
                        )

                results = svc.run_contract_tests(step, {}, modes=modes)
                step_report = {"step_id": step.step_id, "tests": []}

                for r in results:
                    report["summary"]["total"] += 1
                    status_label = "PASS" if r.passed else "FAIL"
                    if r.fixture.mode == "adversarial":
                        status_label = "DETECTED" if r.passed else "UNDETECTED"
                    if r.passed:
                        report["summary"]["passed"] += 1
                    else:
                        report["summary"]["failed"] += 1
                        if r.fixture.mode != "adversarial":
                            feedback.reportError(
                                f"FAIL [{step.step_id}] {r.fixture.name}: {r.error_msg}",
                                fatalError=False,
                            )
                    step_report["tests"].append({
                        "name":      r.fixture.name,
                        "mode":      r.fixture.mode,
                        "status":    status_label,
                        "violation": r.fixture.violation,
                        "error_msg": r.error_msg,
                    })

                report["steps"].append(step_report)
                feedback.pushInfo(f"Step {step.step_id}: {len(results)} tests run.")

            s = report["summary"]
            feedback.pushInfo(
                f"Contract tests complete: {s['passed']}/{s['total']} passed, "
                f"{s['failed']} failed."
            )
            return {self.OUTPUT_REPORT: json.dumps(report, indent=2)}

else:
    class ContractTestAlgorithm:
        pass
