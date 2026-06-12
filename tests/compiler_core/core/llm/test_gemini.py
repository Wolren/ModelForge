"""Tests for the Gemini backend."""

from __future__ import annotations

import json
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest

from model_forge.compiler_core.core.llm.base import (
    LLMRequestError,
    LLMResponseError,
    LLMTimeoutError,
)
from model_forge.compiler_core.core.llm.gemini import GeminiLLMBackend


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


# ─── Construction & validation ─────────────────────────────────────────


def test_requires_api_key():
    with pytest.raises(ValueError):
        GeminiLLMBackend(api_key="")


def test_default_base_url():
    b = GeminiLLMBackend(api_key="k", model="gemini-1.5-pro")
    assert b.base_url == "https://generativelanguage.googleapis.com"


def test_custom_base_url_is_stripped():
    b = GeminiLLMBackend(api_key="k", model="m", base_url="https://example.com/")
    assert b.base_url == "https://example.com"


# ─── URL & header construction ────────────────────────────────────────


def test_url_uses_x_goog_api_key_header_by_default():
    b = GeminiLLMBackend(api_key="k", model="gemini-1.5-pro")
    url = b._build_url()
    assert "models/gemini-1.5-pro:generateContent" in url
    assert "key=" not in url  # not in query
    headers = b._build_headers()
    assert headers["x-goog-api-key"] == "k"
    assert headers["Content-Type"] == "application/json"


def test_url_with_auth_in_query():
    b = GeminiLLMBackend(api_key="secret-key", model="m", auth_in_query=True)
    url = b._build_url()
    assert "key=secret-key" in url
    assert "x-goog-api-key" not in b._build_headers()


def test_url_percent_encodes_special_characters_in_model():
    b = GeminiLLMBackend(api_key="k", model="models/foo bar")
    url = b._build_url()
    # The "/" in the model is also percent-encoded (urllib safe='').
    assert "models%2Ffoo%20bar" in url


# ─── Payload construction ─────────────────────────────────────────────


def test_payload_uses_system_instruction_and_contents():
    b = GeminiLLMBackend(api_key="k", model="m")
    payload = b._build_payload("system prompt here", "user message here")
    assert payload["system_instruction"]["parts"][0]["text"] == "system prompt here"
    assert payload["contents"][0]["role"] == "user"
    assert payload["contents"][0]["parts"][0]["text"] == "user message here"
    assert payload["generationConfig"]["temperature"] == pytest.approx(0.1)


def test_extra_body_is_merged():
    b = GeminiLLMBackend(
        api_key="k",
        model="m",
        extra_body={"generationConfig": {"topP": 0.9}, "safetySettings": []},
    )
    payload = b._build_payload("s", "u")
    # generationConfig is deep-merged with the default.
    assert payload["generationConfig"]["topP"] == 0.9
    assert payload["generationConfig"]["temperature"] == pytest.approx(0.1)
    # Other top-level extra_body keys are added as siblings.
    assert payload["safetySettings"] == []


# ─── chat() ────────────────────────────────────────────────────────────


def test_chat_returns_text_part(patch_urlopen):
    queue, calls = patch_urlopen
    queue.append(
        _FakeResponse(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "first "},
                                {"text": "second"},
                            ]
                        }
                    }
                ]
            }
        )
    )
    out = GeminiLLMBackend(api_key="k", model="m").chat("s", "u")
    # We return the first text part (matches existing backend behavior).
    assert out == "first "
    assert calls[0].endswith(":generateContent")


def test_chat_skips_non_text_parts(patch_urlopen):
    queue, _ = patch_urlopen
    queue.append(
        _FakeResponse(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"functionCall": {"name": "x"}},
                                {"text": "the answer"},
                            ]
                        }
                    }
                ]
            }
        )
    )
    out = GeminiLLMBackend(api_key="k", model="m").chat("s", "u")
    assert out == "the answer"


def test_chat_no_candidates_raises(patch_urlopen):
    queue, _ = patch_urlopen
    queue.append(_FakeResponse({"candidates": []}))
    with pytest.raises(LLMResponseError) as ei:
        GeminiLLMBackend(api_key="k", model="m").chat("s", "u")
    assert "no candidates" in str(ei.value)


def test_chat_no_text_part_raises(patch_urlopen):
    queue, _ = patch_urlopen
    queue.append(_FakeResponse({"candidates": [{"content": {"parts": [{"functionCall": {}}]}}]}))
    with pytest.raises(LLMResponseError) as ei:
        GeminiLLMBackend(api_key="k", model="m").chat("s", "u")
    assert "no text part" in str(ei.value)


def test_chat_429_retries(patch_urlopen):
    queue, calls = patch_urlopen
    queue.append(HTTPError("http://x", 429, "rate", {}, BytesIO(b"")))
    queue.append(_FakeResponse({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}))
    out = GeminiLLMBackend(api_key="k", model="m", max_retries=2).chat("s", "u")
    assert out == "ok"
    assert len(calls) == 2


def test_chat_500_gives_up(patch_urlopen):
    queue, calls = patch_urlopen
    queue.append(HTTPError("http://x", 500, "down", {}, BytesIO(b"")))
    queue.append(HTTPError("http://x", 500, "down", {}, BytesIO(b"")))
    queue.append(HTTPError("http://x", 500, "down", {}, BytesIO(b"")))
    with pytest.raises(LLMRequestError):
        GeminiLLMBackend(api_key="k", model="m", max_retries=2).chat("s", "u")
    assert len(calls) == 3


def test_chat_does_not_retry_400(patch_urlopen):
    queue, calls = patch_urlopen
    queue.append(HTTPError("http://x", 400, "bad", {}, BytesIO(b"")))
    with pytest.raises(LLMRequestError):
        GeminiLLMBackend(api_key="k", model="m", max_retries=3).chat("s", "u")
    assert len(calls) == 1


def test_chat_timeout(patch_urlopen):
    queue, _ = patch_urlopen
    queue.append(TimeoutError("slow"))
    with pytest.raises(LLMTimeoutError):
        GeminiLLMBackend(api_key="k", model="m", max_retries=0, timeout=1).chat("s", "u")


def test_chat_urlerror_raises_typed(patch_urlopen):
    queue, _ = patch_urlopen
    queue.append(URLError("nope"))
    with pytest.raises(LLMRequestError) as ei:
        GeminiLLMBackend(api_key="k", model="m").chat("s", "u")
    assert "Could not reach Gemini" in str(ei.value)
