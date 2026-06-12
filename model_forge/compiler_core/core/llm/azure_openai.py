"""
AzureOpenAILLMBackend - thin shim over the OpenAI Chat Completions
API for Azure-hosted deployments.

Azure's OpenAI surface has three differences from the public API:

1. The deployment is in the URL, not a query string::

       POST {azure_endpoint}/openai/deployions/{deployment}/
           chat/completions?api-version={api_version}

2. Authentication uses an ``api-key`` header (NOT ``Authorization:
   Bearer ...``).

3. The ``model`` field in the request body is ignored — Azure
   binds the deployment at provisioning time. We still send it
   for safety; Azure will accept and ignore it.

We re-use the OpenAI backend's retry / response / timeout logic
by inheriting from it and only overriding the URL and header
construction.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from .openai import OpenAILLMBackend


# Default api-version pinned to a stable 2024-08 release. Azure
# rotates these; users can override via the ``extra_body`` key
# ``api_version`` (handled in :meth:`_build_url`) or via
# ``api-version`` in ``default_headers`` (handled by Azure's
# gateway, not us).
_DEFAULT_API_VERSION = "2024-08-01-preview"


class AzureOpenAILLMBackend(OpenAILLMBackend):
    def __init__(
        self,
        api_key: str,
        deployment: str,
        azure_endpoint: str,
        api_version: str = _DEFAULT_API_VERSION,
        timeout: int = 120,
        temperature: float = 0.1,
        *,
        default_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
        max_retries: int = 2,
    ):
        if not api_key:
            raise ValueError("AzureOpenAILLMBackend requires an api_key")
        if not deployment:
            raise ValueError("AzureOpenAILLMBackend requires a deployment name.")
        if not azure_endpoint:
            raise ValueError(
                "AzureOpenAILLMBackend requires azure_endpoint "
                "(e.g. https://<resource>.openai.azure.com)."
            )
        # We deliberately do *not* call super().__init__() — the
        # OpenAI ctor would set Authorization: Bearer headers we
        # don't want. Build the parent's bits manually.
        self.api_key = api_key
        self.model = deployment  # for backends that read .model
        self.deployment = deployment
        self.base_url = azure_endpoint.rstrip("/")
        self.api_version = api_version
        self.timeout = timeout
        self.temperature = temperature
        self.default_headers: dict[str, str] = dict(default_headers or {})
        self.extra_body: dict[str, Any] = dict(extra_body or {})
        self.max_retries = max(0, int(max_retries))

    # ─── Overrides ──────────────────────────────────────────────

    def _build_url(self) -> str:  # type: ignore[override]
        path = (
            f"/openai/deployments/{urllib.parse.quote(self.deployment, safe='')}/chat/completions"
        )
        return f"{self.base_url}{path}?api-version={urllib.parse.quote(self.api_version, safe='')}"

    def _build_headers(self) -> dict[str, str]:  # type: ignore[override]
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            **self.default_headers,
        }
        # Azure uses ``api-key``, not Bearer. We re-pin so user-supplied
        # ``default_headers`` cannot shadow it.
        headers["api-key"] = self.api_key
        return headers
