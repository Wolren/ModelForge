"""Tests for the wiring between ``generate_model`` and the job
registry. We stub out the real compiler pipeline with a fake
``_run_pipeline`` so the test runs offline in milliseconds.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from model_forge.mcp_server import jobs

# ─── Fakes ─────────────────────────────────────────────────────────────


class _FakePlan:
    issues: list = []


def _fake_run_pipeline(progress_callback=None, **_kwargs: Any):
    """Pretend to be the real compiler pipeline. Reports 4 stages
    and sleeps a bit between them so cancellation can land."""
    if progress_callback is not None:
        for msg in ("intent parsed", "plan ready", "validate done", "emit done"):
            progress_callback(msg)
            time.sleep(0.01)
    return _FakePlan(), {"algorithms": [{"id": "a"}], "inputs": []}


# ─── _make_progress_callback ──────────────────────────────────────────


def test_make_progress_callback_passes_through_structured_input():
    from model_forge.mcp_server.server import _make_progress_callback

    seen: list[tuple[float, float, str]] = []
    cb = _make_progress_callback(
        lambda c, t, m: seen.append((c, t, m)),
        cancel_event=threading.Event(),
    )
    cb(0.10, 1.0, "intent")
    cb(0.30, 1.0, "plan")
    cb(0.55, 1.0, "resolve")
    cb(0.95, 1.0, "emit")
    cb(1.00, 1.0, "completed")
    assert seen == [
        (0.10, 1.0, "intent"),
        (0.30, 1.0, "plan"),
        (0.55, 1.0, "resolve"),
        (0.95, 1.0, "emit"),
        (1.00, 1.0, "completed"),
    ]


def test_make_progress_callback_falls_back_to_legacy_string_input():
    from model_forge.mcp_server.server import _make_progress_callback

    seen: list[tuple[float, float, str]] = []
    cb = _make_progress_callback(
        lambda c, t, m: seen.append((c, t, m)),
        cancel_event=threading.Event(),
    )
    cb("legacy plain string")
    assert seen == [(0.0, 1.0, "legacy plain string")]


def test_make_progress_callback_raises_on_cancel():
    from model_forge.mcp_server.server import _make_progress_callback

    ev = threading.Event()
    ev.set()
    cb = _make_progress_callback(None, cancel_event=ev)
    with pytest.raises(jobs.CancelledError):
        cb(0.5, 1.0, "anything")
    # Also works for the legacy string path.
    cb2 = _make_progress_callback(None, cancel_event=ev)
    with pytest.raises(jobs.CancelledError):
        cb2("legacy string")


def test_make_progress_callback_noop_when_base_is_none():
    from model_forge.mcp_server.server import _make_progress_callback

    cb = _make_progress_callback(None, cancel_event=threading.Event())
    cb(0.5, 1.0, "x")
    cb("legacy")  # no raise, no forward


# ─── _make_progress_callback resilience ───────────────────────────────


def test_make_progress_callback_swallows_callback_exceptions():
    from model_forge.mcp_server.server import _make_progress_callback

    def _bad(_c, _t, _m):
        raise RuntimeError("oops")

    cb = _make_progress_callback(_bad, cancel_event=threading.Event())
    cb("intent parsed")  # no raise


# ─── End-to-end: registry + worker wrapping ──────────────────────────


def test_e2e_pipeline_with_registry_runs_to_completion():
    """Drop-in for ``generate_model``: submit the fake pipeline to
    the registry and check the job is reported as completed."""
    from model_forge.mcp_server.server import _make_progress_callback

    registry = jobs.JobRegistry()
    try:
        seen: list = []

        def _worker(cb):
            # ``cb`` is the registry's (current, total, message) wrapper.
            # The pipeline only knows how to call back with a string,
            # so we bridge via ``_make_progress_callback``.
            str_cb = _make_progress_callback(cb, cancel_event=threading.Event())
            return _fake_run_pipeline(progress_callback=str_cb)

        out = registry.run(_worker, on_progress=lambda c, t, m: seen.append((c, m)))
        plan, model = out
        assert model["algorithms"] == [{"id": "a"}]
        assert seen  # at least one progress emission
    finally:
        registry.shutdown(wait=False)


def test_e2e_cancellation_aborts_long_pipeline():
    registry = jobs.JobRegistry()
    try:
        started = threading.Event()
        proceed = threading.Event()

        def _slow_worker(cb):
            started.set()
            # Block until cancellation lands, or fail after 5s.
            for _ in range(500):
                if list(registry._jobs.values())[0].cancel_event.is_set():
                    raise jobs.CancelledError("cancelled mid-pipeline")
                time.sleep(0.01)
            return _FakePlan(), {"algorithms": [], "inputs": []}

        def _cancel_soon():
            started.wait(timeout=1.0)
            registry.cancel(list(registry._jobs.keys())[0])

        threading.Thread(target=_cancel_soon, daemon=True).start()
        with pytest.raises(jobs.CancelledError):
            registry.run(_slow_worker)
        proceed.set()  # silence the unused warning
    finally:
        registry.shutdown(wait=False)


def test_e2e_timeout_aborts_long_pipeline():
    registry = jobs.JobRegistry()
    try:

        def _slow_worker(cb):
            job = list(registry._jobs.values())[0]
            for _ in range(500):
                if job.cancel_event.is_set():
                    raise jobs.CancelledError("cancelled by timeout")
                time.sleep(0.01)
            return _FakePlan(), {"algorithms": [], "inputs": []}

        with pytest.raises((jobs.MfTimeoutError, jobs.CancelledError)):
            registry.run(_slow_worker, timeout=0.05)
    finally:
        registry.shutdown(wait=False)
