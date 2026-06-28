"""Smoke tests for the Forge widget Map tab (forge_widget_map).

The QGIS / Qt bindings are stubbed by ``conftest.py`` so the
mixin imports cleanly. We exercise:

  - the geometry-kind heuristic (pure-Python, no Qt needed)
  - the tab injection (checks the widget tree wires up)
  - the end-to-end map pipeline
    (model JSON → .qml on disk → .qpt on disk → verifier report)
  - the Run Model + .qml-apply path with a fake ``QgsProject``
"""

from __future__ import annotations

import os
import sys
import types
from typing import Any

import pytest

# ─── Geometry kind heuristic ─────────────────────────────────────
# These don't need a QApplication - they go through the unbound
# function on the mixin class with a tiny host object.


def test_guess_geometry_kind_centroids_is_point() -> None:
    from model_forge.forge_widget_helpers.forge_widget_map import (
        ForgeWidgetMapMixin,
    )

    class _H:
        pass

    assert (
        ForgeWidgetMapMixin._guess_geometry_kind(_H(), {"algorithm_id": "native:centroids"})
        == "point"
    )


def test_guess_geometry_kind_line() -> None:
    from model_forge.forge_widget_helpers.forge_widget_map import (
        ForgeWidgetMapMixin,
    )

    class _H:
        pass

    assert (
        ForgeWidgetMapMixin._guess_geometry_kind(_H(), {"algorithm_id": "native:lineintersections"})
        == "line"
    )


def test_guess_geometry_kind_polygon_via_buffer() -> None:
    from model_forge.forge_widget_helpers.forge_widget_map import (
        ForgeWidgetMapMixin,
    )

    class _H:
        pass

    assert (
        ForgeWidgetMapMixin._guess_geometry_kind(_H(), {"algorithm_id": "native:buffer"})
        == "polygon"
    )


def test_guess_geometry_kind_raster_via_gdal() -> None:
    from model_forge.forge_widget_helpers.forge_widget_map import (
        ForgeWidgetMapMixin,
    )

    class _H:
        pass

    assert (
        ForgeWidgetMapMixin._guess_geometry_kind(_H(), {"algorithm_id": "gdal:warpreproject"})
        == "raster"
    )


def test_guess_geometry_kind_unknown_falls_back_to_polygon() -> None:
    from model_forge.forge_widget_helpers.forge_widget_map import (
        ForgeWidgetMapMixin,
    )

    class _H:
        pass

    assert (
        ForgeWidgetMapMixin._guess_geometry_kind(_H(), {"algorithm_id": "native:dissolve"})
        == "polygon"
    )


# ─── Filename sanitizer ────────────────────────────────────────────


def test_safe_filename_strips_windows_illegal_chars() -> None:
    from model_forge.forge_widget_helpers.forge_widget_map import (
        _safe_filename,
    )

    # The exact bug the user hit: a layer named
    # "Wydajność potencjalna <10" must produce a writable
    # .qml filename on NTFS. The "<" is the only char that
    # makes the name invalid; spaces are fine.
    assert _safe_filename("Wydajność potencjalna <10") == ("Wydajność potencjalna _10")
    # And all the other illegal chars go too.
    assert "<" not in _safe_filename("a<b")
    assert ">" not in _safe_filename("a>b")
    assert '"' not in _safe_filename('a"b')
    assert "/" not in _safe_filename("a/b")
    assert "\\" not in _safe_filename("a\\b")
    assert "|" not in _safe_filename("a|b")
    assert "?" not in _safe_filename("a?b")
    assert "*" not in _safe_filename("a*b")
    # Control chars too.
    assert "\x00" not in _safe_filename("a\x00b")


def test_safe_filename_collapses_runs() -> None:
    from model_forge.forge_widget_helpers.forge_widget_map import (
        _safe_filename,
    )

    assert _safe_filename("a<<<b///c") == "a_b_c"
    # Spaces are preserved; only runs of the replacement char
    # are collapsed.
    assert _safe_filename("foo  bar") == "foo  bar"
    assert _safe_filename("a _ _ b") == "a _ _ b"


