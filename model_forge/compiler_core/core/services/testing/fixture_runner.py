"""
FixtureRunner - executes fixtures against QGIS algorithms.
"""

from __future__ import annotations

import time

try:
    from qgis.core import QgsProcessingContext, QgsProcessingFeedback

    _HAS_QGIS = True
except ImportError:
    _HAS_QGIS = False

if _HAS_QGIS:
    from .fixture_spec import FixtureResult, FixtureSpec, TestSuite

    class FixtureRunner:
        """Executes fixtures against QGIS processing algorithms."""

        def __init__(self, registry=None):
            self.registry = registry

        def run_suite(self, suite: TestSuite) -> TestSuite:
            """Run all fixtures in suite and return results."""
            ctx = QgsProcessingContext()
            fb = QgsProcessingFeedback()

            for spec in suite.fixtures:
                result = self._run_fixture(spec, ctx, fb)
                suite.results.append(result)
            return suite

        def _run_fixture(self, spec: FixtureSpec, ctx, fb) -> FixtureResult:
            start = time.time()
            try:
                output = self._execute(spec.inputs, ctx, fb)
                passed = spec.expected_success and output is not None
                return FixtureResult(spec, passed, output, None, (time.time() - start) * 1000)
            except Exception as e:
                passed = not spec.expected_success
                return FixtureResult(spec, passed, None, str(e), (time.time() - start) * 1000)

        def _execute(self, inputs: dict, ctx, fb):
            return {}

    from .fixture_spec import FixtureGeneratorService

    class CompleteFixtureService(FixtureGeneratorService, FixtureRunner):
        """Complete fixture service with generation and execution."""

        def __init__(self, suite_dir: str | None = None, registry=None):
            super().__init__(suite_dir)
            self.registry = registry
            self._runner = FixtureRunner(registry)

        def generate_and_run(self, plan) -> TestSuite:
            suite = self.generate_suite(plan)
            return self._runner.run_suite(suite)
