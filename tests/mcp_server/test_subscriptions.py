"""Tests for the subscription registry (Phase 5)."""

from __future__ import annotations

import pytest

from model_forge.mcp_server.subscriptions import (
    SUBSCRIBABLE_URIS,
    SUBSCRIBE_CAPABILITIES,
    SubscriptionRegistry,
    get_subscription_registry,
    reset_subscription_registry,
)


@pytest.fixture
def reg():
    reset_subscription_registry()
    r = SubscriptionRegistry()
    yield r


def test_capabilities_flagged_on():
    assert SUBSCRIBE_CAPABILITIES["resources_subscribe"] is True
    assert SUBSCRIBE_CAPABILITIES["tools_list_changed"] is True
    assert SUBSCRIBE_CAPABILITIES["resources_list_changed"] is True


def test_subscribable_uris_are_a_frozenset():
    assert isinstance(SUBSCRIBABLE_URIS, frozenset)
    assert "model-forge://server-info" in SUBSCRIBABLE_URIS
    assert "model-forge://context/layers" in SUBSCRIBABLE_URIS
    assert "model-forge://algorithms" in SUBSCRIBABLE_URIS


def test_subscribe_to_known_uri(reg):
    assert reg.subscribe("model-forge://context/layers", "client-a") is True
    assert "client-a" in reg.subscribers("model-forge://context/layers")


def test_subscribe_to_unknown_uri_returns_false(reg):
    assert reg.subscribe("https://example.com", "client-a") is False
    assert reg.subscribers("https://example.com") == set()


def test_subscribe_idempotent(reg):
    reg.subscribe("model-forge://context/layers", "client-a")
    reg.subscribe("model-forge://context/layers", "client-a")
    assert reg.subscribers("model-forge://context/layers") == {"client-a"}


def test_unsubscribe(reg):
    reg.subscribe("model-forge://context/layers", "client-a")
    assert reg.unsubscribe("model-forge://context/layers", "client-a") is True
    assert reg.subscribers("model-forge://context/layers") == set()


def test_unsubscribe_unknown_returns_false(reg):
    assert reg.unsubscribe("model-forge://context/layers", "nobody") is False


def test_unsubscribe_removes_empty_bucket(reg):
    reg.subscribe("model-forge://context/layers", "client-a")
    reg.unsubscribe("model-forge://context/layers", "client-a")
    assert "model-forge://context/layers" not in reg.status()["subscribed_resources"]


def test_mark_resource_dirty_increments_version(reg):
    initial = reg.status()["version"]
    reg.mark_resource_dirty("model-forge://context/layers")
    assert reg.status()["version"] > initial
    assert "model-forge://context/layers" in reg.status()["dirty_resources"]


def test_mark_resources_dirty(reg):
    reg.mark_resources_dirty({"model-forge://server-info", "model-forge://algorithms"})
    s = reg.status()
    assert "model-forge://server-info" in s["dirty_resources"]
    assert "model-forge://algorithms" in s["dirty_resources"]


def test_mark_resource_dirty_ignores_unknown(reg):
    reg.mark_resource_dirty("https://example.com")
    assert reg.status()["dirty_resources"] == []


def test_mark_tools_changed(reg):
    reg.mark_tools_changed()
    assert reg.status()["tools_changed"] is True


def test_consume_dirty_clears(reg):
    reg.mark_resource_dirty("model-forge://context/layers")
    reg.mark_tools_changed()
    payload = reg.consume_dirty()
    assert "model-forge://context/layers" in payload["dirty_resources"]
    assert payload["tools_changed"] is True
    s = reg.status()
    assert s["dirty_resources"] == []
    assert s["tools_changed"] is False


def test_singleton():
    reset_subscription_registry()
    a = get_subscription_registry()
    b = get_subscription_registry()
    assert a is b


def test_reset_replaces_singleton():
    a = get_subscription_registry()
    reset_subscription_registry()
    b = get_subscription_registry()
    assert a is not b
