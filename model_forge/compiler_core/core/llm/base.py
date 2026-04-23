"""
Abstract LLM backend interface.
"""
from abc import ABC, abstractmethod


class LLMBackendError(RuntimeError):
    """Base class for LLM backend failures."""


class LLMTimeoutError(LLMBackendError):
    """Raised when a backend request times out."""


class LLMRequestError(LLMBackendError):
    """Raised when a backend request/transport fails."""


class LLMResponseError(LLMBackendError):
    """Raised when backend response payload is invalid."""


class LLMBackend(ABC):
    @abstractmethod
    def chat(self, system_prompt: str, user_message: str) -> str:
        """Send a system+user message pair, return the raw text response."""
