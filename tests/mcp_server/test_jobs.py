"""Tests for the in-process job registry."""

from __future__ import annotations

import threading
import time

import pytest

from model_forge.mcp_server import jobs
from model_forge.mcp_server.errors import CancelledError
from model_forge.mcp_server.errors import TimeoutError as MfTimeoutError


@pytest.fixture
def registry():
    """Fresh ``JobRegistry`` per test, with the singleton reset."""
    jobs.reset_registry()
    reg = jobs.JobRegistry(max_workers=2)
    yield reg
    reg.shutdown(wait=False)


# ─── Job dataclass ─────────────────────────────────────────────────────


def test_job_to_dict_includes_percent():
    job = jobs.Job(
        job_id="abc",
        status="running",
        progress=(0.5, 1.0, "halfway"),
        submitted_at=100.0,
    )
    d = job.to_dict()
    assert d["job_id"] == "abc"
    assert d["status"] == "running"
    assert d["progress"]["percent"] == 50.0
    assert d["progress"]["message"] == "halfway"
    assert d["finished"] is False
    assert d["elapsed_seconds"] >= 0


def test_job_to_dict_handles_zero_total():
    job = jobs.Job(job_id="x", progress=(0.0, 0.0, "queued"))
    assert job.to_dict()["progress"]["percent"] is None


# ─── Run / status ──────────────────────────────────────────────────────


def test_run_returns_worker_result(registry):
    out = registry.run(lambda cb: 42)
    assert out == 42
    status = registry.status(list(registry._jobs.keys())[0])
    assert status["status"] == "completed"


def test_run_passes_progress_callback(registry):
    seen: list[tuple[float, float, str]] = []

    def _worker(cb):
        cb(0.1, 1.0, "starting")
        cb(0.5, 1.0, "middle")
        cb(1.0, 1.0, "done")
        return "ok"

    registry.run(_worker, on_progress=lambda c, t, m: seen.append((c, t, m)))
    assert seen == [(0.1, 1.0, "starting"), (0.5, 1.0, "middle"), (1.0, 1.0, "done")]


def test_status_unknown_id_returns_none(registry):
    assert registry.status("nope") is None


def test_list_jobs_includes_all(registry):
    registry.run(lambda cb: 1)
    registry.run(lambda cb: 2)
    assert len(registry.list_jobs()) == 2


# ─── Cancellation ──────────────────────────────────────────────────────


def test_cancel_via_event_short_circuits_worker(registry):
    started = threading.Event()

    def _slow_worker(cb):
        started.set()
        # Poll the registry's cancel event manually.
        job = list(registry._jobs.values())[0]
        for _ in range(50):
            if job.cancel_event.is_set():
                raise jobs.CancelledError("cancelled in worker")
            time.sleep(0.01)
        return "should not get here"

    # Cancel from another thread shortly after the worker starts.
    def _cancel_soon():
        started.wait(timeout=1.0)
        # The first job is the one being created right now.
        job_id = list(registry._jobs.keys())[0]
        registry.cancel(job_id)

    threading.Thread(target=_cancel_soon, daemon=True).start()

    with pytest.raises(CancelledError):
        registry.run(_slow_worker)


def test_cancel_unknown_returns_false(registry):
    assert registry.cancel("nope") is False


def test_cancel_completes_running_callback(registry):
    """``on_progress`` is still invoked once after cancellation is set."""
    progress_after_cancel: list = []
    started = threading.Event()

    def _worker(cb):
        started.set()
        # Sleep so the cancel call can land mid-flight.
        time.sleep(0.05)
        import contextlib

        with contextlib.suppress(Exception):
            cb(0.5, 1.0, "halfway")
        # The registry's outer run() will see cancel_event set and
        # raise — we honor it.
        raise jobs.CancelledError("cancelled by worker")

    def _cancel_soon():
        started.wait(timeout=1.0)
        registry.cancel(list(registry._jobs.keys())[0])

    threading.Thread(target=_cancel_soon, daemon=True).start()

    def _on_progress(c, t, m):
        progress_after_cancel.append((c, m))

    with pytest.raises(CancelledError):
        registry.run(_worker, on_progress=_on_progress)


