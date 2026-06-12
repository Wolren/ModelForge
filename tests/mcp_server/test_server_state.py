"""Tests for the MCP server's :class:`ServerState` and the canvas
extent fix.

We don't import the full module here (it would require ``mcp`` to be
installed). We only import the bits we need to test, and we patch out
the QGIS parts.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest


@pytest.fixture
def server_module(monkeypatch):
    """Import ``server.py`` with QGIS and uvicorn stubs in place.

    This is needed so the module's top-level imports succeed in CI
    environments where neither is available. We *don't* exercise the
    FastMCP path; we only test ``ServerState`` and the canvas helper.
    """
    # Make ``qgis`` and friends importable *before* server.py is loaded,
    # and make sure they look like real packages (so ``from qgis.core
    # import …`` succeeds). The classes themselves are placeholders;
    # the tests that need real behavior (``test_capture_qgis_context_…``)
    # patch the server module's bound names.
    qgis_pkg = types.ModuleType("qgis")
    qgis_pkg.__path__ = []  # type: ignore[attr-defined]
    core_stub = types.ModuleType("qgis.core")
    core_stub.__path__ = []  # type: ignore[attr-defined]
    pyqt_pkg = types.ModuleType("qgis.PyQt")
    pyqt_pkg.__path__ = []  # type: ignore[attr-defined]
    qtcore_stub = types.ModuleType("qgis.PyQt.QtCore")
    qtcore_stub.QCoreApplication = types.SimpleNamespace()
    pyqt_pkg.QtCore = qtcore_stub
    qgis_pkg.core = core_stub
    qgis_pkg.PyQt = pyqt_pkg

    # Pre-register the symbols server.py imports. Tests override
    # these with their own fakes as needed.
    for name in (
        "QgsApplication",
        "QgsCoordinateReferenceSystem",
        "QgsProject",
        "QgsProcessingModelAlgorithm",
    ):
        setattr(core_stub, name, types.SimpleNamespace())

    sys.modules["qgis"] = qgis_pkg
    sys.modules["qgis.core"] = core_stub
    sys.modules["qgis.PyQt"] = pyqt_pkg
    sys.modules["qgis.PyQt.QtCore"] = qtcore_stub

    # Flip _HAS_QGIS=True *before* importing server.py so its top-level
    # ``if _HAS_QGIS:`` block binds the QGIS class names into the
    # server module's namespace.
    init_mod = importlib.import_module("model_forge.mcp_server")
    monkeypatch.setattr(init_mod, "_HAS_QGIS", True, raising=False)

    # Drop any cached server.py so it re-executes with the patched
    # _HAS_QGIS flag.
    sys.modules.pop("model_forge.mcp_server.server", None)
    server = importlib.import_module("model_forge.mcp_server.server")
    yield server
    for name in (
        "model_forge.mcp_server.server",
        "qgis",
        "qgis.core",
        "qgis.PyQt",
        "qgis.PyQt.QtCore",
    ):
        sys.modules.pop(name, None)


def test_version_constant_matches_package(monkeypatch):
    """The server module must not hardcode its own version string."""
    from model_forge import mcp_server

    server = importlib.import_module("model_forge.mcp_server.server")
    # The single source of truth is the package's __version__.
    assert hasattr(server, "__version__")
    assert server.__version__ == mcp_server.__version__


def test_schema_version_is_exported():
    from model_forge import mcp_server
    from model_forge.mcp_server.server import SCHEMA_VERSION

    assert SCHEMA_VERSION == mcp_server.SCHEMA_VERSION


def test_tool_registry_is_a_tuple(server_module):
    """A tuple, not a list, so it can never be mutated at runtime."""
    from model_forge.mcp_server.server import TOOL_REGISTRY

    assert isinstance(TOOL_REGISTRY, tuple)
    assert "ping" in TOOL_REGISTRY
    assert "generate_model" in TOOL_REGISTRY
    assert "set_llm_config" in TOOL_REGISTRY


def test_server_state_constructor_accepts_dict(server_module):
    from model_forge.mcp_server.server import ServerState

    s = ServerState(llm_config={"provider": "ollama", "model": "q"})
    assert s.llm_config["provider"] == "ollama"


def test_server_state_refresh_under_lock(server_module):
    """refresh() must not deadlock when called concurrently."""
    import threading

    from model_forge.mcp_server.server import ServerState

    state = ServerState()
    errors: list[Exception] = []

    def _hammer():
        try:
            for _ in range(50):
                state.refresh()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not errors


def test_capture_qgis_context_with_fake_canvas(monkeypatch, server_module):
    """The canvas extent should be read from mapCanvas(), not from the
    message bar."""
    # Configure a project stub that returns an empty layers dict but
    # a valid CRS. ``server.py`` did ``from qgis.core import ...`` at
    # import time, so the names live in the server module — patch
    # those, not the qgis.core module attribute.
    project = types.SimpleNamespace()
    project.crs = lambda: types.SimpleNamespace(isValid=lambda: True, authid=lambda: "EPSG:4326")
    project.mapLayers = lambda: {}

    class _ProjectCls:
        @classmethod
        def instance(cls):
            return project

    monkeypatch.setattr(server_module, "QgsProject", _ProjectCls)

    # Build a fake canvas. The buggy code would have called
    # ``QgsApplication.instance().activeMessageBar().extent()`` and
    # crashed or returned None.
    class _Extent:
        def __init__(self):
            self._null = False

        def isNull(self):
            return self._null

        def asWktPolygon(self):
            return "POLYGON((0 0, 0 1, 1 1, 1 0, 0 0))"

    class _Canvas:
        def extent(self):
            return _Extent()

    class _App:
        def mapCanvas(self):
            return _Canvas()

    class _AppCls:
        @staticmethod
        def instance():
            return _App()

    monkeypatch.setattr(server_module, "QgsApplication", _AppCls)

    # ``qgis.utils`` is the iface accessor; make it raise so the code
    # falls through to the standalone ``QgsApplication.mapCanvas()``.
    class _NoIface:
        def __getattr__(self, name):
            raise RuntimeError("no iface")

    fake_utils = types.ModuleType("qgis.utils")
    fake_utils.iface = _NoIface()
    monkeypatch.setitem(sys.modules, "qgis.utils", fake_utils)

    from model_forge.mcp_server.server import ServerState

    ctx = ServerState._capture_qgis_context()
    assert ctx["project_crs"] == "EPSG:4326"
    assert ctx["layers"] == []
    # The polygon WKT is what we set, not None (which is what the bug
    # used to produce).
    assert "POLYGON" in (ctx["canvas_extent"] or "")
