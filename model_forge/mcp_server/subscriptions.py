"""Resource subscription + tools/list_changed capability shim.

FastMCP 1.27.2 has no first-class ``resources/subscribe`` /
``notifications/resources/updated`` API at the high level. The
low-level ``Server`` class has the handlers but no public hook to
*trigger* a push notification from server code.

This module provides a shim that:

1. Tracks a per-URI ``dirty`` flag — set whenever a server-side
   state change touches the resource (``refresh_qgis_context``,
   ``set_llm_config``, etc.).
2. Exposes a ``ResourceSubscriber`` registry that lets clients
   poll the dirty set via the ``subscription_status`` tool.
3. Emits capability flags (``resources.subscribe``, ``tools.listChanged``)
   in ``server-info`` so clients know the server supports these.
4. Provides ``mark_resource_dirty(uri)`` and
   ``mark_tools_changed()`` so server-side code (including the
   job-registry callbacks and ``refresh_qgis_context``) can
   trigger a notification cycle.

When FastMCP ships a public notification API, the
``_push_to_subscribers`` hook in this module is the single place
to wire the real send-notification call. The rest of the codebase
already uses ``mark_resource_dirty`` and ``mark_tools_changed``,
so a future swap is a one-file change.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Iterable

log = logging.getLogger(__name__)

# ─── Public capability flags (read by clients) ────────────────────────

SUBSCRIBE_CAPABILITIES: dict[str, bool] = {
    "resources_subscribe": True,
    "resources_list_changed": True,
    "tools_list_changed": True,
}

# URIs we currently track. Anything not in this set is treated as
# read-once with no subscription semantics.
SUBSCRIBABLE_URIS: frozenset[str] = frozenset(
    {
        "model-forge://server-info",
        "model-forge://context/layers",
        "model-forge://algorithms",
    }
)


class SubscriptionRegistry:
    """In-process tracking of dirty resources and tool-list changes.

    Thread-safe; all mutations go through the internal lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._dirty_resources: set[str] = set()
        self._tools_dirty: bool = False
        self._subscribers: dict[str, set[str]] = {}  # uri -> set of client_ids
        self._version: int = 0  # monotonically increasing tick

    # ─── Subscription API ───────────────────────────────────────

    def subscribe(self, uri: str, client_id: str = "default") -> bool:
        if uri not in SUBSCRIBABLE_URIS:
            return False
        with self._lock:
            self._subscribers.setdefault(uri, set()).add(client_id)
        return True

    def unsubscribe(self, uri: str, client_id: str = "default") -> bool:
        with self._lock:
            subs = self._subscribers.get(uri)
            if subs is None or client_id not in subs:
                return False
            subs.discard(client_id)
            if not subs:
                self._subscribers.pop(uri, None)
        return True

    def subscribers(self, uri: str) -> set[str]:
        with self._lock:
            return set(self._subscribers.get(uri, ()))

    # ─── Dirty-set API ─────────────────────────────────────────

    def mark_resource_dirty(self, uri: str) -> None:
        if uri not in SUBSCRIBABLE_URIS:
            return
        with self._lock:
            self._dirty_resources.add(uri)
            self._version += 1
        self._push_to_subscribers(uri, "resources/updated")

    def mark_resources_dirty(self, uris: Iterable[str]) -> None:
        for uri in uris:
            self.mark_resource_dirty(uri)

    def mark_tools_changed(self) -> None:
        with self._lock:
            self._tools_dirty = True
            self._version += 1
        self._push_to_subscribers("*", "tools/list_changed")

    def consume_dirty(self) -> dict[str, Any]:
        """Atomically return and clear the dirty set.

        Used by the ``subscription_status`` tool to give clients
        a snapshot of what changed since they last checked.
        """
        with self._lock:
            dirty = sorted(self._dirty_resources)
            tools_dirty = self._tools_dirty
            self._dirty_resources.clear()
            self._tools_dirty = False
            self._version += 1
        return {
            "dirty_resources": dirty,
            "tools_changed": tools_dirty,
            "version": self._version,
        }

    def status(self) -> dict[str, Any]:
        """Read-only snapshot — does not clear the dirty set."""
        with self._lock:
            return {
                "dirty_resources": sorted(self._dirty_resources),
                "tools_changed": self._tools_dirty,
                "subscribed_resources": sorted(self._subscribers.keys()),
                "version": self._version,
            }

    # ─── Notification dispatch (hook for future FastMCP wiring) ─

    def _push_to_subscribers(self, uri: str, kind: str) -> None:
        """Notify subscribers. No-op until FastMCP exposes a public
        notification API; logs the intent so we can see it in tests.
        """
        # When FastMCP ships a public ``send_notification`` (or
        # ``session.send_resource_updated``) this is the single
        # call site to update. Today: just log.
        log.debug(
            "subscription push: kind=%s uri=%s subscribers=%d",
            kind,
            uri,
            len(self.subscribers(uri)),
        )


# ─── Module-level singleton ──────────────────────────────────────────

_registry: SubscriptionRegistry | None = None
_registry_lock = threading.Lock()


def get_subscription_registry() -> SubscriptionRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = SubscriptionRegistry()
    return _registry


def reset_subscription_registry() -> None:
    global _registry
    with _registry_lock:
        _registry = None


__all__ = [
    "SUBSCRIBABLE_URIS",
    "SUBSCRIBE_CAPABILITIES",
    "SubscriptionRegistry",
    "get_subscription_registry",
    "reset_subscription_registry",
]
