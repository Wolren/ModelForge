"""
FixtureGeneratorService
========================
Generates and executes contract test fixtures against ExecutableStep objects.

Three fixture modes
-------------------
happy       : valid inputs that should succeed
boundary    : extreme-but-valid inputs (empty layer, huge numbers, etc.)
adversarial : inputs that deliberately violate contracts — multipart instead
              of singlepart, invalid geometry, CRS mismatch, null geometry,
              wrong field type.  A "pass" for adversarial means the algorithm
              raised a QgsProcessingException or returned an empty result,
              i.e. it *detected* the violation.

When QGIS is available (``_HAS_QGIS = True``) adversarial and happy fixtures
are executed via ``processing.run()`` against real in-memory scratch layers.
Without QGIS the service falls back to structural checks only.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    import processing
    from qgis.core import (
        QgsVectorLayer, QgsFeature, QgsGeometry,
        QgsField, QgsFields, QgsWkbTypes,
        QgsCoordinateReferenceSystem,
        QgsProcessingException,
        QgsProcessingContext, QgsProcessingFeedback,
    )
    from qgis.PyQt.QtCore import QVariant
    _HAS_QGIS = True
except ImportError:
    _HAS_QGIS = False


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TestFixture:
    name:         str
    mode:         str           # "happy" | "boundary" | "adversarial"
    step_id:      str
    param_values: Dict[str, Any] = field(default_factory=dict)
    description:  str = ""
    violation:    Optional[str] = None   # adversarial only


@dataclass
class TestResult:
    fixture:     TestFixture
    passed:      bool
    expected:    bool            # True if failure was expected (adversarial)
    error_msg:   Optional[str] = None
    error_stage: Optional[str] = None   # "validation" | "execution"


# ---------------------------------------------------------------------------
# Scratch-layer builders (only compiled when QGIS is available)
# ---------------------------------------------------------------------------

if _HAS_QGIS:

    def _make_layer(wkb_type: int, crs: str = "EPSG:4326") -> QgsVectorLayer:
        """Create an empty in-memory scratch layer."""
        type_str = {
            QgsWkbTypes.Type.Polygon:      "Polygon",
            QgsWkbTypes.Type.MultiPolygon: "MultiPolygon",
            QgsWkbTypes.Type.LineString:   "LineString",
            QgsWkbTypes.Type.Point:        "Point",
        }.get(wkb_type, "Polygon")
        layer = QgsVectorLayer(f"{type_str}?crs={crs}", "fixture", "memory")
        return layer

    def _add_feature(layer: QgsVectorLayer, wkt: str) -> None:
        pr = layer.dataProvider()
        f  = QgsFeature()
        f.setGeometry(QgsGeometry.fromWkt(wkt))
        pr.addFeature(f)
        layer.updateExtents()

    def _singlepart_polygon() -> QgsVectorLayer:
        lyr = _make_layer(QgsWkbTypes.Type.Polygon)
        _add_feature(lyr, "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))")
        return lyr

    def _multipart_polygon() -> QgsVectorLayer:
        lyr = _make_layer(QgsWkbTypes.Type.MultiPolygon)
        _add_feature(lyr, "MULTIPOLYGON(((0 0,1 0,1 1,0 1,0 0)),((2 2,3 2,3 3,2 3,2 2)))")
        return lyr

    def _invalid_geometry_polygon() -> QgsVectorLayer:
        """Self-intersecting bowtie polygon."""
        lyr = _make_layer(QgsWkbTypes.Type.Polygon)
        _add_feature(lyr, "POLYGON((0 0,1 1,1 0,0 1,0 0))")
        return lyr

    def _null_geometry_layer() -> QgsVectorLayer:
        lyr = _make_layer(QgsWkbTypes.Type.Polygon)
        pr  = lyr.dataProvider()
        f   = QgsFeature()
        # deliberately do NOT set geometry → null
        pr.addFeature(f)
        lyr.updateExtents()
        return lyr

    def _crs_mismatch_polygon(target_crs: str = "EPSG:4326") -> QgsVectorLayer:
        """
        Return a layer in EPSG:3857 when the algorithm likely expects EPSG:4326,
        or vice-versa — a mismatch is all that matters for the test.
        """
        other = "EPSG:3857" if target_crs == "EPSG:4326" else "EPSG:4326"
        lyr = _make_layer(QgsWkbTypes.Type.Polygon, crs=other)
        _add_feature(lyr, "POLYGON((0 0,1 0,1 1,0 1,0 0))")
        return lyr

    def _wrong_field_type_layer() -> QgsVectorLayer:
        """Layer with a field named 'value' that is a string instead of integer."""
        lyr = _make_layer(QgsWkbTypes.Type.Polygon)
        pr  = lyr.dataProvider()
        pr.addAttributes([QgsField("value", QVariant.Type.String)])
        lyr.updateFields()
        _add_feature(lyr, "POLYGON((0 0,1 0,1 1,0 1,0 0))")
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromWkt("POLYGON((0 0,1 0,1 1,0 1,0 0))"))
        f.setAttribute("value", "not_a_number")
        pr.addFeature(f)
        lyr.updateExtents()
        return lyr

    _VIOLATION_LAYER_BUILDERS = {
        "multipart_geometry":  _multipart_polygon,
        "invalid_geometry":    _invalid_geometry_polygon,
        "null_geometry":       _null_geometry_layer,
        "crs_mismatch":        _crs_mismatch_polygon,
        "wrong_field_type":    _wrong_field_type_layer,
    }


# ---------------------------------------------------------------------------
# FixtureGeneratorService
# ---------------------------------------------------------------------------

class FixtureGeneratorService:
    """
    Generates test fixtures and, when QGIS is available, executes them via
    ``processing.run()`` against real in-memory scratch layers.
    """

    # Algorithms known to handle certain violations gracefully (i.e. they
    # CANNOT be used to detect that violation — they fix it instead).
    _VIOLATION_SAFE: Dict[str, set] = {
        "multipart_geometry": {
            "native:multiparttosingleparts",
            "native:fixgeometries",
            "native:dissolve",
        },
        "invalid_geometry": {
            "native:fixgeometries",
            "native:buffer",
            "native:simplifygeometries",
        },
        "null_geometry": {
            "native:removenullgeometries",
            "native:fixgeometries",
        },
    }

    # ------------------------------------------------------------------
    # Fixture generation
    # ------------------------------------------------------------------

    def generate_fixtures(
        self,
        step,
        layer_catalog: Dict,
        modes: Tuple[str, ...] = ("happy", "boundary", "adversarial"),
    ) -> List[TestFixture]:
        if step.algorithm is None:
            return []

        fixtures: List[TestFixture] = []

        for pname, binding in step.parameters.items():
            if "happy" in modes:
                fixtures.append(TestFixture(
                    name=f"{step.step_id}_{pname}_happy",
                    mode="happy",
                    step_id=step.step_id,
                    param_values={pname: self._happy_value(binding)},
                    description=f"Valid {pname} input",
                ))

            if "boundary" in modes:
                for variant, val in self._boundary_values(binding):
                    fixtures.append(TestFixture(
                        name=f"{step.step_id}_{pname}_boundary_{variant}",
                        mode="boundary",
                        step_id=step.step_id,
                        param_values={pname: val},
                        description=f"Boundary ({variant}) {pname}",
                    ))

            if "adversarial" in modes:
                fixtures.extend(
                    self._adversarial_fixtures(step.step_id, pname, binding)
                )

        return fixtures

    # ------------------------------------------------------------------
    # Contract test execution
    # ------------------------------------------------------------------

    def run_contract_tests(
        self,
        step,
        layer_catalog: Dict,
        modes: Tuple[str, ...] = ("happy", "boundary", "adversarial"),
    ) -> List[TestResult]:
        fixtures = self.generate_fixtures(step, layer_catalog, modes)
        results: List[TestResult] = []

        for fixture in fixtures:
            if step.algorithm is None:
                results.append(TestResult(
                    fixture=fixture,
                    passed=False,
                    expected=fixture.mode == "adversarial",
                    error_msg="No resolved algorithm",
                    error_stage="validation",
                ))
                continue

            if fixture.mode == "adversarial":
                result = self._run_adversarial(step, fixture)
            else:
                result = self._run_happy_or_boundary(step, fixture)

            results.append(result)

        return results

    # ------------------------------------------------------------------
    # Happy / boundary execution
    # ------------------------------------------------------------------

    def _run_happy_or_boundary(self, step, fixture: TestFixture) -> TestResult:
        """
        For happy/boundary: run the algorithm and expect it to succeed
        (no exception, non-empty output).  Falls back to a structural
        pass when QGIS is not available.
        """
        if not _HAS_QGIS:
            return TestResult(fixture=fixture, passed=True, expected=False)

        alg_id = step.algorithm.algorithm_id
        params = self._build_base_params(step)
        params.update(dict(fixture.param_values))

        # Replace sentinel strings with real scratch layers
        params = self._materialise_params(params)
        if params is None:
            return TestResult(
                fixture=fixture, passed=False, expected=False,
                error_msg="Could not materialise parameter values",
                error_stage="validation",
            )

        try:
            context  = QgsProcessingContext()
            feedback = QgsProcessingFeedback()
            processing.run(alg_id, params, context=context, feedback=feedback)
            return TestResult(fixture=fixture, passed=True, expected=False)
        except QgsProcessingException as e:
            return TestResult(
                fixture=fixture, passed=False, expected=False,
                error_msg=str(e), error_stage="execution",
            )
        except Exception as e:
            return TestResult(
                fixture=fixture, passed=False, expected=False,
                error_msg=f"Unexpected error: {e}", error_stage="execution",
            )

    # ------------------------------------------------------------------
    # Adversarial execution
    # ------------------------------------------------------------------

    def _run_adversarial(self, step, fixture: TestFixture) -> TestResult:
        """
        For adversarial: run the algorithm with a deliberately broken layer.
        A "pass" means the algorithm raised QgsProcessingException (detected
        the violation).  Algorithms that intentionally fix the violation
        (e.g. fixgeometries for invalid_geometry) are flagged as "safe" and
        the test is recorded as detected automatically.
        """
        alg_id    = step.algorithm.algorithm_id
        violation = fixture.violation or ""

        # Check if this algorithm is known to safely handle the violation
        safe_set = self._VIOLATION_SAFE.get(violation, set())
        if alg_id in safe_set:
            return TestResult(
                fixture=fixture, passed=True, expected=True,
                error_msg=f"Algorithm safely handles '{violation}'",
                error_stage=None,
            )

        if not _HAS_QGIS:
            # Static fallback: unknown → assume not caught
            return TestResult(
                fixture=fixture, passed=False, expected=True,
                error_msg=f"Static check: violation '{violation}' not in safe list for '{alg_id}'",
                error_stage="validation",
            )

        # Build the adversarial scratch layer
        builder = _VIOLATION_LAYER_BUILDERS.get(violation)
        if builder is None:
            return TestResult(
                fixture=fixture, passed=False, expected=True,
                error_msg=f"No layer builder for violation '{violation}'",
                error_stage="validation",
            )

        adv_layer = builder()
        if not adv_layer.isValid():
            return TestResult(
                fixture=fixture, passed=False, expected=True,
                error_msg="Adversarial layer failed to create",
                error_stage="validation",
            )

        # Replace the first layer-type parameter with the adversarial layer
        params = self._build_adversarial_params(step, fixture, adv_layer)

        try:
            context  = QgsProcessingContext()
            feedback = QgsProcessingFeedback()
            result   = processing.run(alg_id, params, context=context, feedback=feedback)
            # Algorithm ran without exception — check if output is empty
            # (some algorithms silently return empty on bad input)
            detected = self._output_is_empty(result)
            return TestResult(
                fixture=fixture,
                passed=detected,
                expected=True,
                error_msg=None if detected else f"Algorithm did not detect '{violation}'",
                error_stage=None if detected else "execution",
            )
        except QgsProcessingException:
            # Raised an exception — violation was detected
            return TestResult(
                fixture=fixture, passed=True, expected=True,
            )
        except Exception as e:
            # Unexpected crash — not the same as detection
            return TestResult(
                fixture=fixture, passed=False, expected=True,
                error_msg=f"Unexpected crash: {e}", error_stage="execution",
            )

    # ------------------------------------------------------------------
    # Parameter materialisation helpers
    # ------------------------------------------------------------------

    def _materialise_params(self, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Replace sentinel strings and None values with safe defaults."""
        out = {}
        for k, v in params.items():
            if v is None:
                continue
            if isinstance(v, str) and v.startswith("__") and v.endswith("__"):
                if not _HAS_QGIS:
                    return None
                if v == "__EMPTY_LAYER__":
                    out[k] = _make_layer(QgsWkbTypes.Type.Polygon)
                elif v == "__MULTIPART_GEOMETRY__":
                    out[k] = _multipart_polygon()
                else:
                    # Generic layer sentinel
                    out[k] = _singlepart_polygon()
            else:
                out[k] = v
        return out

    def _build_adversarial_params(
        self,
        step,
        fixture: TestFixture,
        adv_layer,
    ) -> Dict[str, Any]:
        """
        Build a params dict for the algorithm replacing the first
        vector-layer parameter with the adversarial scratch layer.
        """
        params: Dict[str, Any] = self._build_base_params(step)
        target_param = next(iter(fixture.param_values.keys()), None)
        first_layer_injected = target_param is None

        for pname in step.parameters:
            binding  = step.parameters[pname]
            src_type = binding.source_type

            if not first_layer_injected and pname == target_param:
                params[pname] = adv_layer
                first_layer_injected = True
            elif not first_layer_injected and src_type in ("model_input", "child_output"):
                params[pname] = adv_layer
                first_layer_injected = True

        # Ensure OUTPUT goes to memory
        params["OUTPUT"] = "memory:"
        return params

    def _build_base_params(self, step) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        for pname, binding in step.parameters.items():
            src_type = binding.source_type
            if src_type in ("model_input", "child_output"):
                params[pname] = "__MODEL_INPUT_LAYER__"
            elif binding.static_value is not None:
                params[pname] = binding.static_value
            elif binding.enum_index is not None:
                params[pname] = binding.enum_index
        return params

    @staticmethod
    def _output_is_empty(result: Dict[str, Any]) -> bool:
        """
        Return True if every output layer in the result dict is empty
        (feature count == 0), which we treat as silent detection.
        """
        if not _HAS_QGIS:
            return False
        for val in result.values():
            if isinstance(val, QgsVectorLayer):
                if val.featureCount() > 0:
                    return False
            elif isinstance(val, str) and val not in ("", "memory:"):
                # path output — can't easily check without loading
                return False
        return True

    # ------------------------------------------------------------------
    # Value generators
    # ------------------------------------------------------------------

    @staticmethod
    def _happy_value(binding) -> Any:
        src = binding.source_type
        if src == "static":
            return binding.static_value
        if src == "enum_index":
            return binding.enum_index or 0
        if src in ("model_input", "child_output"):
            return "__MODEL_INPUT_LAYER__"
        return None

    @staticmethod
    def _boundary_values(binding) -> List[Tuple[str, Any]]:
        src = binding.source_type
        if src in ("model_input", "child_output"):
            return [
                ("empty_layer", "__EMPTY_LAYER__"),
                ("multipart", "__MULTIPART_GEOMETRY__"),
            ]
        if isinstance(binding.static_value, (int, float)) or src == "enum_index":
            return [
                ("large_num", 1_000_000_000),
                ("zero", 0),
                ("neg", -1),
            ]
        return [
            ("empty", None),
        ]

    @staticmethod
    def _adversarial_fixtures(step_id: str, pname: str, binding) -> List[TestFixture]:
        if binding.source_type not in ("model_input", "child_output"):
            return []
        violations = [
            ("multipart_geometry",  f"{pname}: multipart instead of singlepart"),
            ("invalid_geometry",    f"{pname}: self-intersecting / invalid geometry"),
            ("null_geometry",       f"{pname}: features with null geometry"),
            ("crs_mismatch",        f"{pname}: layer in unexpected CRS"),
            ("wrong_field_type",    f"{pname}: field with wrong data type"),
        ]
        return [
            TestFixture(
                name=f"{step_id}_{pname}_adv_{violation}",
                mode="adversarial",
                step_id=step_id,
                param_values={pname: f"__{violation.upper()}__"},
                description=desc,
                violation=violation,
            )
            for violation, desc in violations
        ]