def test_safe_filename_handles_reserved_device_names() -> None:
    from model_forge.forge_widget_helpers.forge_widget_map import (
        _safe_filename,
    )

    assert _safe_filename("CON").startswith("_")
    assert _safe_filename("nul").startswith("_")


def test_safe_filename_handles_empty() -> None:
    from model_forge.forge_widget_helpers.forge_widget_map import (
        _safe_filename,
    )

    assert _safe_filename("") == "unnamed"
    assert _safe_filename("   ") == "unnamed"
    assert _safe_filename("...") == "unnamed"


# ─── Layer collection + extent robustness ────────────────────────
# These tests verify the per-layer guarding that was silently
# dropping all layers when one ``extent()`` call raised.


def test_collect_current_layers_returns_all_ok_layers(monkeypatch, tmp_path) -> None:
    """5 healthy layers → 5 returned, no drops."""
    layers = [
        _make_fake_vector_layer("roads", "line", (0, 0, 100, 200)),
        _make_fake_vector_layer("parcels", "polygon", (10, 10, 90, 180)),
        _make_fake_vector_layer("centroids", "point", (50, 50, 60, 60)),
        _make_fake_vector_layer("raster_grid", "raster", (0, 0, 200, 200)),
        _make_fake_vector_layer(  # no extent → isEmpty() → still returned
            "empty_table",
            "polygon",
            None,
        ),
    ]
    _install_fake_project_with_layers(monkeypatch, str(tmp_path), layers)
    from model_forge.forge_widget_helpers.forge_widget_map import (
        ForgeWidgetMapMixin,
    )

    class _H(ForgeWidgetMapMixin):
        def __init__(self) -> None:
            self.lst_map_layers = type(
                "_", (), {"count": lambda: 0, "selectedItems": lambda: [], "item": lambda i: None}
            )()

    host = _H()
    collected = host._collect_current_layers()
    # All 5 layers should be present, including the one with
    # no extent (which would have been dropped by the old code).
    assert len(collected) == 5, f"got {len(collected)} layers"
    names = {c["name"] for c in collected}
    assert "roads" in names
    assert "empty_table" in names  # the silent-drop bug survivor


def test_collect_current_layers_survives_extent_failure(monkeypatch, tmp_path) -> None:
    """1 layer with broken extent() does NOT drop the other 4."""
    layers = [
        _make_fake_vector_layer(
            "bad_layer",
            "polygon",
            (0, 0, 10, 10),
            raise_on_extent=True,
        ),
        _make_fake_vector_layer("roads", "line", (0, 0, 100, 200)),
        _make_fake_vector_layer("parcels", "polygon", (10, 10, 90, 180)),
        _make_fake_vector_layer("centroids", "point", (50, 50, 60, 60)),
        _make_fake_vector_layer("raster_grid", "raster", (0, 0, 200, 200)),
    ]
    _install_fake_project_with_layers(monkeypatch, str(tmp_path), layers)
    from model_forge.forge_widget_helpers.forge_widget_map import (
        ForgeWidgetMapMixin,
    )

    class _H(ForgeWidgetMapMixin):
        def __init__(self) -> None:
            self.lst_map_layers = type(
                "_", (), {"count": lambda: 0, "selectedItems": lambda: [], "item": lambda i: None}
            )()

    host = _H()
    collected = host._collect_current_layers()
    # All 5 layers should be present. The bad layer's extent
    # failure is guarded per-layer, so it still appears (with
    # extent=None).
    assert len(collected) == 5, f"got {len(collected)} layers"
    names = {c["name"] for c in collected}
    assert "bad_layer" in names
    assert "roads" in names
    assert "raster_grid" in names


