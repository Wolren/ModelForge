"""Tests for the OpenAI backend's header / extra_body / retry logic."""

from __future__ import annotations

import json
from io import BytesIO
from urllib.error import HTTPError

import pytest

from model_forge.compiler_core.core.llm.base import LLMRequestError, LLMTimeoutError
from model_forge.compiler_core.core.llm.openai import OpenAILLMBackend


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
    """Patch ``urllib.request.urlopen`` with a queue of responses/errors."""
    queue: list = []
    calls: list = []

    def _stub(url, *args, **kwargs):
        # ``urlopen`` accepts a URL string or a ``Request``; normalize to
        # the full URL string for the recorder.
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


def test_build_headers_includes_auth_and_content_type():
    b = OpenAILLMBackend(api_key="sk", model="x")
    headers = b._build_headers()
    assert headers["Authorization"] == "Bearer sk"
    assert headers["Content-Type"] == "application/json"


def test_custom_headers_are_merged_but_cannot_shadow_auth():
    b = OpenAILLMBackend(
        api_key="sk",
        model="x",
        default_headers={"X-Org": "acme", "Authorization": "Bearer attacker"},
    )
    headers = b._build_headers()
    # Auth must be re-pinned so the custom header can't replace it.
    assert headers["Authorization"] == "Bearer sk"
    assert headers["X-Org"] == "acme"


def test_extra_body_is_merged_into_payload():
    b = OpenAILLMBackend(api_key="sk", model="x", extra_body={"safe_prompt": True, "top_p": 0.9})
    payload = b._build_payload("sys", "user")
    assert payload["safe_prompt"] is True
    assert payload["top_p"] == 0.9
    assert payload["temperature"] == pytest.approx(0.1)


def test_chat_returns_message_content(patch_urlopen):
    queue, calls = patch_urlopen
    queue.append(
        _FakeResponse(
            {
                "choices": [
                    {"message": {"role": "assistant", "content": "hello"}},
                ]
            }
        )
    )
    out = OpenAILLMBackend(api_key="sk", model="x").chat("sys", "user")
    assert out == "hello"
    assert len(calls) == 1
    assert calls[0].endswith("/v1/chat/completions")


def test_chat_retries_on_429(patch_urlopen):
    queue, calls = patch_urlopen
    queue.append(HTTPError("http://x", 429, "rate", {}, BytesIO(b"")))
    queue.append(_FakeResponse({"choices": [{"message": {"content": "ok"}}]}))
    out = OpenAILLMBackend(api_key="sk", model="x", max_retries=2).chat("s", "u")
    assert out == "ok"
    assert len(calls) == 2


def test_chat_does_not_retry_on_4xx_other_than_429(patch_urlopen):
    queue, calls = patch_urlopen
    queue.append(HTTPError("http://x", 401, "unauth", {}, BytesIO(b"")))
    b = OpenAILLMBackend(api_key="sk", model="x", max_retries=3)
    with pytest.raises(LLMRequestError) as ei:
        b.chat("s", "u")
    assert "401" in str(ei.value)
    assert len(calls) == 1


def test_chat_retries_on_500_then_succeeds(patch_urlopen):
    queue, calls = patch_urlopen
    queue.append(HTTPError("http://x", 503, "down", {}, BytesIO(b"")))
    queue.append(_FakeResponse({"choices": [{"message": {"content": "ok"}}]}))
    out = OpenAILLMBackend(api_key="sk", model="x", max_retries=2).chat("s", "u")
    assert out == "ok"
    assert len(calls) == 2


def test_chat_gives_up_on_persistent_5xx(patch_urlopen):
    queue, calls = patch_urlopen
    queue.append(HTTPError("http://x", 503, "down", {}, BytesIO(b"")))
    queue.append(HTTPError("http://x", 503, "down", {}, BytesIO(b"")))
    queue.append(HTTPError("http://x", 503, "down", {}, BytesIO(b"")))
    b = OpenAILLMBackend(api_key="sk", model="x", max_retries=2)
    with pytest.raises(LLMRequestError):
        b.chat("s", "u")
    assert len(calls) == 3


def test_chat_timeout_raises(patch_urlopen):
    queue, calls = patch_urlopen
    queue.append(TimeoutError("slow"))
    b = OpenAILLMBackend(api_key="sk", model="x", max_retries=0, timeout=1)
    with pytest.raises(LLMTimeoutError):
        b.chat("s", "u")
    assert len(calls) == 1
