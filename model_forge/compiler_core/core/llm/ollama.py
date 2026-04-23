"""
OllamaLLMBackend - calls a local Ollama server.
Default endpoint: http://localhost:11434
"""
from __future__ import annotations
import json
import socket
import urllib.error
import urllib.request
from typing import Optional
from .base import LLMBackend, LLMTimeoutError, LLMRequestError, LLMResponseError


class OllamaLLMBackend(LLMBackend):
    def __init__(
        self,
        model: str = "llama3",
        base_url: str = "http://localhost:11434",
        timeout: int = 120,
        temperature: float = 0.1,
    ):
        self.model       = model
        self.base_url    = base_url.rstrip("/")
        self.timeout     = timeout
        self.temperature = temperature

    def chat(self, system_prompt: str, user_message: str) -> str:
        payload = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system",  "content": system_prompt},
                {"role": "user",    "content": user_message},
            ],
            "options": {"temperature": self.temperature},
            "stream": False,
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
        except (TimeoutError, socket.timeout) as e:
            raise LLMTimeoutError(
                f"Ollama request timed out after {self.timeout}s. "
                f"Consider increasing timeout or simplifying the prompt."
            ) from e
        except urllib.error.URLError as e:
            raise LLMRequestError(
                f"Could not reach Ollama at {self.base_url}. Check that the server is running."
            ) from e
        except json.JSONDecodeError as e:
            raise LLMResponseError(f"Ollama returned invalid JSON: {e}") from e

        try:
            return data["message"]["content"]
        except Exception as e:
            raise LLMResponseError("Ollama response missing expected message content.") from e