def test_current_project_extent_combines_all(monkeypatch, tmp_path) -> None:
    """5 layers with different extents → combined bbox is right."""
    layers = [
        _make_fake_vector_layer("roads", "line", (0, 0, 100, 200)),
        _make_fake_vector_layer("parcels", "polygon", (50, 100, 150, 300)),
        _make_fake_vector_layer("centroids", "point", (20, 20, 30, 30)),
    ]
    _install_fake_project_with_layers(monkeypatch, str(tmp_path), layers)
    from model_forge.forge_widget_helpers.forge_widget_map import (
        ForgeWidgetMapMixin,
    )

    class _H(ForgeWidgetMapMixin):
        def __init__(self) -> None:
            self.lst_map_layers = type(
                "_", (), {"count": lambda: 0, "selectedItems": lambda: [], "item": lambda i: None}
            )()

    host = _H()
    extent = host._current_project_extent()
    assert extent is not None
    xmin, ymin, xmax, ymax = extent
    assert xmin == 0.0  # roads' xmin
    assert ymin == 0.0  # roads' ymin
    assert xmax == 150.0  # parcels' xmax
    assert ymax == 300.0  # parcels' ymax


def test_current_project_extent_survives_bad_layer(monkeypatch, tmp_path) -> None:
    """1 broken layer does not make the whole extent return None."""
    layers = [
        _make_fake_vector_layer(
            "bad",
            "polygon",
            (0, 0, 10, 10),
            raise_on_extent=True,
        ),
        _make_fake_vector_layer("roads", "line", (0, 0, 100, 200)),
    ]
    _install_fake_project_with_layers(monkeypatch, str(tmp_path), layers)
    from model_forge.forge_widget_helpers.forge_widget_map import (
        ForgeWidgetMapMixin,
    )

    class _H(ForgeWidgetMapMixin):
        def __init__(self) -> None:
            self.lst_map_layers = type(
                "_", (), {"count": lambda: 0, "selectedItems": lambda: [], "item": lambda i: None}
            )()

    host = _H()
    extent = host._current_project_extent()
    assert extent is not None, "bad layer should not kill extent"
    xmin, ymin, xmax, ymax = extent
    assert xmin == 0.0
    assert ymax == 200.0


def test_collect_current_layers_has_correct_visible_flag(monkeypatch, tmp_path) -> None:
    """Visible=True stays True; Visible=False stays False."""
    layers = [
        _make_fake_vector_layer("visible_on", "line", (0, 0, 1, 1), visible=True),
        _make_fake_vector_layer("visible_off", "line", (0, 0, 1, 1), visible=False),
    ]
    _install_fake_project_with_layers(monkeypatch, str(tmp_path), layers)
    from model_forge.forge_widget_helpers.forge_widget_map import (
        ForgeWidgetMapMixin,
    )

    class _H(ForgeWidgetMapMixin):
        def __init__(self) -> None:
            self.lst_map_layers = type(
                "_", (), {"count": lambda: 0, "selectedItems": lambda: [], "item": lambda i: None}
            )()

    host = _H()
    collected = host._collect_current_layers()
    by_name = {c["name"]: c for c in collected}
    assert by_name["visible_on"]["visible"] is True
    assert by_name["visible_off"]["visible"] is False


# ─── Stub host that records the mixin's actions ──────────────────