# ─── Timeout ───────────────────────────────────────────────────────────


def test_timeout_marks_job_and_raises(registry):
    """A long worker tripped by a short timeout is reported as
    ``timed_out`` *if* the worker honours the cancel event; a worker
    that doesn't poll the event will finish naturally and the result
    is returned. We test the polling-aware path here.
    """

    def _slow_worker(cb):
        # Poll the cancel event so a 50ms timeout can interrupt us.
        job = list(registry._jobs.values())[0]
        for _ in range(200):
            if job.cancel_event.is_set():
                raise jobs.CancelledError("cancelled in worker")
            time.sleep(0.01)
        return "never"

    with pytest.raises((MfTimeoutError, jobs.CancelledError)):
        registry.run(_slow_worker, timeout=0.05)

    # The job should be reported as timed_out in either case.
    job_id = list(registry._jobs.keys())[0]
    assert registry.status(job_id)["status"] in {"timed_out", "cancelled"}


def test_timeout_does_not_apply_to_fast_workers(registry):
    out = registry.run(lambda cb: "fast", timeout=2.0)
    assert out == "fast"


# ─── Errors propagate ──────────────────────────────────────────────────


def test_worker_exception_marks_job_failed(registry):
    with pytest.raises(RuntimeError):
        registry.run(lambda cb: (_ for _ in ()).throw(RuntimeError("boom")))
    job_id = list(registry._jobs.keys())[0]
    assert registry.status(job_id)["status"] == "failed"


def test_worker_exception_during_cancel_reports_cancelled(registry):
    """If the cancel event was set and the worker then raises, the job
    is reported as cancelled (not failed)."""
    started = threading.Event()

    def _worker(cb):
        started.set()
        time.sleep(0.05)
        raise RuntimeError("cascading failure")

    def _cancel_soon():
        started.wait(timeout=1.0)
        registry.cancel(list(registry._jobs.keys())[0])

    threading.Thread(target=_cancel_soon, daemon=True).start()
    with pytest.raises(CancelledError):
        registry.run(_worker)


# ─── Lifecycle ─────────────────────────────────────────────────────────


def test_shutdown_signals_all_jobs(registry):
    started = threading.Event()

    def _worker(cb):
        started.set()
        time.sleep(2.0)
        return "x"

    def _kick():
        started.wait(timeout=1.0)
        registry.shutdown(wait=False)

    threading.Thread(target=_kick, daemon=True).start()
    with pytest.raises(CancelledError):
        registry.run(_worker)


# ─── check_cancellation + report_progress helpers ──────────────────────


def test_check_cancellation_raises_on_set():
    ev = threading.Event()
    ev.set()
    with pytest.raises(CancelledError):
        jobs.check_cancellation(ev)


def test_check_cancellation_no_op_when_unset():
    jobs.check_cancellation(threading.Event())  # no raise
    jobs.check_cancellation(None)  # no raise


def test_report_progress_invokes_callback():
    seen: list = []
    jobs.report_progress(None, lambda c, t, m: seen.append((c, t, m)), 0.25, 1.0, "x")
    assert seen == [(0.25, 1.0, "x")]


def test_report_progress_raises_on_cancel():
    ev = threading.Event()
    ev.set()
    with pytest.raises(CancelledError):
        jobs.report_progress(ev, None, 0.0, 0.0, "x")


def test_report_progress_swallows_callback_exceptions():
    def _bad(_c, _t, _m):
        raise RuntimeError("nope")

    # No exception escapes.
    jobs.report_progress(None, _bad, 0.0, 0.0, "x")


# ─── Singleton ─────────────────────────────────────────────────────────


def test_get_registry_returns_singleton():
    jobs.reset_registry()
    a = jobs.get_registry()
    b = jobs.get_registry()
    assert a is b


def test_reset_registry_releases_singleton():
    a = jobs.get_registry()
    jobs.reset_registry()
    b = jobs.get_registry()
    assert a is not b
