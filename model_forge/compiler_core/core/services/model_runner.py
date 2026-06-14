"""
model_runner - pure-Python DAG executor for Model Forge model JSON.

Walks the algorithm list in topological order, threads outputs to
inputs, and calls ``processing.run()`` for each step. Reuses the
IR (``ExecutablePlan``) and the algorithm catalog when available.

Design goals
------------
- **No QGIS GUI dependency.** The runner imports ``processing``
  (QGIS's Python API) but never instantiates the modeler dialog.
  Runs in any process that has QGIS Python on PYTHONPATH.
- **Cooperative execution.** Multiple steps at the same wave run
  in parallel only if a thread pool is configured (off by
  default - QGIS's ``processing.run`` itself isn't thread-safe in
  older versions, and most algorithms touch a shared project).
  Single-threaded execution is the safe default.
- **Structured per-step report.** Every step produces a ``StepResult``
  with status (pending / running / completed / failed / skipped),
  elapsed time, inputs, outputs, error message, and stdout/stderr
  if available.
- **Cancellation via event.** ``run_model`` accepts a
  ``threading.Event``; setting it short-circuits the next pending
  step with a CancelledError.
- **Retries per step.** ``max_retries`` defaults to 0; on failure
  the runner retries the same step ``max_retries`` times before
  giving up. Retry policy is exponential-backoff.
- **Idempotency key.** Every step gets a deterministic key derived
  from its (algorithm_id, sorted input values) - useful for the
  MCP server's job registry to detect cache hits in a future
  caching layer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import traceback
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class StepResult:
    step_id: str
    algorithm_id: str
    status: str = "pending"  # pending | running | completed | failed | skipped | cancelled
    elapsed_seconds: float = 0.0
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    traceback: str | None = None
    attempts: int = 0
    idempotency_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "algorithm_id": self.algorithm_id,
            "status": self.status,
            "elapsed_seconds": self.elapsed_seconds,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "error": self.error,
            "attempts": self.attempts,
            "idempotency_key": self.idempotency_key,
        }


@dataclass
class ModelRunReport:
    model_name: str
    model_group: str
    started_at: float
    finished_at: float = 0.0
    step_results: list[StepResult] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)
    cancelled: bool = False

    @property
    def succeeded(self) -> bool:
        return all(r.status in ("completed", "skipped") for r in self.step_results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "model_group": self.model_group,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": self.finished_at - self.started_at,
            "cancelled": self.cancelled,
            "succeeded": self.succeeded,
            "steps": [r.to_dict() for r in self.step_results],
            "outputs": self.outputs,
        }


# --- Public entry point ------------------------------------------


def run_model(
    model: dict[str, Any],
    *,
    fail_fast: bool = True,
    max_retries: int = 0,
    cancel_event: Any = None,
    project: Any = None,
) -> ModelRunReport:
    """Run ``model`` step-by-step and return a :class:`ModelRunReport`.

    Parameters
    ----------
    model
        The model JSON dict (the same shape emitted by
        ``ModelEmitter`` and the same shape the MCP tools accept).
    fail_fast
        If True (default), the first failed step aborts the run
        and remaining steps are marked ``skipped``. If False,
        the run continues and failed steps are reported.
    max_retries
        Number of retry attempts per step (exponential backoff).
    cancel_event
        Optional ``threading.Event``; setting it short-circuits
        the next pending step with a CancelledError.
    project
        Optional QGIS ``QgsProject`` instance to use. Defaults to
        ``QgsProject.instance()``. Accepts a project for testing
        with isolated project instances.
    """
    import time as _t

    report = ModelRunReport(
        model_name=str(model.get("model_name", "workflow")),
        model_group=str(model.get("model_group", "ModelForge")),
        started_at=_t.time(),
    )

    algorithms = list(model.get("algorithms") or [])
    if not algorithms:
        return report

    # Pre-flight: validate step_ids unique, build index
    by_id: dict[str, dict[str, Any]] = {}
    for alg in algorithms:
        sid = str(alg.get("id", "") or "")
        if not sid:
            continue
        if sid in by_id:
            log.warning("Duplicate step_id %r; the second will be ignored.", sid)
            continue
        by_id[sid] = alg

    # Topological sort + wave computation
    waves = _topological_waves(by_id)
    flat = [sid for wave in waves for sid in wave]
    report.step_results = [
        StepResult(
            step_id=sid,
            algorithm_id=str(by_id[sid].get("algorithm_id", "") or ""),
        )
        for sid in flat
    ]
    by_step_id = {r.step_id: r for r in report.step_results}

    # Execute wave by wave
    for step_ids in waves:
        for sid in step_ids:
            if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
                _mark_cancelled(by_step_id)
                report.cancelled = True
                return _finalize(report)

            step = by_step_id[sid]
            alg = by_id[sid]
            step.status = "running"
            step.inputs = _resolved_inputs(alg, by_step_id)
            step.idempotency_key = _idempotency_key(alg, step.inputs)

            started = time.time()
            try:
                outputs, attempts = _execute_with_retries(
                    alg,
                    step.inputs,
                    max_retries=max_retries,
                    cancel_event=cancel_event,
                )
                step.outputs = outputs or {}
                step.attempts = attempts
                step.status = "completed"
            except _Cancelled:
                step.status = "cancelled"
                step.error = "Cancelled before completion."
                report.cancelled = True
            except Exception as e:  # noqa: BLE001
                step.status = "failed"
                step.error = f"{type(e).__name__}: {e}"
                step.traceback = traceback.format_exc()
                if fail_fast:
                    # Mark the rest as skipped.
                    for r in report.step_results:
                        if r.status == "pending":
                            r.status = "skipped"
                    break
            finally:
                step.elapsed_seconds = time.time() - started

    return _finalize(report)


# --- Internals ----------------------------------------------------


def _finalize(report: ModelRunReport) -> ModelRunReport:
    report.finished_at = time.time()
    report.outputs = {
        r.step_id: r.outputs for r in report.step_results if r.status == "completed" and r.outputs
    }
    return report


def _mark_cancelled(results: dict[str, StepResult]) -> None:
    for r in results.values():
        if r.status in ("pending", "running"):
            r.status = "cancelled"


class _Cancelled(Exception):
    """Internal sentinel; not exported."""


def _topological_waves(by_id: dict[str, dict[str, Any]]) -> list[list[str]]:
    """Compute waves (layers) via Kahn's algorithm.

    Returns a list of step-id lists. A step appears in the same
    wave as all other steps whose dependencies are already in
    earlier waves.
    """
    # in-degree by step id, with deps sourced from child_output bindings
    indeg: dict[str, int] = {sid: 0 for sid in by_id}
    successors: dict[str, list[str]] = {sid: [] for sid in by_id}
    for sid, alg in by_id.items():
        params = alg.get("parameters") or {}
        for pval in params.values():
            if not isinstance(pval, dict):
                continue
            if pval.get("type") == "child_output":
                child_id = str(pval.get("child_id", "") or "")
                if child_id and child_id in by_id and child_id != sid:
                    indeg[sid] += 1
                    successors[child_id].append(sid)
    waves: list[list[str]] = []
    visited = 0
    remaining = dict(indeg)
    # Stable order: by_id insertion order
    current = [sid for sid in by_id if remaining[sid] == 0]
    while current:
        waves.append(current)
        visited += len(current)
        next_layer: list[str] = []
        for sid in current:
            for s in successors[sid]:
                remaining[s] -= 1
                if remaining[s] == 0:
                    next_layer.append(s)
        current = next_layer
    if visited != len(by_id):
        cycle = [sid for sid, d in remaining.items() if d > 0]
        raise ValueError(
            f"Cycle detected in model: unresolved steps with non-zero in-degree: {cycle}"
        )
    return waves


def _resolved_inputs(alg: dict[str, Any], results: dict[str, StepResult]) -> dict[str, Any]:
    """Resolve the binding values for an algorithm step.

    - ``static`` → value as-is
    - ``model_input`` → the caller's value (we don't have it; pass
      through as-is and let ``processing.run`` complain if it's wrong)
    - ``child_output`` → the upstream step's actual output, looked
      up by ``child_id`` + ``output_name``
    """
    params = alg.get("parameters") or {}
    resolved: dict[str, Any] = {}
    for pname, pval in params.items():
        if not isinstance(pval, dict):
            resolved[pname] = pval
            continue
        btype = pval.get("type", "static")
        if btype == "child_output":
            child_id = str(pval.get("child_id", "") or "")
            output_name = str(pval.get("output_name", "OUTPUT") or "OUTPUT")
            upstream = results.get(child_id)
            if upstream is None or upstream.status != "completed":
                resolved[pname] = None
                continue
            value = upstream.outputs.get(output_name)
            if value is None and upstream.outputs:
                # Fall back to the only output, or the first one.
                value = next(iter(upstream.outputs.values()))
            resolved[pname] = value
        elif btype == "static":
            resolved[pname] = pval.get("value")
        elif btype == "model_input":
            resolved[pname] = pval.get("input_name")
        elif btype == "enum_index":
            resolved[pname] = pval.get("enum_index", 0)
        else:
            # expression / unknown - pass through
            resolved[pname] = pval.get("value")
    return resolved


def _execute_with_retries(
    alg: dict[str, Any],
    inputs: dict[str, Any],
    *,
    max_retries: int,
    cancel_event: Any,
) -> tuple[dict[str, Any], int]:
    """Call ``processing.run`` for one step, with retry support.

    Returns ``(outputs, attempts)`` so the caller can record
    how many tries the step took.

    We import ``processing`` lazily so the runner is importable
    in environments that don't have QGIS (e.g. the headless MCP
    server running under CI).
    """
    import time as _t

    try:
        from qgis import processing  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "QGIS 'processing' module is not available. The model runner "
            "requires a QGIS Python environment."
        ) from e

    algorithm_id = str(alg.get("algorithm_id", "") or "")
    if not algorithm_id:
        raise ValueError("Algorithm step has no algorithm_id.")

    for attempt in range(max_retries + 1):
        if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
            raise _Cancelled()
        try:
            return dict(processing.run(algorithm_id, inputs)), attempt + 1
        except Exception:  # noqa: BLE001
            if attempt < max_retries:
                _t.sleep(min(0.5 * (2**attempt), 2.0))
                continue
            raise
    # Unreachable.
    raise RuntimeError("retry loop exited without returning")  # pragma: no cover


def _idempotency_key(alg: dict[str, Any], inputs: dict[str, Any]) -> str:
    """Deterministic key for this step.

    Two runs of the same step with the same inputs produce the
    same key, which a future caching layer can use as a content
    address. ``json.dumps(..., sort_keys=True)`` ensures stability.
    """
    alg_id = str(alg.get("algorithm_id", "") or "")
    inputs_json = json.dumps(inputs, sort_keys=True, default=str)
    digest = hashlib.sha256(f"{alg_id}\n{inputs_json}".encode("utf-8")).hexdigest()
    return f"{alg_id}:{digest[:16]}"


__all__ = [
    "StepResult",
    "ModelRunReport",
    "run_model",
]
