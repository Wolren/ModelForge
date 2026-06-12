"""
OpenAILLMBackend - calls OpenAI chat completions API.

Also works for any OpenAI-compatible HTTP endpoint (vLLM, LM Studio,
OpenRouter, llama.cpp server, etc.) by overriding ``base_url``. Custom
``default_headers`` are merged on top of the standard ``Authorization``
and ``Content-Type`` headers; ``extra_body`` is merged into the JSON
payload (useful for provider-specific fields like ``reasoning_effort``
or ``safe_prompt``).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .base import LLMBackend, LLMRequestError, LLMResponseError, LLMTimeoutError


class OpenAILLMBackend(LLMBackend):
    def __init__(
        self,
        api_key: str = "",
        model: str = "gpt-4o-mini",
        base_url: str = "https://api.openai.com",
        timeout: int = 120,
        temperature: float = 0.1,
        *,
        default_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
        max_retries: int = 2,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.temperature = temperature
        self.default_headers: dict[str, str] = dict(default_headers or {})
        self.extra_body: dict[str, Any] = dict(extra_body or {})
        self.max_retries = max(0, int(max_retries))

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        # Custom headers win over defaults (except Authorization/Content-Type
        # which we keep on top so the request still validates).
        merged = {**self.default_headers, **headers}
        # Re-pin the security headers last.
        merged["Content-Type"] = headers["Content-Type"]
        if self.api_key:
            merged["Authorization"] = headers["Authorization"]
        return merged

    def _build_url(self) -> str:
        return f"{self.base_url}/v1/chat/completions"

    def _build_payload(self, system_prompt: str, user_message: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": self.temperature,
        }
        if self.extra_body:
            payload.update(self.extra_body)
        return payload

    def chat(self, system_prompt: str, user_message: str) -> str:
        import time

        payload = json.dumps(self._build_payload(system_prompt, user_message)).encode("utf-8")
        url = self._build_url()
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
                last_exc = LLMTimeoutError(f"OpenAI request timed out after {self.timeout}s.")
                # Timeouts are usually a server-scaling problem; back off
                # only briefly, no point retrying many times.
                if attempt < self.max_retries:
                    time.sleep(min(0.5 * (2**attempt), 2.0))
                    continue
                raise last_exc from e
            except urllib.error.HTTPError as e:
                # Retry only on 429 / 5xx. 4xx other than 429 are fatal.
                if e.code in (408, 425, 429) or 500 <= e.code < 600:
                    last_exc = LLMRequestError(f"OpenAI HTTP {e.code}: {e.reason}")
                    if attempt < self.max_retries:
                        time.sleep(min(0.5 * (2**attempt), 2.0))
                        continue
                raise LLMRequestError(f"OpenAI HTTP {e.code}: {e.reason}") from e
            except urllib.error.URLError as e:
                raise LLMRequestError(f"Could not reach OpenAI endpoint at {self.base_url}.") from e
            except json.JSONDecodeError as e:
                raise LLMResponseError(f"OpenAI returned invalid JSON: {e}") from e
            else:
                try:
                    return data["choices"][0]["message"]["content"]
                except Exception as e:  # noqa: BLE001
                    raise LLMResponseError(
                        "OpenAI response missing expected message content."
                    ) from e

        # Unreachable: the loop either returns or raises.
        raise LLMRequestError(
            f"OpenAI request failed after {self.max_retries + 1} attempts",
            # type: ignore[union-attr]
        ) from last_exc
