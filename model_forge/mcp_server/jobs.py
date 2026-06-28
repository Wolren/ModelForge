"""In-process job registry for long-running MCP tools.

The MCP server's ``generate_model`` tool can take a long time (the
underlying LLM call alone is often 30s-2min, and the surrounding
compiler pipeline can run for several minutes on a complex model).
Long-running tools need three things:

1. **Progress reporting** — clients (Claude Desktop, Cursor, Cline)
   surface a progress bar to the user; without it the client looks
   hung.
2. **Cancellation** — the user may want to abort a long generation
   after kicking it off.
3. **Timeout** — even without a cancel, the server should refuse
   to wait forever.

This module owns a small ``ThreadPoolExecutor`` and a dict of
``Job`` records. The job records carry:

* the future for the running coroutine
* a ``cancel_event`` that the running task can poll (this is the
  only reliable way to cancel an in-flight LLM request, since the
  LLM SDKs we use don't honour ``Future.cancel()`` once the
  request is on the wire)
* the most recent progress message
* the time the job was submitted and the time it finished

The :class:`JobRegistry` is a process-wide singleton. The MCP
server creates it at startup; the QGIS widget glue stops it when
the server is stopped.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import CancelledError
from .errors import TimeoutError as MfTimeoutError

# Re-export for callers that import the alias from this module.
__all__ = [
    "Job",
    "JobRegistry",
    "MfTimeoutError",
    "CancelledError",
    "ProgressCallback",
    "check_cancellation",
    "get_registry",
    "report_progress",
    "reset_registry",
]

log = logging.getLogger(__name__)


ProgressCallback = Callable[[float, float, str], None]


@dataclass
class Job:
    """Record for one in-flight generation.

    Attributes
    ----------
    job_id
        UUID4 string, stable for the lifetime of the job.
    status
        One of ``"pending"``, ``"running"``, ``"completed"``,
        ``"failed"``, ``"cancelled"``, ``"timed_out"``.
    progress
        Last reported ``(current, total, message)`` tuple. ``total``
        is a float; clients can use ``current / total`` for a
        percentage.
    submitted_at
        ``time.monotonic()`` of submission.
    finished_at
        ``time.monotonic()`` of completion (or None while running).
    result
        Whatever the worker returned. Populated only on success.
    error
        Stringified exception. Populated only on failure.
    cancel_event
        Set by :meth:`JobRegistry.cancel` to signal the worker.
    future
        The :class:`concurrent.futures.Future` for the worker.
    """

    job_id: str
    status: str = "pending"
    progress: tuple[float, float, str] = (0.0, 0.0, "queued")
    submitted_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    result: Any = None
    error: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    future: Future | None = None

    def to_dict(self) -> dict[str, Any]:
        """Public, JSON-safe view of the job's state."""
        current, total, message = self.progress
        return {
            "job_id": self.job_id,
            "status": self.status,
            "progress": {
                "current": current,
                "total": total,
                "message": message,
                "percent": (current / total * 100.0) if total else None,
            },
            "elapsed_seconds": ((self.finished_at or time.monotonic()) - self.submitted_at),
            "finished": self.finished_at is not None,
            "error": self.error,
        }