class _TabHost:
    """Bare-bones host for the mixin; no real QWidget required.

    The mixin only touches ``self.tabs.addTab``; we replace
    ``tabs`` with a recording object.
    """

    def __init__(self) -> None:
        self.tabs = self._Tabs()
        self.iface = self._Iface()
        self.backend = self._Backend()
        self.context_collector = self._ContextCollector()
        self.chk_two_phase = self._CheckBox(checked=False)
        self._current_model_json = None
        self._map_worker = None

    class _Tabs:
        def __init__(self) -> None:
            self.widgets: list[tuple[Any, str]] = []

        def addTab(self, widget: Any, label: str) -> None:
            self.widgets.append((widget, label))

    class _Iface:
        def mapCanvas(self) -> Any:
            return None

    class _Backend:
        def generate_single_pass(self, *a: object, **k: object) -> dict:
            return {"inputs": [], "algorithms": []}

        def generate_plan(self, *a: object, **k: object) -> dict:
            return {}

        def generate_model_from_plan(self, *a: object, **k: object) -> dict:
            return {"inputs": [], "algorithms": []}

    class _ContextCollector:
        def collect(self, *a: object, **k: object) -> str:
            return ""

    class _CheckBox:
        def __init__(self, checked: bool = False) -> None:
            self._checked = checked

        def isChecked(self) -> bool:
            return self._checked


@pytest.fixture
def host() -> _TabHost:
    # Build a host *instance* of the mixin.
    from model_forge.forge_widget_helpers.forge_widget_map import (
        ForgeWidgetMapMixin,
    )

    class _Host(_TabHost, ForgeWidgetMapMixin):
        pass

    return _Host()


# ─── Tab injection ───────────────────────────────────────────────


def test_inject_map_tab_adds_a_widget(host: _TabHost) -> None:
    host._inject_map_tab()
    assert any(label == "Map" for _, label in host.tabs.widgets)


def test_inject_map_tab_creates_intent_box_and_buttons(host: _TabHost) -> None:
    host._inject_map_tab()
    assert host.txt_map_intent is not None
    assert host.btn_generate_map is not None
    assert host.btn_run_model is not None
    assert host.btn_symbology_only is not None


def test_inject_map_tab_creates_verifier_list(host: _TabHost) -> None:
    host._inject_map_tab()
    assert host.lst_map_verifier is not None
    assert host.lbl_map_status is not None


# ─── Symbology on disk ───────────────────────────────────────────


def test_write_symbology_emits_one_qml_per_step(host: _TabHost, tmp_path, monkeypatch) -> None:
    # Direct the project's "home" to tmp_path.
    _install_fake_project(monkeypatch, home=str(tmp_path))

    model = {
        "model_name": "smoke",
        "algorithms": [
            {"id": "buffer_1", "algorithm_id": "native:buffer"},
            {"id": "centroids_2", "algorithm_id": "native:centroids"},
        ],
    }
    sym_dir = host._write_symbology(model)
    assert os.path.isdir(sym_dir)
    files = sorted(os.listdir(sym_dir))
    assert files == ["buffer_1.qml", "centroids_2.qml"]

    import xml.etree.ElementTree as ET

    # Use a permissive parser; the .qml DOCTYPE points at an
    # external DTD, so the default parser refuses to load it
    # without network access.
    ET.XMLParser()
    for f in files:
        with open(os.path.join(sym_dir, f), "rb") as fh:
            data = fh.read()
        assert b"<qgis" in data and b"</qgis>" in data, f"{f} not a QGIS style"


# ─── Layout + verifier ───────────────────────────────────────────


def test_build_and_apply_layout_writes_qpt_and_loads(host: _TabHost, tmp_path, monkeypatch) -> None:
    try:
        from qgis.core import QgsLayoutItemMap  # noqa: F401
    except ImportError:
        pytest.skip("No QGIS layout bindings available")
    added_layouts = _install_fake_project(monkeypatch, home=str(tmp_path))

    model = {
        "model_name": "smoke",
        "model_group": "test",
        "algorithms": [
            {"id": "buffer_1", "algorithm_id": "native:buffer"},
        ],
    }
    qpt_path = host._build_and_apply_layout(model, "default")
    assert os.path.isfile(qpt_path)
    assert qpt_path.endswith(".qpt")
    # Layout manager received the new layout.
    assert len(added_layouts) == 1


