"""
OpenAILLMBackend - calls OpenAI chat completions API.
"""
from __future__ import annotations
import json
import urllib.request
from .base import LLMBackend


class OpenAILLMBackend(LLMBackend):
    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        base_url: str = "https://api.openai.com",
        timeout: int = 120,
        temperature: float = 0.1,
    ):
        self.api_key     = api_key
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
            "temperature": self.temperature,
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=payload,
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"]
