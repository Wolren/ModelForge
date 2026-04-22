"""
LLMBackendFactory - creates backends from a config dict.
"""
from __future__ import annotations
from typing import Any, Dict
from .base import LLMBackend


def create_backend(config: Dict[str, Any]) -> LLMBackend:
    """
    config dict shape (stored in QSettings):
    {
        "provider": "ollama" | "openai",
        "model":    "<model name>",
        "api_key":  "<key, openai only>",
        "base_url": "<optional override>",
        "temperature": 0.1,
    }
    """
    provider = config.get("provider", "ollama").lower()

    if provider == "ollama":
        from .ollama import OllamaLLMBackend
        return OllamaLLMBackend(
            model=config.get("model", "llama3"),
            base_url=config.get("base_url", "http://localhost:11434"),
            timeout=int(config.get("timeout", 240)),
            temperature=config.get("temperature", 0.1),
        )

    if provider == "openai":
        from .openai import OpenAILLMBackend
        return OpenAILLMBackend(
            api_key=config.get("api_key", ""),
            model=config.get("model", "gpt-4o-mini"),
            base_url=config.get("base_url", "https://api.openai.com"),
            timeout=int(config.get("timeout", 180)),
            temperature=config.get("temperature", 0.1),
        )

    raise ValueError(f"Unknown LLM provider: {provider!r}. Use 'ollama' or 'openai'.")