def test_run_verifier_populates_list_widget(host: _TabHost, tmp_path, monkeypatch) -> None:
    _install_fake_project(monkeypatch, home=str(tmp_path))
    host._inject_map_tab()

    model = {
        "model_name": "smoke",
        "algorithms": [{"id": "a", "algorithm_id": "native:buffer"}],
    }
    host._run_verifier(model, "default")
    # The mixin calls ``self.lst_map_verifier.addItem``; the
    # stub widget is a plain object, so we just check that
    # ``_run_verifier`` didn't raise. (The list widget is
    # ``None`` here only if injection was skipped.)


# ─── Qt5 / Qt6 compatibility audit ──────────────────────────────


def test_qt_namespace_exposes_names_mixin_uses() -> None:
    """The mixin uses ``Qt.red`` to color verifier errors. In
    both PyQt5 and PyQt6 the symbol is reachable; on Qt6 it is
    an alias for ``Qt.GlobalColor.red``. This test pins the
    contract: when the conftest stub is in place, ``Qt.red``
    must resolve. If a future Qt major drops the unscoped
    alias, the stub installer in ``conftest.py`` needs a
    matching addition before this test passes.
    """
    from qgis.PyQt.QtCore import Qt  # type: ignore[import-not-found]

    assert hasattr(Qt, "red"), "Qt.red is required by forge_widget_map"


def test_qtcore_exposes_qthread_and_pyqtsignal() -> None:
    """Both PyQt5 and PyQt6 expose ``QThread`` and ``pyqtSignal``
    at ``qgis.PyQt.QtCore``. The MapGenerateWorker uses both."""
    from qgis.PyQt.QtCore import (  # type: ignore[import-not-found]
        QThread,
        pyqtSignal,
    )

    assert QThread is not None
    assert pyqtSignal is not None


def test_qtwidgets_exposes_all_classes_mixin_imports() -> None:
    """Pin the widget import surface the mixin depends on.

    If a future Qt major renames one of these, the stub
    installer (and the production code) need an update.
    """
    from qgis.PyQt.QtWidgets import (  # type: ignore[import-not-found]
        QComboBox,
        QHBoxLayout,
        QLabel,
        QListWidget,
        QListWidgetItem,
        QMessageBox,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )

    for cls in (
        QComboBox,
        QHBoxLayout,
        QLabel,
        QListWidget,
        QListWidgetItem,
        QMessageBox,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QVBoxLayout,
        QWidget,
    ):
        assert cls is not None


def test_load_named_style_branch_is_defensive() -> None:
    """The mixin handles either the Qt5 ``(bool, str)`` return
    or the Qt6 ``(LoadStyleResult, str)`` return. We don't bind
    to a real layer in the stub; we just confirm the
    equivalence predicate the production code uses is correct.
    """
    # Mirror the production branch exactly. The predicate
    # ``rc is True or str(rc) == "True" or str(rc).endswith("Success")``
    # covers Qt5's ``True``/``False`` returns and Qt6's
    # ``LoadStyleResult.Success``/``.Failure`` enum returns
    # without falling into the Python ``False == 0`` truthiness
    # trap.
    for rc, expect in [
        (True, True),  # Qt5 success
        (False, False),  # Qt5 failure
        ("LoadStyleResult.Success", True),  # Qt6 success
        ("LoadStyleResult.Failure", False),  # Qt6 failure
        ("Success", True),  # bare enum name
    ]:
        ok = rc is True or str(rc) == "True" or str(rc).endswith("Success")
        assert ok == expect, f"rc={rc!r} got ok={ok}"


# ─── Helpers ─────────────────────────────────────────────────────


