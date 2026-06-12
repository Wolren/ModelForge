"""Shared pytest fixtures for the Model Forge test suite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the project root is importable. ``pyproject.toml`` also pins
# ``pythonpath = ["model_forge"]`` so this is belt-and-braces for
# editors that don't read the config.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch, tmp_path):
    """Point the persisted MCP config at a per-test temp file.

    This is autouse so that nothing in the suite accidentally writes
    to the user's real ``~/.config/model-forge/mcp.json``.
    """
    monkeypatch.setenv("MODELFORGE_MCP_CONFIG", str(tmp_path / "mcp.json"))
    yield
