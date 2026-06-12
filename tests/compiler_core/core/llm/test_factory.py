"""Tests for the LLM backend factory.

These tests exercise the factory and the contract the backends
implement — they don't hit the network. ``urllib.request.urlopen`` is
stubbed out in those that do.
"""

from __future__ import annotations

from typing import Any

import pytest

from model_forge.compiler_core.core.llm import factory

# ─── Factory dispatch ───────────────────────────────────────────────────


def test_factory_routes_ollama(monkeypatch):
    captured: dict[str, Any] = {}

    class FakeOllama:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("model_forge.compiler_core.core.llm.ollama.OllamaLLMBackend", FakeOllama)
    factory.create_backend({"provider": "ollama", "model": "q", "base_url": "http://x"})
    assert captured["model"] == "q"
    assert captured["base_url"] == "http://x"


def test_factory_routes_openai_defaults(monkeypatch):
    captured: dict[str, Any] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("model_forge.compiler_core.core.llm.openai.OpenAILLMBackend", FakeOpenAI)
    factory.create_backend({"provider": "openai", "api_key": "k"})
    # Default base_url is the public OpenAI service.
    assert captured["base_url"] == "https://api.openai.com"
    assert captured["model"] == "gpt-4o-mini"


def test_factory_routes_openai_compat_requires_base_url():
    with pytest.raises(ValueError) as ei:
        factory.create_backend({"provider": "openai_compat", "model": "x"})
    assert "base_url" in str(ei.value)


def test_factory_routes_anthropic(monkeypatch):
    captured: dict[str, Any] = {}

    class FakeAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        "model_forge.compiler_core.core.llm.anthropic.AnthropicLLMBackend", FakeAnthropic
    )
    factory.create_backend(
        {
            "provider": "anthropic",
            "model": "claude-3-5-sonnet",
            "api_key": "ak",
        }
    )
    assert captured["base_url"] == "https://api.anthropic.com"
    assert captured["api_key"] == "ak"


def test_factory_routes_azure_openai(monkeypatch):
    captured: dict[str, Any] = {}

    class FakeAzure:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        "model_forge.compiler_core.core.llm.azure_openai.AzureOpenAILLMBackend",
        FakeAzure,
    )
    factory.create_backend(
        {
            "provider": "azure_openai",
            "model": "my-gpt4",  # deployment name
            "api_key": "azure-key",
            "base_url": "https://r.openai.azure.com",
        }
    )
    assert captured["deployment"] == "my-gpt4"
    assert captured["azure_endpoint"] == "https://r.openai.azure.com"
    assert captured["api_key"] == "azure-key"
    # Default api_version is set.
    assert captured["api_version"] == "2024-08-01-preview"


def test_factory_routes_gemini(monkeypatch):
    captured: dict[str, Any] = {}

    class FakeGemini:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("model_forge.compiler_core.core.llm.gemini.GeminiLLMBackend", FakeGemini)
    factory.create_backend(
        {
            "provider": "gemini",
            "model": "gemini-1.5-pro",
            "api_key": "gk",
        }
    )
    assert captured["base_url"] == "https://generativelanguage.googleapis.com"
    assert captured["api_version"] == "v1beta"
    # auth_in_query defaults to False.
    assert captured["auth_in_query"] is False


def test_factory_forwards_headers_and_extras(monkeypatch):
    captured: dict[str, Any] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("model_forge.compiler_core.core.llm.openai.OpenAILLMBackend", FakeOpenAI)
    factory.create_backend(
        {
            "provider": "openai_compat",
            "base_url": "http://x",
            "api_key": "k",
            "model": "m",
            "default_headers": {"X-Org": "acme"},
            "extra_body": {"safe_prompt": True},
            "max_retries": 5,
        }
    )
    assert captured["default_headers"] == {"X-Org": "acme"}
    assert captured["extra_body"] == {"safe_prompt": True}
    assert captured["max_retries"] == 5


def test_factory_unknown_provider_raises():
    with pytest.raises(ValueError) as ei:
        factory.create_backend({"provider": "magic", "model": "x"})
    assert "Unknown LLM provider" in str(ei.value)
    # Error message enumerates the supported set.
    for name in ("ollama", "openai", "openai_compat", "anthropic"):
        assert name in str(ei.value)