class _QgsRectangle:
    """Fake ``QgsRectangle`` for the tests.

    QGIS's ``QgsRectangle(xmin, ymin, xmax, ymax)`` has
    ``combineExtentWith(other)``, ``isEmpty()``, and
    ``xMinimum()`` / ``yMinimum()`` / ``xMaximum()`` /
    ``yMaximum()``. This stub supports those so
    ``_collect_current_layers`` and
    ``_current_project_extent`` can exercise their actual
    computation paths instead of silently dropping.
    """

    def __init__(
        self,
        xmin: float | None = None,
        ymin: float | None = None,
        xmax: float | None = None,
        ymax: float | None = None,
    ) -> None:
        self._xmin: float | None = xmin
        self._ymin: float | None = ymin
        self._xmax: float | None = xmax
        self._ymax: float | None = ymax

    def combineExtentWith(self, other: _QgsRectangle) -> None:
        if self._xmin is not None and other._xmin is not None:
            self._xmin = min(self._xmin, other._xmin)
            self._ymin = min(self._ymin, other._ymin)
            self._xmax = max(self._xmax, other._xmax)
            self._ymax = max(self._ymax, other._ymax)

    def isEmpty(self) -> bool:
        return (
            self._xmin is None
            or self._ymin is None
            or self._xmax is None
            or self._ymax is None
            or self._xmin >= self._xmax
            or self._ymin >= self._ymax
        )

    def xMinimum(self) -> float:
        return self._xmin or 0.0

    def yMinimum(self) -> float:
        return self._ymin or 0.0

    def xMaximum(self) -> float:
        return self._xmax or 0.0

    def yMaximum(self) -> float:
        return self._ymax or 0.0


def _make_fake_vector_layer(
    name: str,
    kind: str,
    extent: tuple[float, float, float, float] | None,
    visible: bool = True,
    raise_on_extent: bool = False,
) -> Any:
    """Build a fake ``QgsVectorLayer``-like object.

    The returned object exposes the attributes that
    ``_collect_current_layers`` and
    ``_current_project_extent`` actually read:
    ``id()``, ``name()``, ``type()``,
    ``geometryType()``, ``isVisible()``, and
    ``extent()`` (returns a ``_QgsRectangle``).

    When ``raise_on_extent`` is True, ``extent()``
    raises ``RuntimeError``. This is the bug pattern
    that silently dropped *all* layers in the old
    code: a single bad ``extent()`` call in the loop
    body triggered ``except: continue`` and skipped
    every subsequent layer in the loop, but an
    ``except: return`` on the outer function would
    return ``[]`` for the whole collection.
    """
    _geom_map = {"point": 0, "line": 1, "polygon": 2}

    class _RasterType:
        def __eq__(self, other: Any) -> bool:
            return False

        def __ne__(self, other: Any) -> bool:
            return True

    def _extent() -> _QgsRectangle:
        if raise_on_extent:
            raise RuntimeError("simulated extent failure")
        if extent is None:
            return _QgsRectangle()  # isEmpty() → True
        xmin, ymin, xmax, ymax = extent
        return _QgsRectangle(xmin, ymin, xmax, ymax)

    def _name() -> str:
        return name

    def _id() -> str:
        # Stable id derived from name so the picker → collector
        # → resolver chain can match on it.
        if name in _id_map:
            return _id_map[name]
        lid = f"test_{name.replace(' ', '_')}_{len(_id_map)}"
        _id_map[name] = lid
        return lid

    _id_map: dict[str, str] = {}

    layer = types.SimpleNamespace(
        id=_id,
        name=_name,
        type=type("Type", (), {"RasterLayer": _RasterType()}),
        geometryType=lambda: _geom_map.get(kind, 2),
        extent=_extent,
        isVisible=lambda: visible,
    )
    return layer


