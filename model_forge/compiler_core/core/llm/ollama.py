"""
OllamaLLMBackend - calls a local Ollama server.
Default endpoint: http://localhost:11434

Supports custom ``default_headers`` (for proxied deployments) and
``extra_body`` keys (Ollama accepts ``keep_alive``, ``num_ctx``,
``num_predict``, ``stop``, and a few others inside ``options`` — we
forward any caller-supplied ``extra_body`` keys onto the top-level
payload, but the legacy ``options`` field is preserved).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .base import LLMBackend, LLMRequestError, LLMResponseError, LLMTimeoutError


class OllamaLLMBackend(LLMBackend):
    def __init__(
        self,
        model: str = "llama3",
        base_url: str = "http://localhost:11434",
        timeout: int = 120,
        temperature: float = 0.1,
        *,
        default_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
        max_retries: int = 2,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.temperature = temperature
        self.default_headers: dict[str, str] = dict(default_headers or {})
        self.extra_body: dict[str, Any] = dict(extra_body or {})
        self.max_retries = max(0, int(max_retries))

    def _build_payload(self, system_prompt: str, user_message: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "options": {"temperature": self.temperature},
            "stream": False,
        }
        if self.extra_body:
            # Caller-supplied keys may live anywhere in the payload; merge
            # them at the top level. ``options`` is preserved unless the
            # caller explicitly overrides it via ``extra_body['options']``.
            for key, value in self.extra_body.items():
                if key == "options" and isinstance(value, dict):
                    payload["options"] = {**payload["options"], **value}
                else:
                    payload[key] = value
        return payload

    def chat(self, system_prompt: str, user_message: str) -> str:
        import time

        payload = json.dumps(self._build_payload(system_prompt, user_message)).encode("utf-8")
        url = f"{self.base_url}/api/chat"
        headers = {"Content-Type": "application/json", **self.default_headers}
        last_exc: BaseException | None = None

        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(
                url,
                data=payload,
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read())
            except TimeoutError as e:
                last_exc = LLMTimeoutError(
                    f"Ollama request timed out after {self.timeout}s. "
                    f"Consider increasing timeout or simplifying the prompt."
                )
                if attempt < self.max_retries:
                    time.sleep(min(0.5 * (2**attempt), 2.0))
                    continue
                raise last_exc from e
            except urllib.error.HTTPError as e:
                if e.code in (408, 425, 429) or 500 <= e.code < 600:
                    last_exc = LLMRequestError(f"Ollama HTTP {e.code}: {e.reason}")
                    if attempt < self.max_retries:
                        time.sleep(min(0.5 * (2**attempt), 2.0))
                        continue
                raise LLMRequestError(f"Ollama HTTP {e.code}: {e.reason}") from e
            except urllib.error.URLError as e:
                raise LLMRequestError(
                    f"Could not reach Ollama at {self.base_url}. Check that the server is running."
                ) from e
            except json.JSONDecodeError as e:
                raise LLMResponseError(f"Ollama returned invalid JSON: {e}") from e
            else:
                try:
                    return data["message"]["content"]
                except Exception as e:  # noqa: BLE001
                    raise LLMResponseError(
                        "Ollama response missing expected message content."
                    ) from e

        raise LLMRequestError(
            f"Ollama request failed after {self.max_retries + 1} attempts",
        ) from last_exc
