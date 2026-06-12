"""
AnthropicLLMBackend - calls the Anthropic Messages API.

Uses urllib only so the project keeps zero hard dependencies for the
LLM layer. Compatible with the Messages API
(``/v1/messages``) as of API version 2023-06-01.

The Messages API has a few structural differences from the OpenAI
Chat Completions API:

* The system prompt lives in the top-level ``system`` field, not in
  the messages array.
* The model string does not include a provider prefix.
* The response carries content in ``content[0].text`` (we ignore tool
  use / images and just return the first text block).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from .base import LLMBackend, LLMRequestError, LLMResponseError, LLMTimeoutError


class AnthropicLLMBackend(LLMBackend):
    def __init__(
        self,
        api_key: str,
        model: str = "claude-3-5-sonnet-latest",
        base_url: str = "https://api.anthropic.com",
        timeout: int = 120,
        temperature: float = 0.1,
        *,
        anthropic_version: str = "2023-06-01",
        default_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
        max_retries: int = 2,
    ):
        if not api_key:
            raise ValueError("AnthropicLLMBackend requires an api_key")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.temperature = temperature
        self.anthropic_version = anthropic_version
        self.default_headers: dict[str, str] = dict(default_headers or {})
        self.extra_body: dict[str, Any] = dict(extra_body or {})
        self.max_retries = max(0, int(max_retries))

    def _build_headers(self) -> dict[str, str]:
        # Anthropic-specific headers; the ``x-api-key`` is the auth header
        # and ``anthropic-version`` pins the API contract. We refuse to
        # let ``default_headers`` shadow these — caller-supplied keys
        # are merged in first, then the security headers are re-pinned
        # on top.
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            **self.default_headers,
        }
        headers["x-api-key"] = self.api_key
        headers["anthropic-version"] = self.anthropic_version
        return headers

    def _build_payload(self, system_prompt: str, user_message: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "system": system_prompt,
            "messages": [
                {"role": "user", "content": user_message},
            ],
            "max_tokens": 4096,
            "temperature": self.temperature,
        }
        if self.extra_body:
            payload.update(self.extra_body)
        return payload

    def chat(self, system_prompt: str, user_message: str) -> str:
        payload = json.dumps(self._build_payload(system_prompt, user_message)).encode("utf-8")
        url = f"{self.base_url}/v1/messages"
        last_exc: BaseException | None = None

        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(
                url,
                data=payload,
                headers=self._build_headers(),
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read())
            except TimeoutError as e:
                last_exc = LLMTimeoutError(f"Anthropic request timed out after {self.timeout}s.")
                if attempt < self.max_retries:
                    time.sleep(min(0.5 * (2**attempt), 2.0))
                    continue
                raise last_exc from e
            except urllib.error.HTTPError as e:
                if e.code in (408, 425, 429) or 500 <= e.code < 600:
                    last_exc = LLMRequestError(f"Anthropic HTTP {e.code}: {e.reason}")
                    if attempt < self.max_retries:
                        time.sleep(min(0.5 * (2**attempt), 2.0))
                        continue
                raise LLMRequestError(f"Anthropic HTTP {e.code}: {e.reason}") from e
            except urllib.error.URLError as e:
                raise LLMRequestError(
                    f"Could not reach Anthropic endpoint at {self.base_url}."
                ) from e
            except json.JSONDecodeError as e:
                raise LLMResponseError(f"Anthropic returned invalid JSON: {e}") from e
            else:
                try:
                    content = data.get("content") or []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            return str(block.get("text") or "")
                    raise LLMResponseError("Anthropic response contained no text block.")
                except LLMResponseError:
                    raise
                except Exception as e:  # noqa: BLE001
                    raise LLMResponseError("Anthropic response missing expected content.") from e

        raise LLMRequestError(
            f"Anthropic request failed after {self.max_retries + 1} attempts",
        ) from last_exc
