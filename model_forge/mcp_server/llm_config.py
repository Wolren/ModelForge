"""LLM configuration: load, merge, validate.

The effective config is built from four sources in priority order:

1. Explicit CLI / programmatic arguments
2. The persisted file at ``$MODELFORGE_MCP_CONFIG`` (default
   ``~/.config/model-forge/mcp.json``)
3. Environment variables (``MODELFORGE_PROVIDER`` etc.)
4. Auto-detection (probe Ollama; fall back to OpenAI env vars)

The merge is *per-field*, not per-block, so a CLI override of just the
``model`` still inherits ``base_url`` from the file or env.

Providers supported:

* ``ollama`` — local Ollama server (no API key)
* ``openai`` — OpenAI Chat Completions
* ``openai_compat`` — any OpenAI-compatible endpoint (LM Studio,
  vLLM, OpenRouter, Azure OpenAI, etc.) with optional custom headers
* ``anthropic`` — Anthropic Messages API (optional ``anthropic`` package)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .errors import ConfigError, ProviderError

# Schema version for the MCP surface. Bump when the tool/resource shape
# changes in a way clients must be aware of.
SCHEMA_VERSION = "1.0"

# Provider names accepted by the factory. ``openai_compat`` is the
# canonical name for "any OpenAI-compatible HTTP endpoint" — useful for
# vLLM, LM Studio, OpenRouter, llama.cpp's server, etc. ``openai`` is
# kept as a shortcut for the public OpenAI service.
PROVIDERS: tuple[str, ...] = (
    "ollama",
    "openai",
    "openai_compat",
    "azure_openai",
    "anthropic",
    "gemini",
)

# Catalog cap. Override at runtime with MODELFORGE_MCP_CATALOG_LIMIT.
DEFAULT_CATALOG_LIMIT = 1000


@dataclass
class LLMConfig:
    """Normalized, validated LLM configuration.

    Stored in ``ServerState.llm_config`` and passed to the compiler
    backend factory. Field names match the legacy dict shape so existing
    backends don't need to change.
    """

    provider: str = ""
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    temperature: float = 0.2
    timeout: float = 120.0
    max_retries: int = 2
    default_headers: dict[str, str] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)
    anthropic_version: str = "2023-06-01"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "temperature": self.temperature,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "default_headers": dict(self.default_headers),
            "extra_body": dict(self.extra_body),
            "anthropic_version": self.anthropic_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> LLMConfig:
        data = dict(data or {})
        headers = data.get("default_headers") or {}
        if isinstance(headers, str):
            try:
                headers = json.loads(headers)
            except json.JSONDecodeError as e:
                raise ConfigError(
                    "default_headers must be a JSON object",
                    details={"value": headers, "error": str(e)},
                ) from e
        if not isinstance(headers, dict):
            raise ConfigError(
                "default_headers must be a JSON object",
                details={"value_type": type(headers).__name__},
            )
        extra = data.get("extra_body") or {}
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except json.JSONDecodeError as e:
                raise ConfigError(
                    "extra_body must be a JSON object",
                    details={"value": extra, "error": str(e)},
                ) from e
        if not isinstance(extra, dict):
            raise ConfigError(
                "extra_body must be a JSON object",
                details={"value_type": type(extra).__name__},
            )
        return cls(
            provider=str(data.get("provider", "") or ""),
            model=str(data.get("model", "") or ""),
            base_url=str(data.get("base_url", "") or ""),
            api_key=str(data.get("api_key", "") or ""),
            temperature=float(data.get("temperature", 0.2) or 0.2),
            timeout=float(data.get("timeout", 120) or 120),
            max_retries=int(data.get("max_retries", 2) or 2),
            default_headers={str(k): str(v) for k, v in headers.items()},
            extra_body=dict(extra),
            anthropic_version=str(data.get("anthropic_version", "2023-06-01")),
        )

    def validate(self, *, require_key: bool = True) -> None:
        """Raise :class:`ConfigError` if the config is not usable.

        ``require_key`` is False only for ``ollama`` and for the
        ``MODELFORGE_REQUIRE_KEY=0`` escape hatch.
        """
        provider = (self.provider or "").lower()
        if provider not in PROVIDERS:
            raise ConfigError(
                f"Unknown LLM provider {provider!r}. Use one of: {', '.join(PROVIDERS)}.",
                details={"provider": provider, "allowed": list(PROVIDERS)},
            )
        if not self.model:
            raise ConfigError("LLM model is required.", details={"provider": provider})

        # Generic numeric checks run for every provider — they don't
        # depend on connectivity.
        if self.temperature < 0 or self.temperature > 2:
            raise ConfigError(
                "temperature must be between 0 and 2",
                details={"temperature": self.temperature},
            )
        if self.timeout <= 0:
            raise ConfigError("timeout must be > 0", details={"timeout": self.timeout})

        if provider == "ollama":
            return
        if provider == "anthropic" and not self.base_url:
            self.base_url = "https://api.anthropic.com"
        if provider == "gemini" and not self.base_url:
            self.base_url = "https://generativelanguage.googleapis.com"
        if provider in ("openai", "openai_compat") and not self.base_url:
            if provider == "openai":
                self.base_url = "https://api.openai.com"
            else:
                raise ConfigError(
                    "openai_compat requires a base_url "
                    "(e.g. http://localhost:1234 for LM Studio, "
                    "https://openrouter.ai/api for OpenRouter, etc.).",
                    details={"provider": provider},
                )
        if provider == "azure_openai" and not self.base_url:
            raise ConfigError(
                "azure_openai requires a base_url pointing to the Azure "
                "OpenAI resource, e.g. https://<resource>.openai.azure.com. "
                "The 'model' field is treated as the Azure deployment name.",
                details={"provider": provider},
            )
        if require_key and not self.api_key:
            allow_empty = os.environ.get("MODELFORGE_REQUIRE_KEY", "1") == "0"
            if not allow_empty:
                raise ConfigError(
                    f"API key is required for provider {provider!r}. "
                    "Set api_key, MODELFORGE_API_KEY, or MODELFORGE_REQUIRE_KEY=0 "
                    "to skip (for self-hosted / local-only deployments).",
                    details={"provider": provider},
                )


# ─── Persistence ────────────────────────────────────────────────────────


def config_path() -> str:
    """Return the on-disk config path. Override with ``MODELFORGE_MCP_CONFIG``."""
    return os.environ.get(
        "MODELFORGE_MCP_CONFIG",
        os.path.join(os.path.expanduser("~"), ".config", "model-forge", "mcp.json"),
    )


def load_config() -> dict[str, Any]:
    """Read the persisted config file. Missing file is not an error."""
    import logging

    log = logging.getLogger(__name__)
    path = config_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to read config %s: %s", path, e)
        return {}


def save_config(cfg: dict[str, Any]) -> None:
    """Persist the config to disk. Creates parent dirs as needed."""
    import logging

    log = logging.getLogger(__name__)
    path = config_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
    log.info("Saved config to %s", path)


# ─── Auto-detection ─────────────────────────────────────────────────────


def auto_detect() -> dict[str, Any]:
    """Probe the local environment for an LLM backend.

    Order: Ollama (HTTP probe) → OpenAI-compatible env vars.
    Returns an empty dict if nothing was detected.
    """
    detected: dict[str, Any] = {}

    base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    try:
        req = urllib.request.Request(f"{base.rstrip('/')}/api/tags")
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                detected["provider"] = "ollama"
                detected["base_url"] = base
                detected["model"] = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b")
    except (urllib.error.URLError, TimeoutError, OSError):
        pass

    if not detected:
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("MODELFORGE_API_KEY")
        if api_key:
            detected["provider"] = "openai"
            detected["api_key"] = api_key
            detected["base_url"] = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com")
            detected["model"] = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

    return detected


# ─── Merging ────────────────────────────────────────────────────────────


def _coerce_headers(value: Any) -> dict[str, str]:
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    if isinstance(value, str):
        try:
            data = json.loads(value)
        except json.JSONDecodeError as e:
            raise ConfigError(
                "default_headers must be a JSON object or dict",
                details={"value": value, "error": str(e)},
            ) from e
        if not isinstance(data, dict):
            raise ConfigError(
                "default_headers JSON must decode to an object",
                details={"value_type": type(data).__name__},
            )
        return {str(k): str(v) for k, v in data.items()}
    raise ConfigError(
        "default_headers has unsupported type",
        details={"value_type": type(value).__name__},
    )


def _coerce_extra(value: Any) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            data = json.loads(value)
        except json.JSONDecodeError as e:
            raise ConfigError(
                "extra_body must be a JSON object or dict",
                details={"value": value, "error": str(e)},
            ) from e
        if not isinstance(data, dict):
            raise ConfigError(
                "extra_body JSON must decode to an object",
                details={"value_type": type(data).__name__},
            )
        return dict(data)
    raise ConfigError(
        "extra_body has unsupported type",
        details={"value_type": type(value).__name__},
    )


def build_llm_config(
    *,
    cli: dict[str, Any] | None = None,
    file_cfg: dict[str, Any] | None = None,
) -> LLMConfig:
    """Merge CLI > file > env > auto-detect into a validated ``LLMConfig``.

    ``cli`` is the dict of explicit overrides (typically from argparse).
    ``file_cfg`` is the parsed ``mcp.json``. Both are optional; the
    remaining layers are read from the environment.

    The merge is *per-field*: a CLI override of just ``model`` still
    inherits ``base_url`` from the file or env. Env values only fill
    in fields that the higher-priority layers left empty.
    """
    cli = dict(cli or {})
    file_llm = dict((file_cfg or {}).get("llm") or {})

    # Start with auto-detected defaults (if any), then layer file, then
    # env, then CLI on top.
    base: dict[str, Any] = {}
    explicit_provider = (
        cli.get("provider")
        or file_llm.get("provider")
        or os.environ.get("MODELFORGE_PROVIDER", "").strip()
    )
    if not explicit_provider:
        base.update(auto_detect())
    base.update(file_llm)

    # Env fills in fields the layers above didn't set.
    env_provider = os.environ.get("MODELFORGE_PROVIDER", "").strip()
    env_model = os.environ.get("MODELFORGE_MODEL", "").strip()
    env_base = os.environ.get("MODELFORGE_BASE_URL", "").strip()
    env_key = os.environ.get("MODELFORGE_API_KEY", "").strip()
    env_temp_raw = os.environ.get("MODELFORGE_TEMPERATURE", "").strip()
    env_timeout_raw = os.environ.get("MODELFORGE_TIMEOUT", "").strip()

    if env_provider:
        base["provider"] = env_provider
    if env_model:
        base["model"] = env_model
    if env_base:
        base["base_url"] = env_base
    if env_key:
        base["api_key"] = env_key
    if env_temp_raw:
        try:
            base["temperature"] = float(env_temp_raw)
        except ValueError as e:
            raise ConfigError(
                "MODELFORGE_TEMPERATURE must be a float",
                details={"value": env_temp_raw},
            ) from e
    if env_timeout_raw:
        try:
            base["timeout"] = float(env_timeout_raw)
        except ValueError as e:
            raise ConfigError(
                "MODELFORGE_TIMEOUT must be a number",
                details={"value": env_timeout_raw},
            ) from e

    env_headers = _coerce_headers(os.environ.get("MODELFORGE_DEFAULT_HEADERS", ""))
    if env_headers:
        merged_headers = {**env_headers, **base.get("default_headers", {})}
        base["default_headers"] = merged_headers
    env_extra = _coerce_extra(os.environ.get("MODELFORGE_EXTRA_BODY", ""))
    if env_extra:
        merged_extra = {**env_extra, **base.get("extra_body", {})}
        base["extra_body"] = merged_extra

    # CLI wins last, but only for fields the caller actually provided
    # (filters out the argparse defaults like ``None`` / empty string).
    for key, value in cli.items():
        if value not in (None, ""):
            base[key] = value

    return LLMConfig.from_dict(base)


def normalize_provider(provider: str) -> str:
    """Map legacy / short names onto the canonical set.

    ``gpt`` / ``openai`` → ``openai``; ``claude`` → ``anthropic``;
    anything unknown passes through and will fail validation later.
    """
    p = (provider or "").strip().lower()
    aliases = {
        "gpt": "openai",
        "chatgpt": "openai",
        "claude": "anthropic",
    }
    return aliases.get(p, p)


def parse_provider_error(provider: str, exc: BaseException) -> ProviderError:
    """Wrap a provider-specific exception into a structured error."""
    return ProviderError(
        f"{provider} request failed: {exc}",
        details={
            "provider": provider,
            "exception_type": type(exc).__name__,
            "exception": str(exc),
        },
    )
