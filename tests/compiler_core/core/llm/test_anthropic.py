"""Tests for the Anthropic backend."""

from __future__ import annotations

import json
from io import BytesIO
from urllib.error import HTTPError

import pytest

from model_forge.compiler_core.core.llm.anthropic import AnthropicLLMBackend
from model_forge.compiler_core.core.llm.base import LLMRequestError, LLMResponseError


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


def test_anthropic_requires_api_key():
    with pytest.raises(ValueError):
        AnthropicLLMBackend(api_key="")


def test_headers_pin_api_key_and_version():
    b = AnthropicLLMBackend(api_key="sk-ant", model="claude-3-5-sonnet")
    headers = b._build_headers()
    assert headers["x-api-key"] == "sk-ant"
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["Content-Type"] == "application/json"


def test_custom_headers_cannot_shadow_security_headers():
    b = AnthropicLLMBackend(
        api_key="sk-ant",
        model="claude-3-5-sonnet",
        default_headers={"x-api-key": "Bearer attacker", "anthropic-version": "1999-01-01"},
    )
    headers = b._build_headers()
    assert headers["x-api-key"] == "sk-ant"
    assert headers["anthropic-version"] == "2023-06-01"


def test_payload_uses_top_level_system_field():
    b = AnthropicLLMBackend(api_key="k", model="m")
    payload = b._build_payload("system prompt", "user message")
    assert payload["system"] == "system prompt"
    assert payload["messages"] == [{"role": "user", "content": "user message"}]
    assert payload["max_tokens"] == 4096


def test_extra_body_is_merged():
    b = AnthropicLLMBackend(
        api_key="k", model="m", extra_body={"max_tokens": 8192, "metadata": {"user_id": "u"}}
    )
    payload = b._build_payload("s", "u")
    assert payload["max_tokens"] == 8192
    assert payload["metadata"] == {"user_id": "u"}


def test_chat_returns_first_text_block(patch_urlopen):
    queue, calls = patch_urlopen
    queue.append(
        _FakeResponse(
            {
                "content": [
                    {"type": "text", "text": "hello "},
                    {"type": "text", "text": "world"},
                ]
            }
        )
    )
    out = AnthropicLLMBackend(api_key="k").chat("s", "u")
    assert out == "hello "
    assert calls[0].endswith("/v1/messages")


def test_chat_skips_non_text_blocks(patch_urlopen):
    queue, _ = patch_urlopen
    queue.append(
        _FakeResponse(
            {
                "content": [
                    {"type": "tool_use", "id": "x"},
                    {"type": "text", "text": "answer"},
                ]
            }
        )
    )
    out = AnthropicLLMBackend(api_key="k").chat("s", "u")
    assert out == "answer"


def test_chat_no_text_block_raises(patch_urlopen):
    queue, _ = patch_urlopen
    queue.append(_FakeResponse({"content": [{"type": "tool_use", "id": "x"}]}))
    with pytest.raises(LLMResponseError) as ei:
        AnthropicLLMBackend(api_key="k").chat("s", "u")
    assert "no text block" in str(ei.value)


def test_chat_429_retries(patch_urlopen):
    queue, calls = patch_urlopen
    queue.append(HTTPError("http://x", 429, "rate", {}, BytesIO(b"")))
    queue.append(_FakeResponse({"content": [{"type": "text", "text": "ok"}]}))
    out = AnthropicLLMBackend(api_key="k", max_retries=2).chat("s", "u")
    assert out == "ok"
    assert len(calls) == 2


def test_chat_500_gives_up(patch_urlopen):
    queue, calls = patch_urlopen
    queue.append(HTTPError("http://x", 500, "down", {}, BytesIO(b"")))
    queue.append(HTTPError("http://x", 500, "down", {}, BytesIO(b"")))
    queue.append(HTTPError("http://x", 500, "down", {}, BytesIO(b"")))
    with pytest.raises(LLMRequestError):
        AnthropicLLMBackend(api_key="k", max_retries=2).chat("s", "u")
    assert len(calls) == 3
