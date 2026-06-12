"""Tests for the Ollama backend's extra_body / retry logic."""

from __future__ import annotations

import json
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest

from model_forge.compiler_core.core.llm.base import LLMRequestError
from model_forge.compiler_core.core.llm.ollama import OllamaLLMBackend


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


def test_extra_body_options_are_merged():
    """Ollama ``options`` are merged; arbitrary keys are added at top level."""
    b = OllamaLLMBackend(
        model="q",
        base_url="http://x",
        extra_body={"options": {"num_ctx": 8192}, "keep_alive": "5m"},
    )
    payload = b._build_payload("sys", "user")
    assert payload["options"]["num_ctx"] == 8192
    assert payload["options"]["temperature"] == pytest.approx(0.1)
    assert payload["keep_alive"] == "5m"


def test_custom_headers_are_added():
    b = OllamaLLMBackend(model="q", default_headers={"Authorization": "Bearer x"})
    payload = b._build_payload("sys", "u")
    assert payload["model"] == "q"


def test_chat_returns_message_content(patch_urlopen):
    queue, calls = patch_urlopen
    queue.append(_FakeResponse({"message": {"role": "assistant", "content": "hi"}}))
    out = OllamaLLMBackend(model="q").chat("sys", "user")
    assert out == "hi"
    assert calls[0].endswith("/api/chat")


def test_chat_urlerror_raises_typed(patch_urlopen):
    queue, _ = patch_urlopen
    queue.append(URLError("nope"))
    with pytest.raises(LLMRequestError) as ei:
        OllamaLLMBackend(model="q").chat("s", "u")
    assert "Ollama" in str(ei.value)


def test_chat_retries_5xx(patch_urlopen):
    queue, calls = patch_urlopen
    queue.append(HTTPError("http://x", 500, "boom", {}, BytesIO(b"")))
    queue.append(_FakeResponse({"message": {"content": "ok"}}))
    out = OllamaLLMBackend(model="q", max_retries=2).chat("s", "u")
    assert out == "ok"
    assert len(calls) == 2


def test_chat_does_not_retry_400(patch_urlopen):
    queue, calls = patch_urlopen
    queue.append(HTTPError("http://x", 400, "bad", {}, BytesIO(b"")))
    with pytest.raises(LLMRequestError):
        OllamaLLMBackend(model="q", max_retries=3).chat("s", "u")
    assert len(calls) == 1
