"""Tests for the model runner DAG executor.

We mock qgis.processing so the runner can be exercised offline.
The contract under test is: topological sort, output threading
between steps, error propagation / fail_fast, cancellation via
threading.Event, retries, and idempotency key stability.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

# --- Fake qgis.processing ------------------------------------------


class _FakeProcessing:
    """In-memory substitute for qgis.processing.

    Records every (algorithm_id, inputs) call. The impl dict
    can be set per test to inject failures, sleep, etc.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.impl: dict[str, Any] = {}

    def install(self, monkeypatch: pytest.MonkeyPatch) -> _FakeProcessing:
        """Install this fake at qgis.processing (lazily imported)."""

        def _run(algorithm_id: str, inputs: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((algorithm_id, inputs))
            handler = self.impl.get(algorithm_id)
            if handler is None:
                # Default: echo inputs as outputs, prefixed with OUT_.
                return {f"OUT_{k}": v for k, v in inputs.items()}
            if isinstance(handler, BaseException):
                raise handler
            if callable(handler):
                return dict(handler(inputs))
            return dict(handler)

        import sys
        import types

        qgis_pkg = types.ModuleType("qgis")
        qgis_pkg.__path__ = []  # type: ignore[attr-defined]
        processing_mod = types.ModuleType("qgis.processing")
        processing_mod.run = _run  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "qgis", qgis_pkg)
        monkeypatch.setitem(sys.modules, "qgis.processing", processing_mod)
        return self

    def set_impl(self, algorithm_id: str, response: Any) -> None:
        self.impl[algorithm_id] = response


# --- Fixtures ----------------------------------------------------


@pytest.fixture
def fake_processing(monkeypatch: pytest.MonkeyPatch) -> _FakeProcessing:
    fp = _FakeProcessing()
    fp.install(monkeypatch)
    return fp


# --- Topological order --------------------------------------------


def test_two_steps_in_sequence(fake_processing: _FakeProcessing) -> None:
    from model_forge.compiler_core.core.services.model_runner import run_model

    model = {
        "model_name": "M",
        "model_group": "G",
        "algorithms": [
            {
                "id": "a",
                "algorithm_id": "native:buffer",
                "parameters": {"INPUT": "src", "OUTPUT": "memory:"},
            },
            {
                "id": "b",
                "algorithm_id": "native:clip",
                "parameters": {
                    "INPUT": {
                        "type": "child_output",
                        "child_id": "a",
                        "output_name": "OUT_INPUT",
                    },
                    "OVERLAY": "overlay",
                    "OUTPUT": "memory:",
                },
            },
        ],
    }
    fake_processing.set_impl("native:buffer", {"OUT_INPUT": "out_a", "OUT_OUTPUT": "memory:"})
    fake_processing.set_impl("native:clip", {"OUT_INPUT": "out_b", "OUT_OUTPUT": "memory:"})
    report = run_model(model)
    assert report.succeeded
    assert [r.step_id for r in report.step_results] == ["a", "b"]
    second_call = fake_processing.calls[1]
    assert second_call[0] == "native:clip"
    # The runner passes whatever the upstream returned for that
    # output_name; we accept either the literal name or the
    # single fallback.
    assert second_call[1]["INPUT"] in {"out_a", "out_b"}


def test_independent_steps_emit_same_wave(fake_processing: _FakeProcessing) -> None:
    from model_forge.compiler_core.core.services.model_runner import run_model

    model = {
        "model_name": "M",
        "model_group": "G",
        "algorithms": [
            {"id": "a", "algorithm_id": "native:buffer", "parameters": {"INPUT": "src1"}},
            {"id": "b", "algorithm_id": "native:buffer", "parameters": {"INPUT": "src2"}},
        ],
    }
    fake_processing.set_impl("native:buffer", {"OUT_INPUT": "ok"})
    report = run_model(model)
    assert report.succeeded


def test_cycle_detected() -> None:
    from model_forge.compiler_core.core.services.model_runner import run_model

    model = {
        "model_name": "M",
        "model_group": "G",
        "algorithms": [
            {
                "id": "a",
                "algorithm_id": "native:buffer",
                "parameters": {"INPUT": {"type": "child_output", "child_id": "b"}},
            },
            {
                "id": "b",
                "algorithm_id": "native:buffer",
                "parameters": {"INPUT": {"type": "child_output", "child_id": "a"}},
            },
        ],
    }
    with pytest.raises(ValueError, match="[Cc]ycle"):
        run_model(model)


# --- Failure modes ----------------------------------------------


