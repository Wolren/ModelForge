"""Tests for the structured error module."""

from __future__ import annotations

import json

import pytest

from model_forge.mcp_server.errors import (
    AlgorithmNotFoundError,
    CancelledError,
    ConfigError,
    InvalidJSONError,
    LayerNotFoundError,
    LLMNotConfiguredError,
    MFCPError,
    PipelineFailedError,
    ProviderError,
    QGISNotAvailableError,
    TimeoutError,
    UnknownExportFormatError,
    ValidationFailedError,
    error_response,
    error_response_json,
)


@pytest.mark.parametrize(
    "exc_cls, expected_code",
    [
        (LLMNotConfiguredError, "E_LLM_NOT_CONFIGURED"),
        (InvalidJSONError, "E_INVALID_JSON"),
        (AlgorithmNotFoundError, "E_ALG_NOT_FOUND"),
        (LayerNotFoundError, "E_LAYER_NOT_FOUND"),
        (ValidationFailedError, "E_VALIDATION_FAILED"),
        (PipelineFailedError, "E_PIPELINE_FAILED"),
        (QGISNotAvailableError, "E_QGIS_NOT_AVAILABLE"),
        (ProviderError, "E_LLM_PROVIDER"),
        (CancelledError, "E_CANCELLED"),
        (TimeoutError, "E_TIMEOUT"),
        (UnknownExportFormatError, "E_UNKNOWN_FORMAT"),
        (ConfigError, "E_CONFIG"),
    ],
)
def test_error_codes_are_stable(exc_cls, expected_code):
    """Codes are part of the public contract and must not change."""
    assert exc_cls.code == expected_code
    err = exc_cls("hi", details={"k": "v"})
    assert err.to_dict() == {
        "code": expected_code,
        "message": "hi",
        "details": {"k": "v"},
    }
    assert str(err) == "hi"


def test_error_response_omits_empty_details():
    err = MFCPError("x")
    assert err.to_dict() == {"code": "E_INTERNAL", "message": "x"}


def test_error_response_unknown_exception():
    """Anything that is not an MFCPError collapses into E_INTERNAL."""
    out = error_response(ValueError("oops"))
    assert out == {
        "code": "E_INTERNAL",
        "message": "oops",
        "details": {"exception_type": "ValueError"},
    }


def test_error_response_handles_empty_message():
    out = error_response(ValueError(""))
    # The fallback uses the class name when ``str(exc)`` is empty.
    assert out["code"] == "E_INTERNAL"
    assert out["message"]  # non-empty


def test_error_response_json_is_parseable():
    """Tools embed ``error_response_json(e)`` in their return values."""
    raw = error_response_json(AlgorithmNotFoundError("nope", details={"id": "native:buffer"}))
    parsed = json.loads(raw)
    assert parsed["code"] == "E_ALG_NOT_FOUND"
    assert parsed["details"] == {"id": "native:buffer"}