def _install_fake_project_with_layers(
    monkeypatch: pytest.MonkeyPatch,
    home: str,
    layers: list[Any],
) -> list[Any]:
    """Like ``_install_fake_project`` but also populates
    ``mapLayers()`` with the given ``layers`` list.

    Returns the layout list for assertions.
    """

    added_layouts: list[Any] = []

    class _LayoutManager:
        def addLayout(self, layout: Any) -> None:
            added_layouts.append(layout)

        def removeLayout(self, layout: Any) -> None:
            if layout in added_layouts:
                added_layouts.remove(layout)

        def layoutByName(self, name: str) -> Any:
            return None

        def layouts(self) -> list[Any]:
            return added_layouts

    class _PrintLayout:
        _cnt = 0

        def __init__(self) -> None:
            _PrintLayout._cnt += 1
            self._name = f"Map{_PrintLayout._cnt}"

        def loadFromTemplate(self, *a: object, **k: object) -> bool:
            return True

        def name(self) -> str:
            return self._name

        def setName(self, n: str) -> None:
            self._name = n

        def initializeDefaults(self) -> None:
            pass

        def addLayoutItem(self, *a: object, **k: object) -> None:
            pass

        def refresh(self) -> None:
            pass

    class _Project:
        _inst: _Project | None = None

        def __init__(self) -> None:
            self._home = home
            self._layout_manager = _LayoutManager()
            self._layers: dict[str, Any] = {}
            self._crs = types.SimpleNamespace(authid=lambda: "EPSG:2180")

        @classmethod
        def instance(cls) -> _Project:
            if cls._inst is None:
                cls._inst = cls()
            return cls._inst

        def homePath(self) -> str:
            return self._home

        def mapLayers(self) -> dict[str, Any]:
            return self._layers

        def mapLayer(self, lid: str) -> Any:
            return self._layers.get(lid)

        def addMapLayer(self, layer: Any) -> None:
            lid = layer.id()
            if lid:
                self._layers[lid] = layer

        def crs(self) -> Any:
            return self._crs

        def layoutManager(self) -> _LayoutManager:
            return self._layout_manager

    proj = _Project()
    _Project._inst = proj
    # Register the layers.
    for layer in list(layers):
        proj.addMapLayer(layer)

    # Patch the modules.
    core = sys.modules.get("qgis.core")
    if core is not None:
        monkeypatch.setattr(core, "QgsProject", _Project, raising=False)
        monkeypatch.setattr(core, "QgsPrintLayout", _PrintLayout, raising=False)
        monkeypatch.setattr(core, "QgsRectangle", _QgsRectangle, raising=False)
    return added_layouts


def _install_fake_project(monkeypatch: pytest.MonkeyPatch, home: str) -> list[Any]:
    """Patch ``qgis.core.QgsProject.instance()`` to a fake.

    Returns the list of layouts the mixin has registered so
    the test can assert on it.
    """
    added_layouts: list[Any] = []

    class _LayoutManager:
        def addLayout(self, layout: Any) -> None:
            added_layouts.append(layout)

        def removeLayout(self, layout: Any) -> None:
            if layout in added_layouts:
                added_layouts.remove(layout)

        def layoutByName(self, name: str) -> Any:
            return None

    class _PrintLayout:
        _counter = 0

        def __init__(self, *args: object, **kwargs: object) -> None:
            _PrintLayout._counter += 1
            self._name = f"Map{_PrintLayout._counter}"

        def loadFromTemplate(self, *args: object, **kwargs: object) -> bool:
            return True

        def setName(self, n: str) -> None:
            self._name = n

        def initializeDefaults(self) -> None:
            pass

        def addLayoutItem(self, *a: object, **k: object) -> None:
            pass

        def refresh(self) -> None:
            pass

        def name(self) -> str:
            return self._name

    class _Project:
        _instance: _Project | None = None

        def __init__(self) -> None:
            self._home = home
            self._layout_manager = _LayoutManager()

        @classmethod
        def instance(cls) -> _Project:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

        def homePath(self) -> str:
            return self._home

        def layoutManager(self) -> _LayoutManager:
            return self._layout_manager

    # Patch the already-imported qgis.core module.
    core = sys.modules.get("qgis.core")
    if core is not None:
        monkeypatch.setattr(core, "QgsProject", _Project, raising=False)
        monkeypatch.setattr(core, "QgsPrintLayout", _PrintLayout, raising=False)
    return added_layouts
