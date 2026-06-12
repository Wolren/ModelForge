"""Structured error types for the Model Forge MCP server.

Every public tool wraps its failures in one of these exceptions. The error
response shape is uniform across tools::

    {"code": "E_LLM_NOT_CONFIGURED", "message": "...", "details": {...}}

The codes are part of the public contract; do not change a string once
shipped, only add new ones.
"""

from __future__ import annotations

from typing import Any


class MFCPError(Exception):
    """Base class for structured MCP server errors.

    ``code`` is a stable, machine-readable string. ``message`` is human-
    readable. ``details`` is an arbitrary dict with extra context (e.g.
    the failing field, the algorithm id, the raw LLM response excerpt).
    """

    code: str = "E_INTERNAL"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            out["details"] = self.details
        return out

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class LLMNotConfiguredError(MFCPError):
    code = "E_LLM_NOT_CONFIGURED"


class InvalidJSONError(MFCPError):
    code = "E_INVALID_JSON"


class AlgorithmNotFoundError(MFCPError):
    code = "E_ALG_NOT_FOUND"


class LayerNotFoundError(MFCPError):
    code = "E_LAYER_NOT_FOUND"


class ValidationFailedError(MFCPError):
    code = "E_VALIDATION_FAILED"


class PipelineFailedError(MFCPError):
    code = "E_PIPELINE_FAILED"


class QGISNotAvailableError(MFCPError):
    code = "E_QGIS_NOT_AVAILABLE"


class ProviderError(MFCPError):
    code = "E_LLM_PROVIDER"


class CancelledError(MFCPError):
    code = "E_CANCELLED"


class TimeoutError(MFCPError):
    code = "E_TIMEOUT"


class UnknownExportFormatError(MFCPError):
    code = "E_UNKNOWN_FORMAT"


class ConfigError(MFCPError):
    code = "E_CONFIG"


def error_response(err: BaseException) -> dict[str, Any]:
    """Return the canonical error payload for any exception.

    Unknown exceptions collapse into ``E_INTERNAL`` so the wire format
    stays predictable.
    """
    if isinstance(err, MFCPError):
        return err.to_dict()
    return {
        "code": "E_INTERNAL",
        "message": str(err) or type(err).__name__,
        "details": {"exception_type": type(err).__name__},
    }


def error_response_json(err: BaseException) -> str:
    """JSON-stringify the canonical error response.

    Tools that historically returned a string payload (rather than a dict)
    can ``return error_response_json(e)`` to keep their public signature
    stable while still emitting structured errors.
    """
    import json

    return json.dumps(error_response(err), indent=2, ensure_ascii=False)
