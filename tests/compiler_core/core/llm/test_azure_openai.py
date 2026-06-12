"""Tests for the Azure OpenAI backend (shim over OpenAI)."""

from __future__ import annotations

import json
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest

from model_forge.compiler_core.core.llm.azure_openai import AzureOpenAILLMBackend
from model_forge.compiler_core.core.llm.base import LLMRequestError


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def patch_urlopen(monkeypatch):
    queue: list = []
    calls: list = []

    def _stub(url, *args, **kwargs):
        if hasattr(url, "full_url"):
            calls.append(url.full_url)
        else:
            calls.append(url)
        if not queue:
            raise AssertionError("urlopen called more times than expected")
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item()
        return item

    monkeypatch.setattr("urllib.request.urlopen", _stub)
    return queue, calls


# ─── Construction validation ──────────────────────────────────────────


def test_requires_api_key():
    with pytest.raises(ValueError):
        AzureOpenAILLMBackend(
            api_key="",
            deployment="dep",
            azure_endpoint="https://r.openai.azure.com",
        )


def test_requires_deployment():
    with pytest.raises(ValueError):
        AzureOpenAILLMBackend(
            api_key="k",
            deployment="",
            azure_endpoint="https://r.openai.azure.com",
        )


def test_requires_endpoint():
    with pytest.raises(ValueError):
        AzureOpenAILLMBackend(api_key="k", deployment="dep", azure_endpoint="")


def test_default_api_version():
    b = AzureOpenAILLMBackend(
        api_key="k", deployment="d", azure_endpoint="https://r.openai.azure.com"
    )
    assert b.api_version == "2024-08-01-preview"


def test_custom_api_version():
    b = AzureOpenAILLMBackend(
        api_key="k",
        deployment="d",
        azure_endpoint="https://r.openai.azure.com",
        api_version="2024-10-01",
    )
    assert b.api_version == "2024-10-01"


def test_endpoint_trailing_slash_stripped():
    b = AzureOpenAILLMBackend(
        api_key="k", deployment="d", azure_endpoint="https://r.openai.azure.com/"
    )
    assert b.base_url == "https://r.openai.azure.com"


# ─── URL construction ────────────────────────────────────────────────


def test_url_has_deployment_and_api_version():
    b = AzureOpenAILLMBackend(
        api_key="k",
        deployment="my-gpt4",
        azure_endpoint="https://r.openai.azure.com",
        api_version="2024-08-01-preview",
    )
    url = b._build_url()
    assert "openai/deployments/my-gpt4/chat/completions" in url
    assert "api-version=2024-08-01-preview" in url


def test_url_percent_encodes_special_chars_in_deployment():
    b = AzureOpenAILLMBackend(
        api_key="k",
        deployment="dep with space",
        azure_endpoint="https://r.openai.azure.com",
    )
    url = b._build_url()
    assert "dep%20with%20space" in url


# ─── Header construction ─────────────────────────────────────────────


def test_headers_use_api_key_not_bearer():
    b = AzureOpenAILLMBackend(
        api_key="azure-secret",
        deployment="d",
        azure_endpoint="https://r.openai.azure.com",
    )
    headers = b._build_headers()
    assert headers["api-key"] == "azure-secret"
    # Crucially, no Bearer token.
    assert "Authorization" not in headers
    assert headers["Content-Type"] == "application/json"


def test_custom_headers_cannot_shadow_api_key():
    b = AzureOpenAILLMBackend(
        api_key="real",
        deployment="d",
        azure_endpoint="https://r.openai.azure.com",
        default_headers={"api-key": "Bearer attacker"},
    )
    headers = b._build_headers()
    assert headers["api-key"] == "real"


# ─── Body (inherits OpenAI) ──────────────────────────────────────────


def test_body_uses_deployment_as_model():
    b = AzureOpenAILLMBackend(
        api_key="k", deployment="my-gpt4", azure_endpoint="https://r.openai.azure.com"
    )
    payload = b._build_payload("s", "u")
    # Azure ignores this but we still send it for safety.
    assert payload["model"] == "my-gpt4"
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][1]["role"] == "user"


def test_extra_body_merged():
    b = AzureOpenAILLMBackend(
        api_key="k",
        deployment="d",
        azure_endpoint="https://r.openai.azure.com",
        extra_body={"safe_prompt": True},
    )
    payload = b._build_payload("s", "u")
    assert payload["safe_prompt"] is True


# ─── chat() ────────────────────────────────────────────────────────────


def test_chat_hits_azure_url(patch_urlopen):
    queue, calls = patch_urlopen
    queue.append(_FakeResponse({"choices": [{"message": {"role": "assistant", "content": "hi"}}]}))
    out = AzureOpenAILLMBackend(
        api_key="k",
        deployment="my-gpt4",
        azure_endpoint="https://r.openai.azure.com",
    ).chat("s", "u")
    assert out == "hi"
    assert calls[0].startswith("https://r.openai.azure.com/")
    assert "openai/deployments/my-gpt4/chat/completions" in calls[0]


def test_chat_429_retries(patch_urlopen):
    queue, calls = patch_urlopen
    queue.append(HTTPError("http://x", 429, "rate", {}, BytesIO(b"")))
    queue.append(_FakeResponse({"choices": [{"message": {"role": "assistant", "content": "ok"}}]}))
    out = AzureOpenAILLMBackend(
        api_key="k",
        deployment="d",
        azure_endpoint="https://r.openai.azure.com",
        max_retries=2,
    ).chat("s", "u")
    assert out == "ok"
    assert len(calls) == 2


def test_chat_500_persistent_failure(patch_urlopen):
    queue, calls = patch_urlopen
    queue.append(HTTPError("http://x", 500, "down", {}, BytesIO(b"")))
    queue.append(HTTPError("http://x", 500, "down", {}, BytesIO(b"")))
    queue.append(HTTPError("http://x", 500, "down", {}, BytesIO(b"")))
    with pytest.raises(LLMRequestError):
        AzureOpenAILLMBackend(
            api_key="k",
            deployment="d",
            azure_endpoint="https://r.openai.azure.com",
            max_retries=2,
        ).chat("s", "u")
    assert len(calls) == 3


def test_chat_does_not_retry_401(patch_urlopen):
    queue, calls = patch_urlopen
    queue.append(HTTPError("http://x", 401, "unauth", {}, BytesIO(b"")))
    with pytest.raises(LLMRequestError):
        AzureOpenAILLMBackend(
            api_key="k",
            deployment="d",
            azure_endpoint="https://r.openai.azure.com",
            max_retries=3,
        ).chat("s", "u")
    assert len(calls) == 1


def test_chat_urlerror(patch_urlopen):
    queue, _ = patch_urlopen
    queue.append(URLError("nope"))
    with pytest.raises(LLMRequestError) as ei:
        AzureOpenAILLMBackend(
            api_key="k",
            deployment="d",
            azure_endpoint="https://r.openai.azure.com",
        ).chat("s", "u")
    # Inherits the OpenAI error message.
    assert "OpenAI endpoint" in str(ei.value)
