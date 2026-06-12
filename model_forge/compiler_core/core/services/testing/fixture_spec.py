"""
FixtureGeneratorService - generates contract test fixtures.

Three fixture modes:
- happy: valid inputs that should succeed
- boundary: extreme-but-valid inputs
- adversarial: inputs that deliberately violate contracts
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

try:
    import processing
    from qgis.core import (
        QgsCoordinateReferenceSystem,
        QgsFeature,
        QgsField,
        QgsFields,
        QgsGeometry,
        QgsProcessingContext,
        QgsProcessingException,
        QgsProcessingFeedback,
        QgsVectorLayer,
        QgsWkbTypes,
    )

    _HAS_QGIS = True
except ImportError:
    _HAS_QGIS = False


@dataclass
class FixtureSpec:
    mode: str
    inputs: dict[str, Any]
    expected_success: bool = True
    description: str = ""


@dataclass
class FixtureResult:
    spec: FixtureSpec
    passed: bool
    output: dict[str, Any] | None = None
    error: str | None = None
    execution_time_ms: float = 0.0


@dataclass
class TestSuite:
    name: str
    steps: list[str] = field(default_factory=list)
    fixtures: list[FixtureSpec] = field(default_factory=list)
    results: list[FixtureResult] = field(default_factory=list)


class FixtureGeneratorService:
    def __init__(self, suite_dir: str | None = None):
        self.suite_dir = suite_dir

    def generate_suite(self, plan) -> TestSuite:
        suite = TestSuite(name=f"test_{plan.model_name}")
        for step in plan.steps:
            spec = self._generate_fixtures_for_step(step)
            suite.fixtures.extend(spec)
        return suite

    def _generate_fixtures_for_step(self, step) -> list[FixtureSpec]:
        specs = []
        step_id = step.step_id

        specs.append(
            FixtureSpec(
                mode="happy",
                inputs=self._make_happy_inputs(step),
                expected_success=True,
                description=f"{step_id}: valid happy path",
            )
        )

        specs.append(
            FixtureSpec(
                mode="boundary",
                inputs=self._make_boundary_inputs(step),
                expected_success=True,
                description=f"{step_id}: boundary values",
            )
        )

        if self._step_accepts_geometry(step):
            specs.append(
                FixtureSpec(
                    mode="adversarial",
                    inputs=self._make_adversarial_inputs(step),
                    expected_success=False,
                    description=f"{step_id}: invalid geometry",
                )
            )
        return specs

    def _make_happy_inputs(self, step) -> dict[str, Any]:
        return {}

    def _make_boundary_inputs(self, step) -> dict[str, Any]:
        return {}

    def _make_adversarial_inputs(self, step) -> dict[str, Any]:
        return {}

    def _step_accepts_geometry(self, step) -> bool:
        for p in step.parameters.values():
            if "vector" in str(p.source_type).lower():
                return True
        return False

    def save_suite(self, suite: TestSuite, path: str):
        import json

        data = {
            "name": suite.name,
            "fixtures": [
                {"mode": f.mode, "inputs": f.inputs, "expected_success": f.expected_success}
                for f in suite.fixtures
            ],
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
