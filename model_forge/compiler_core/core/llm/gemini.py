"""
GeminiLLMBackend - calls the Google Gemini ``generateContent`` API.

Endpoint shape (as of API version ``v1beta``):
    POST {base_url}/v1beta/models/{model}:generateContent
    ?key={api_key}                 (or ``x-goog-api-key`` header)
    Headers: Content-Type: application/json
    Body:    { "system_instruction": {...},
              "contents": [{"role": "user", "parts": [{"text": "..."}]}],
              "generationConfig": {"temperature": 0.2, ...} }
    Response: { "candidates": [{"content": {"parts": [{"text": "..."}]}] }

Notable differences from OpenAI:
- No role-prefixed messages array; the system prompt is a
  sibling field, not a message.
- Multiple ``parts`` in a single content block — for our
  text-only use case we only ever emit one.
- Multiple candidates — we take the first.
- The API key is normally a query string parameter
  (``?key=...``). The header form is preferred in newer
  clients and is what we use by default.

We do not override Gemini's safety settings. The defaults
(block_none) are permissive enough for technical model-design
prompts; if a deployment has tighter org-level policies those
will still apply.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .base import LLMBackend, LLMRequestError, LLMResponseError, LLMTimeoutError


class GeminiLLMBackend(LLMBackend):
    def __init__(
        self,
        api_key: str,
        model: str = "gemini-1.5-pro",
        base_url: str = "https://generativelanguage.googleapis.com",
        timeout: int = 120,
        temperature: float = 0.1,
        *,
        default_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
        max_retries: int = 2,
        api_version: str = "v1beta",
        auth_in_query: bool = False,
    ):
        if not api_key:
            raise ValueError("GeminiLLMBackend requires an api_key")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.temperature = temperature
        self.default_headers: dict[str, str] = dict(default_headers or {})
        self.extra_body: dict[str, Any] = dict(extra_body or {})
        self.max_retries = max(0, int(max_retries))
        self.api_version = api_version.strip("/")
        # ``auth_in_query`` is kept for users whose network blocks the
        # ``x-goog-api-key`` header (some proxies strip custom
        # headers). The query-string form is the API's documented
        # default; the header form is the recommended best practice.
        self.auth_in_query = auth_in_query

    def _build_url(self) -> str:
        path = (
            f"/{self.api_version}/models/{urllib.parse.quote(self.model, safe='')}:generateContent"
        )
        if self.auth_in_query:
            return f"{self.base_url}{path}?key={urllib.parse.quote(self.api_key, safe='')}"
        return f"{self.base_url}{path}"

    def _build_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            **self.default_headers,
        }
        if not self.auth_in_query:
            headers["x-goog-api-key"] = self.api_key
        return headers

    def _build_payload(self, system_prompt: str, user_message: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "system_instruction": {
                "parts": [{"text": system_prompt}],
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": user_message}],
                }
            ],
            "generationConfig": {
                "temperature": self.temperature,
            },
        }
        if self.extra_body:
            for key, value in self.extra_body.items():
                if key == "generationConfig" and isinstance(value, dict):
                    payload["generationConfig"].update(value)
                else:
                    payload[key] = value
        return payload

    def chat(self, system_prompt: str, user_message: str) -> str:
        payload = json.dumps(self._build_payload(system_prompt, user_message)).encode("utf-8")
        url = self._build_url()
        headers = self._build_headers()
        last_exc: BaseException | None = None

        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read())
            except TimeoutError as e:
                last_exc = LLMTimeoutError(f"Gemini request timed out after {self.timeout}s.")
                if attempt < self.max_retries:
                    time.sleep(min(0.5 * (2**attempt), 2.0))
                    continue
                raise last_exc from e
            except urllib.error.HTTPError as e:
                if e.code in (408, 425, 429) or 500 <= e.code < 600:
                    last_exc = LLMRequestError(f"Gemini HTTP {e.code}: {e.reason}")
                    if attempt < self.max_retries:
                        time.sleep(min(0.5 * (2**attempt), 2.0))
                        continue
                raise LLMRequestError(f"Gemini HTTP {e.code}: {e.reason}") from e
            except urllib.error.URLError as e:
                raise LLMRequestError(f"Could not reach Gemini endpoint at {self.base_url}.") from e
            except json.JSONDecodeError as e:
                raise LLMResponseError(f"Gemini returned invalid JSON: {e}") from e
            else:
                try:
                    candidates = data.get("candidates") or []
                    if not candidates:
                        raise LLMResponseError(
                            "Gemini response contained no candidates.",
                        )
                    content = candidates[0].get("content") or {}
                    parts = content.get("parts") or []
                    for part in parts:
                        if isinstance(part, dict) and part.get("text"):
                            return str(part["text"])
                    raise LLMResponseError("Gemini response contained no text part.")
                except LLMResponseError:
                    raise
                except Exception as e:  # noqa: BLE001
                    raise LLMResponseError(f"Gemini response missing expected content: {e}") from e

        raise LLMRequestError(
            f"Gemini request failed after {self.max_retries + 1} attempts",
        ) from last_exc