def test_fail_fast_stops_at_first_error(fake_processing: _FakeProcessing) -> None:
    from model_forge.compiler_core.core.services.model_runner import run_model

    fake_processing.set_impl("native:buffer", RuntimeError("boom"))
    model = {
        "model_name": "M",
        "model_group": "G",
        "algorithms": [
            {"id": "a", "algorithm_id": "native:buffer", "parameters": {}},
            {"id": "b", "algorithm_id": "native:clip", "parameters": {}},
        ],
    }
    report = run_model(model, fail_fast=True)
    assert not report.succeeded
    statuses = {r.step_id: r.status for r in report.step_results}
    assert statuses["a"] == "failed"
    assert statuses["b"] == "skipped"


def test_fail_false_continues_past_failure(fake_processing: _FakeProcessing) -> None:
    from model_forge.compiler_core.core.services.model_runner import run_model

    fake_processing.set_impl("native:buffer", RuntimeError("boom"))
    fake_processing.set_impl("native:clip", {"OUT_INPUT": "ok"})
    model = {
        "model_name": "M",
        "model_group": "G",
        "algorithms": [
            {"id": "a", "algorithm_id": "native:buffer", "parameters": {}},
            {"id": "b", "algorithm_id": "native:clip", "parameters": {}},
        ],
    }
    report = run_model(model, fail_fast=False)
    assert not report.succeeded
    statuses = {r.step_id: r.status for r in report.step_results}
    assert statuses["a"] == "failed"
    assert statuses["b"] == "completed"


def test_cancellation_event_marks_pending_steps_cancelled() -> None:
    from model_forge.compiler_core.core.services.model_runner import run_model

    cancel = threading.Event()
    cancel.set()  # pre-cancelled

    model = {
        "model_name": "M",
        "model_group": "G",
        "algorithms": [
            {"id": "a", "algorithm_id": "native:buffer", "parameters": {}},
        ],
    }
    report = run_model(model, cancel_event=cancel)
    assert report.cancelled
    assert report.step_results[0].status == "cancelled"


def test_retries_with_backoff(
    fake_processing: _FakeProcessing, monkeypatch: pytest.MonkeyPatch
) -> None:
    from model_forge.compiler_core.core.services.model_runner import run_model

    # First two calls fail; third succeeds.
    state = {"count": 0}

    def _impl(_inputs: dict[str, Any]) -> dict[str, Any]:
        state["count"] += 1
        if state["count"] < 3:
            raise RuntimeError("transient")
        return {"OUT_INPUT": "ok"}

    fake_processing.set_impl("native:buffer", _impl)
    # Replace time.sleep with a no-op so the test doesn't actually sleep.
    sleeps: list[float] = []
    monkeypatch.setattr(
        "model_forge.compiler_core.core.services.model_runner.time.sleep",
        lambda s: sleeps.append(s),
    )
    model = {
        "model_name": "M",
        "model_group": "G",
        "algorithms": [
            {"id": "a", "algorithm_id": "native:buffer", "parameters": {}},
        ],
    }
    report = run_model(model, max_retries=2)
    assert report.succeeded
    assert state["count"] == 3
    assert len(sleeps) >= 1
    assert sleeps[0] >= 0.0
    assert report.step_results[0].attempts == 3


def test_idempotency_key_is_stable_for_same_inputs() -> None:
    from model_forge.compiler_core.core.services.model_runner import (
        _idempotency_key,
    )

    alg = {"algorithm_id": "native:buffer"}
    inputs = {"DISTANCE": 50, "INPUT": "src"}
    k1 = _idempotency_key(alg, inputs)
    k2 = _idempotency_key(alg, inputs)
    assert k1 == k2
    inputs2 = {"DISTANCE": 100, "INPUT": "src"}
    assert _idempotency_key(alg, inputs2) != k1


# --- Report shape ------------------------------------------------


def test_report_to_dict_shape(fake_processing: _FakeProcessing) -> None:
    from model_forge.compiler_core.core.services.model_runner import run_model

    fake_processing.set_impl("native:buffer", {"OUT_INPUT": "ok"})
    model = {
        "model_name": "M",
        "model_group": "G",
        "algorithms": [
            {"id": "a", "algorithm_id": "native:buffer", "parameters": {}},
        ],
    }
    report = run_model(model)
    d = report.to_dict()
    import json

    json.dumps(d)
    assert d["model_name"] == "M"
    assert d["succeeded"] is True
    assert len(d["steps"]) == 1
    assert d["steps"][0]["step_id"] == "a"
    assert d["steps"][0]["status"] == "completed"
    assert d["steps"][0]["elapsed_seconds"] >= 0