class JobRegistry:
    """Singleton registry of long-running jobs.

    The registry owns a single :class:`ThreadPoolExecutor`. Each
    :meth:`run` call submits a callable to it and tracks the resulting
    :class:`Job`. Cancellation works by signalling the job's
    ``cancel_event``; the running coroutine is expected to call
    :func:`check_cancellation` periodically (the compiler pipeline
    does this at every progress message).
    """

    def __init__(self, max_workers: int = 2) -> None:
        # ``max_workers=2`` so the user can run one model generation
        # and still have a slot for fast operations like list_algorithms.
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="mf-job",
        )
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    # ─── Lifecycle ────────────────────────────────────────────────────

    def shutdown(self, wait: bool = True) -> None:
        """Stop accepting new jobs and (optionally) wait for in-flight ones."""
        with self._lock:
            for job in self._jobs.values():
                job.cancel_event.set()
                if job.future is not None:
                    job.future.cancel()
            self._jobs.clear()
        self._executor.shutdown(wait=wait, cancel_futures=not wait)

    # ─── Submission ───────────────────────────────────────────────────

    def run(
        self,
        worker: Callable[[ProgressCallback], Any],
        *,
        job_id: str | None = None,
        on_progress: ProgressCallback | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Run ``worker(progress_callback)`` in a thread.

        Parameters
        ----------
        worker
            A callable that takes a single ``progress_callback`` and
            returns the result. It must be a *blocking* function; if
            you have an async function, wrap it with
            :func:`asyncio.to_thread` first.
        job_id
            Optional explicit id. A UUID is generated if missing.
        on_progress
            Optional callback invoked with ``(current, total, message)``.
            Typically forwarded to MCP's ``report_progress``.
        timeout
            Optional wall-clock timeout in seconds. If exceeded, the
            job's ``cancel_event`` is set and a :class:`MfTimeoutError`
            is raised once the worker finishes (or immediately, if the
            worker honours cancellation).

        Returns
        -------
        Whatever ``worker`` returned.
        """
        if job_id is None:
            job_id = uuid.uuid4().hex
        job = Job(job_id=job_id)
        with self._lock:
            self._jobs[job_id] = job

        cancel_event = job.cancel_event
        progress_box: dict[str, tuple[float, float, str]] = {"last": (0.0, 0.0, "starting")}

        def _wrapped_progress(current: float, total: float, message: str) -> None:
            progress_box["last"] = (current, total, message)
            job.progress = (current, total, message)
            if on_progress is not None:
                try:
                    on_progress(current, total, message)
                except Exception:  # noqa: BLE001
                    log.debug("progress callback failed", exc_info=True)

        def _runner() -> Any:
            job.status = "running"
            try:
                if cancel_event.is_set():
                    raise CancelledError(
                        "Job cancelled before start",
                        details={"job_id": job_id},
                    )
                result = worker(_wrapped_progress)
                if cancel_event.is_set():
                    # Worker returned while cancellation was pending.
                    raise CancelledError(
                        "Job cancelled",
                        details={"job_id": job_id},
                    )
                job.status = "completed"
                job.result = result
                return result
            except CancelledError as e:
                job.status = "cancelled"
                job.error = e.message
                raise
            except Exception as e:  # noqa: BLE001
                # If the cancel event was set, the failure is almost
                # certainly a downstream consequence of the cancel.
                if cancel_event.is_set():
                    job.status = "cancelled"
                    job.error = str(e)
                    raise CancelledError(
                        "Job cancelled",
                        details={"job_id": job_id, "cause": str(e)},
                    ) from e
                job.status = "failed"
                job.error = str(e)
                raise
            finally:
                job.finished_at = time.monotonic()

        future = self._executor.submit(_runner)
        job.future = future

        if timeout is not None:
            self._install_timeout(job_id, timeout, cancel_event)

        return future.result()  # blocks the caller; MCP wraps in to_thread

    # ─── Cancellation & status ───────────────────────────────────────

    def cancel(self, job_id: str) -> bool:
        """Signal cancellation. Returns ``True`` if a job was found."""
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return False
        job.cancel_event.set()
        return True

    def status(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return None
        return job.to_dict()

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [j.to_dict() for j in self._jobs.values()]

    # ─── Internals ───────────────────────────────────────────────────

    def _install_timeout(
        self,
        job_id: str,
        timeout: float,
        cancel_event: threading.Event,
    ) -> None:
        """Fire the cancel event after ``timeout`` seconds.

        We use a daemon :class:`threading.Timer` rather than
        :func:`concurrent.futures.Future.result(timeout=)` because the
        worker thread doesn't honour ``Future.cancel()`` once the LLM
        request is on the wire. The timer sets the cancel event;
        :meth:`run`'s inner check then raises ``CancelledError`` on
        the next progress poll (or at the natural return point).
        """

        def _fire() -> None:
            if cancel_event.is_set():
                return
            cancel_event.set()
            with self._lock:
                job = self._jobs.get(job_id)
            if job is not None and job.finished_at is None:
                job.status = "timed_out"
                job.error = f"Timed out after {timeout}s"
                log.info("Job %s timed out after %ss", job_id, timeout)

        timer = threading.Timer(timeout, _fire)
        timer.daemon = True
        timer.start()


# ─── Module-level singleton ────────────────────────────────────────────

_registry: JobRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> JobRegistry:
    """Return the process-wide :class:`JobRegistry`.

    Lazily created on first use; tests can call :func:`reset_registry`
    to obtain a fresh instance.
    """
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = JobRegistry()
    return _registry


def reset_registry() -> None:
    """Tear down and drop the singleton. Used by tests."""
    global _registry
    with _registry_lock:
        if _registry is not None:
            _registry.shutdown(wait=False)
        _registry = None


# ─── Async helpers ─────────────────────────────────────────────────────


async def run_in_thread(
    worker: Callable[[ProgressCallback], Any],
    *,
    on_progress: ProgressCallback | None = None,
    timeout: float | None = None,
) -> Any:
    """Async wrapper around :meth:`JobRegistry.run`.

    The MCP ``generate_model`` tool is ``async def``; the underlying
    compiler pipeline is synchronous. This coroutine submits the
    worker to the registry, then awaits the result via
    :func:`asyncio.to_thread` so the MCP event loop is not blocked.

    Cancellation: :func:`asyncio.CancelledError` raised on this
    coroutine is forwarded to the job's ``cancel_event``.
    """
    registry = get_registry()
    loop = asyncio.get_running_loop()
    future: Future = loop.run_in_executor(
        None,  # default executor
        lambda: registry.run(worker, on_progress=on_progress, timeout=timeout),
    )
    try:
        return await future
    except asyncio.CancelledError:
        # The MCP runtime cancelled the coroutine (user pressed
        # "Stop" in the client). Set the cancel event so the worker
        # can short-circuit on its next progress poll.
        # The JobRegistry doesn't know our future; we just iterate
        # active jobs and set the event for the most recent one.
        with registry._lock:  # noqa: SLF001 - intentional
            for job in registry._jobs.values():  # noqa: SLF001
                if job.future is not None and not job.future.done():
                    job.cancel_event.set()
                    break
        raise


def check_cancellation(cancel_event: threading.Event | None) -> None:
    """Raise :class:`CancelledError` if the event is set.

    Worker functions call this between heavy stages. It is a no-op
    when the event is ``None`` (the registry's API exposes the
    event, but tests can omit it).
    """
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError("Job cancelled")


def report_progress(
    cancel_event: threading.Event | None,
    callback: ProgressCallback | None,
    current: float,
    total: float,
    message: str,
) -> None:
    """Convenience: check cancellation, then report progress.

    The compiler pipeline can call this at every stage to keep
    clients informed. The ``cancel_event`` is the same handle the
    registry owns; passing it in lets us also raise on cancel.
    """
    check_cancellation(cancel_event)
    if callback is not None:
        try:
            callback(current, total, message)
        except Exception:  # noqa: BLE001
            log.debug("progress callback raised", exc_info=True)
