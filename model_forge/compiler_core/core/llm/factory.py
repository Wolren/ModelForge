"""
LLMBackendFactory - creates backends from a config dict.

Supported ``provider`` values:

* ``ollama``         — local Ollama server (no API key)
* ``openai``         — OpenAI Chat Completions
* ``openai_compat``  — any OpenAI-compatible HTTP endpoint (vLLM, LM
  Studio, OpenRouter, llama.cpp server, etc.). Same wire format as
  ``openai``; the only difference is the default ``base_url``.
* ``azure_openai``   — Azure-hosted OpenAI deployments. Same wire
  format as ``openai`` but with deployment-in-URL, ``api-key``
  header instead of Bearer, and ``api-version`` query param.
* ``anthropic``      — Anthropic Messages API
* ``gemini``         — Google Gemini ``generateContent`` API

The ``default_headers`` and ``extra_body`` keys are forwarded to the
backend. ``timeout``, ``temperature``, and ``max_retries`` are honored
by all backends.
"""

from __future__ import annotations

from typing import Any

from .base import LLMBackend

_KNOWN_PROVIDERS: tuple[str, ...] = (
    "ollama",
    "openai",
    "openai_compat",
    "azure_openai",
    "anthropic",
    "gemini",
)


def create_backend(config: dict[str, Any]) -> LLMBackend:
    """Construct an :class:`LLMBackend` from a plain config dict.

    Raises :class:`ValueError` for unknown providers.
    """
    provider = (config.get("provider") or "ollama").lower()

    timeout = int(config.get("timeout", 120) or 120)
    temperature = float(config.get("temperature", 0.1) or 0.1)
    max_retries = int(config.get("max_retries", 2) or 2)
    default_headers = dict(config.get("default_headers") or {})
    extra_body = dict(config.get("extra_body") or {})

    if provider == "ollama":
        from .ollama import OllamaLLMBackend

        return OllamaLLMBackend(
            model=config.get("model", "llama3"),
            base_url=config.get("base_url") or "http://localhost:11434",
            timeout=timeout,
            temperature=temperature,
            default_headers=default_headers,
            extra_body=extra_body,
            max_retries=max_retries,
        )

    if provider in ("openai", "openai_compat"):
        from .openai import OpenAILLMBackend

        if provider == "openai":
            base_url = config.get("base_url") or "https://api.openai.com"
        else:
            base_url = config.get("base_url") or ""
            if not base_url:
                raise ValueError(
                    "openai_compat requires a base_url (e.g. http://localhost:1234 for LM Studio)."
                )
        return OpenAILLMBackend(
            api_key=config.get("api_key", ""),
            model=config.get("model", "gpt-4o-mini"),
            base_url=base_url,
            timeout=timeout,
            temperature=temperature,
            default_headers=default_headers,
            extra_body=extra_body,
            max_retries=max_retries,
        )

    if provider == "azure_openai":
        from .azure_openai import AzureOpenAILLMBackend

        return AzureOpenAILLMBackend(
            api_key=config.get("api_key", ""),
            deployment=config.get("model", ""),
            azure_endpoint=config.get("base_url", ""),
            api_version=config.get("api_version", "2024-08-01-preview"),
            timeout=timeout,
            temperature=temperature,
            default_headers=default_headers,
            extra_body=extra_body,
            max_retries=max_retries,
        )

    if provider == "anthropic":
        from .anthropic import AnthropicLLMBackend

        return AnthropicLLMBackend(
            api_key=config.get("api_key", ""),
            model=config.get("model", "claude-3-5-sonnet-latest"),
            base_url=config.get("base_url") or "https://api.anthropic.com",
            timeout=timeout,
            temperature=temperature,
            anthropic_version=config.get("anthropic_version", "2023-06-01"),
            default_headers=default_headers,
            extra_body=extra_body,
            max_retries=max_retries,
        )

    if provider == "gemini":
        from .gemini import GeminiLLMBackend

        return GeminiLLMBackend(
            api_key=config.get("api_key", ""),
            model=config.get("model", "gemini-1.5-pro"),
            base_url=config.get("base_url") or "https://generativelanguage.googleapis.com",
            timeout=timeout,
            temperature=temperature,
            default_headers=default_headers,
            extra_body=extra_body,
            max_retries=max_retries,
            api_version=config.get("api_version", "v1beta"),
            auth_in_query=bool(config.get("auth_in_query", False)),
        )

    raise ValueError(
        f"Unknown LLM provider: {provider!r}. Use one of: {', '.join(_KNOWN_PROVIDERS)}."
    )
