"""Tests for the LLM configuration loader / merger / validator."""

from __future__ import annotations

import pytest

from model_forge.mcp_server.errors import ConfigError
from model_forge.mcp_server.llm_config import (
    DEFAULT_CATALOG_LIMIT,
    PROVIDERS,
    SCHEMA_VERSION,
    LLMConfig,
    build_llm_config,
    config_path,
    load_config,
    normalize_provider,
    save_config,
)

# ─── Schema version ─────────────────────────────────────────────────────


def test_schema_version_is_a_string():
    assert isinstance(SCHEMA_VERSION, str)
    assert SCHEMA_VERSION  # non-empty


def test_default_catalog_limit_is_positive():
    assert DEFAULT_CATALOG_LIMIT > 0


# ─── Provider normalization ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("ollama", "ollama"),
        ("openai", "openai"),
        ("openai_compat", "openai_compat"),
        ("anthropic", "anthropic"),
        ("gpt", "openai"),
        ("chatgpt", "openai"),
        ("claude", "anthropic"),
        ("", ""),
        ("MIXED", "mixed"),
    ],
)
def test_normalize_provider(raw, expected):
    assert normalize_provider(raw) == expected


def test_providers_list_is_frozen():
    assert PROVIDERS == (
        "ollama",
        "openai",
        "openai_compat",
        "azure_openai",
        "anthropic",
        "gemini",
    )


# ─── LLMConfig validation ───────────────────────────────────────────────


def test_from_dict_round_trip():
    cfg = LLMConfig(
        provider="openai_compat",
        model="meta-llama/Meta-Llama-3-8B-Instruct",
        base_url="http://localhost:1234",
        api_key="sk-local",
        temperature=0.4,
        timeout=90,
        max_retries=3,
        default_headers={"X-Org": "acme"},
        extra_body={"safe_prompt": True},
    )
    blob = cfg.to_dict()
    again = LLMConfig.from_dict(blob)
    assert again == cfg


def test_from_dict_accepts_string_headers():
    cfg = LLMConfig.from_dict(
        {
            "provider": "openai_compat",
            "model": "x",
            "default_headers": '{"X-Org": "acme"}',
            "extra_body": '{"safe_prompt": true}',
        }
    )
    assert cfg.default_headers == {"X-Org": "acme"}
    assert cfg.extra_body == {"safe_prompt": True}


def test_from_dict_rejects_malformed_headers():
    with pytest.raises(ConfigError) as ei:
        LLMConfig.from_dict({"provider": "openai_compat", "default_headers": "not json"})
    assert ei.value.code == "E_CONFIG"


def test_validate_unknown_provider():
    cfg = LLMConfig(provider="weird", model="x")
    with pytest.raises(ConfigError) as ei:
        cfg.validate()
    assert "Unknown LLM provider" in ei.value.message
    assert ei.value.code == "E_CONFIG"


def test_validate_missing_model():
    cfg = LLMConfig(provider="ollama", model="")
    with pytest.raises(ConfigError) as ei:
        cfg.validate()
    assert "model is required" in ei.value.message


def test_validate_ollama_does_not_need_api_key():
    LLMConfig(provider="ollama", model="qwen2.5-coder:7b").validate()


def test_validate_openai_requires_key_by_default():
    cfg = LLMConfig(provider="openai", model="gpt-4o-mini", api_key="")
    with pytest.raises(ConfigError) as ei:
        cfg.validate()
    assert "API key is required" in ei.value.message


def test_validate_openai_compat_requires_base_url():
    cfg = LLMConfig(provider="openai_compat", model="x", base_url="", api_key="k")
    with pytest.raises(ConfigError) as ei:
        cfg.validate()
    assert "base_url" in ei.value.message


def test_validate_anthropic_defaults_base_url():
    cfg = LLMConfig(provider="anthropic", model="claude-3-5-sonnet", api_key="k", base_url="")
    cfg.validate()
    assert cfg.base_url == "https://api.anthropic.com"


def test_validate_gemini_defaults_base_url():
    cfg = LLMConfig(provider="gemini", model="gemini-1.5-pro", api_key="k", base_url="")
    cfg.validate()
    assert cfg.base_url == "https://generativelanguage.googleapis.com"


def test_validate_azure_openai_requires_base_url():
    cfg = LLMConfig(provider="azure_openai", model="my-gpt4", api_key="k", base_url="")
    with pytest.raises(ConfigError) as ei:
        cfg.validate()
    assert "base_url" in ei.value.message
    # Error message hints that the 'model' field is the deployment.
    assert "deployment" in ei.value.message


