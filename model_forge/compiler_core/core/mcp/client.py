"""
DirectMCPClient - in-process MCP client with timeout + retry.
"""

from __future__ import annotations

import threading
from typing import Any

from .server import ModelForgeMCPServer


class DirectMCPClient:
    def __init__(self, server: ModelForgeMCPServer, timeout: float = 120.0):
        self._server = server
        self._timeout = timeout

    def call(
        self,
        tool_name: str,
        args: dict[str, Any],
        max_retries: int = 2,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("Cancelled before call.")

        last_exc: Exception | None = None
        for attempt in range(1, max_retries + 1):
            if cancel_event and cancel_event.is_set():
                raise RuntimeError("Cancelled during call.")

            try:
                return self._call_with_timeout(tool_name, args)
            except TimeoutError as e:
                last_exc = e
                if attempt < max_retries:
                    continue
            except Exception as e:
                if attempt < max_retries and _is_retryable(e):
                    last_exc = e
                    continue
                raise

        raise last_exc  # type: ignore[misc]

    def _call_with_timeout(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        result_container: list[dict[str, Any] | Exception] = []
        t = threading.Thread(
            target=lambda: result_container.append(self._server.invoke(tool_name, args)),
            daemon=True,
        )
        t.start()
        t.join(timeout=self._timeout)
        if t.is_alive():
            raise TimeoutError(f"MCP tool '{tool_name}' timed out after {self._timeout}s.")
        result = result_container[0]
        if isinstance(result, Exception):
            raise result
        return result

    def list_tools(self):
        return self._server.list_tools()


def _is_retryable(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(kw in msg for kw in ("timeout", "temporarily", "busy", "unavailable", "retry"))