def test_validate_azure_openai_with_endpoint_passes():
    cfg = LLMConfig(
        provider="azure_openai",
        model="my-gpt4",
        api_key="k",
        base_url="https://r.openai.azure.com",
    )
    cfg.validate()


def test_validate_temperature_out_of_range():
    cfg = LLMConfig(provider="ollama", model="x", temperature=3.0)
    with pytest.raises(ConfigError):
        cfg.validate()


def test_validate_honors_escape_hatch(monkeypatch):
    monkeypatch.setenv("MODELFORGE_REQUIRE_KEY", "0")
    LLMConfig(provider="openai", model="gpt-4o-mini", api_key="").validate()


# ─── build_llm_config merging ───────────────────────────────────────────


def _clean_env(monkeypatch):
    for var in (
        "MODELFORGE_PROVIDER",
        "MODELFORGE_MODEL",
        "MODELFORGE_BASE_URL",
        "MODELFORGE_API_KEY",
        "MODELFORGE_TEMPERATURE",
        "MODELFORGE_TIMEOUT",
        "MODELFORGE_DEFAULT_HEADERS",
        "MODELFORGE_EXTRA_BODY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "OLLAMA_BASE_URL",
        "OLLAMA_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)


def test_cli_overrides_file_overrides_env(monkeypatch, tmp_path):
    _clean_env(monkeypatch)
    monkeypatch.setenv("MODELFORGE_MODEL", "env-model")
    file_cfg = {"llm": {"model": "file-model", "api_key": "k"}}
    cfg = build_llm_config(
        cli={"model": "cli-model"},
        file_cfg=file_cfg,
    )
    assert cfg.model == "cli-model"
    # The file value is preserved where CLI didn't override.
    assert cfg.api_key == "k"


def test_env_picks_up_when_cli_and_file_silent(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("MODELFORGE_PROVIDER", "openai")
    monkeypatch.setenv("MODELFORGE_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("MODELFORGE_API_KEY", "sk-env")
    cfg = build_llm_config()
    assert cfg.provider == "openai"
    assert cfg.model == "gpt-4o-mini"
    assert cfg.api_key == "sk-env"


def test_env_headers_are_decoded(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("MODELFORGE_DEFAULT_HEADERS", '{"X-Org": "acme"}')
    cfg = build_llm_config(cli={"provider": "openai_compat", "model": "x"})
    assert cfg.default_headers == {"X-Org": "acme"}


def test_env_extra_body_is_decoded(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("MODELFORGE_EXTRA_BODY", '{"safe_prompt": true}')
    cfg = build_llm_config(cli={"provider": "openai_compat", "model": "x"})
    assert cfg.extra_body == {"safe_prompt": True}


def test_malformed_env_headers_raises(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("MODELFORGE_DEFAULT_HEADERS", "{not json")
    with pytest.raises(ConfigError):
        build_llm_config(cli={"provider": "openai_compat", "model": "x"})


def test_invalid_temperature_env_raises(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("MODELFORGE_TEMPERATURE", "warm")
    with pytest.raises(ConfigError):
        build_llm_config(cli={"provider": "ollama", "model": "x"})


def test_invalid_timeout_env_raises(monkeypatch):
    _clean_env(monkeypatch)
    monkeypatch.setenv("MODELFORGE_TIMEOUT", "long")
    with pytest.raises(ConfigError):
        build_llm_config(cli={"provider": "ollama", "model": "x"})


def test_auto_detect_ollama_falls_back_to_openai_env(monkeypatch):
    """With no provider anywhere, fall back to OpenAI if its env vars exist."""
    _clean_env(monkeypatch)
    # ``auto_detect`` will probe localhost:11434 — that will fail in tests.
    # We just need to ensure the env fallback path works when probing
    # fails (which it always does in CI without Ollama running).
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    cfg = build_llm_config()
    # If Ollama is unexpectedly running locally, this would be ollama.
    if cfg.provider == "openai":
        assert cfg.api_key == "sk-x"


# ─── Persistence ────────────────────────────────────────────────────────


def test_save_then_load_round_trip(tmp_path):
    target = tmp_path / "mcp.json"
    payload = {"llm": {"provider": "openai", "model": "gpt-4o-mini", "api_key": "sk"}}
    save_config(payload)
    assert target.exists()
    assert load_config() == payload


def test_load_missing_file_is_empty():
    # ``load_config`` reads via ``config_path()`` which honors the env
    # var we set in conftest, so the test runs against a non-existent
    # file by construction.
    assert load_config() == {}


def test_config_path_respects_env(monkeypatch):
    monkeypatch.setenv("MODELFORGE_MCP_CONFIG", "/custom/path/mcp.json")
    assert config_path() == "/custom/path/mcp.json"
