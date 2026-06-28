"""Auto-stub QGIS bindings for the forge_widget tests.

The Map-tab mixin imports ``qgis.PyQt.QtCore`` and
``qgis.PyQt.QtWidgets`` at module import time. When the test
suite runs outside QGIS (e.g. CI on a generic Python image),
those imports must still resolve.

This conftest installs a minimal stub module tree — no real
PyQt5 / PyQt6 binding is needed. The stubs are version-neutral
(the production code uses the ``qgis.PyQt`` re-export shim
that QGIS provides on top of whichever Qt is bundled, and
the stub mirrors that shim's surface area).

If you need to add a real Qt event loop to a test, install
``pytest-qt`` with the matching Qt major version. Otherwise
the test functions in this directory only exercise pure-Python
behaviour through the mixin.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest


def _make_signal() -> type:
    class _Signal:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.args = args

        def connect(self, *args: object, **kwargs: object) -> None:
            return None

        def emit(self, *args: object, **kwargs: object) -> None:
            return None

    return _Signal


def _make_widget_class() -> type:
    class _W:
        # QListWidget-style enums that the mixin references.
        # QListWidget-style enums and QAbstractItemView enums
        # that the mixin references. Returning ints makes
        # ``QListWidget.NoSelection`` / ``QAbstractItemView.MultiSelection``
        # no-ops; the real value isn't used by the stub.
        NoSelection = 0
        SingleSelection = 1
        MultiSelection = 2
        ExtendedSelection = 3
        ItemIsUserCheckable = 32
        Checked = 2
        Unchecked = 0

        def __init__(self, *args: object, **kwargs: object) -> None:
            # Recording dict for any kwargs the mixin uses.
            self._calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

        def __getattr__(self, name: str) -> Any:
            # Qt signal names return a Signal; everything else
            # returns a callable stub that records the call.
            # Methods the mixin calls that return lists.
            if name in ("selectedItems",):
                return lambda *a, **k: []
            if name in ("count",):
                return lambda *a, **k: 0
            if name in ("item",):
                return lambda *a, **k: None

            if name in (
                "clicked",
                "pressed",
                "released",
                "toggled",
                "textChanged",
                "currentIndexChanged",
                "editingFinished",
                "returnPressed",
                "itemSelectionChanged",
                "itemChanged",
                "valueChanged",
                "triggered",
            ):
                return _make_signal()

            def _stub(*args: object, **kwargs: object) -> Any:
                self._calls.append((name, args, kwargs))
                return None

            return _stub

        # Used by Qt's API in some constructors.
        def setObjectName(self, *args: object, **kwargs: object) -> None:
            return None

    return _W


@pytest.fixture(autouse=True)
def _stub_qgis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the qgis.* stubs for every test in this directory."""
    qgis_pkg = types.ModuleType("qgis")
    qgis_pkg.__path__ = []  # type: ignore[attr-defined]
    core = types.ModuleType("qgis.core")
    pyqt = types.ModuleType("qgis.PyQt")
    pyqt.__path__ = []  # type: ignore[attr-defined]
    qtcore = types.ModuleType("qgis.PyQt.QtCore")
    qtwidgets = types.ModuleType("qgis.PyQt.QtWidgets")

    # Qt namespace: the mixin uses ``Qt.red`` (GlobalColor alias
    # in Qt6 / direct in Qt5). Both PyQt5 and PyQt6 expose it
    # as an attribute of ``Qt``; the stub mirrors that.
    class _Qt:
        red = object()  # sentinel - the value doesn't matter for the stub
        darkGreen = object()  # same for diagnostic items

    qtcore.Qt = _Qt
    qtcore.QThread = type("QThread", (), {})
    qtcore.QSettings = type("QSettings", (), {})
    qtcore.pyqtSignal = _make_signal()  # type: ignore[attr-defined]

    W = _make_widget_class()
    qtwidgets.QApplication = W
    qtwidgets.QWidget = W
    qtwidgets.QLabel = W
    qtwidgets.QPlainTextEdit = W
    qtwidgets.QPushButton = W
    qtwidgets.QCheckBox = W
    qtwidgets.QComboBox = W
    qtwidgets.QAbstractItemView = W
    qtwidgets.QListWidget = W
    qtwidgets.QListWidgetItem = W
    qtwidgets.QGroupBox = W
    qtwidgets.QMessageBox = W
    qtwidgets.QProgressBar = W

    class _Layout:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def addWidget(self, *a: object, **k: object) -> None:
            return None

        def addLayout(self, *a: object, **k: object) -> None:
            return None

        def addStretch(self, *a: object, **k: object) -> None:
            return None

        def setSpacing(self, *a: object, **k: object) -> None:
            return None

    qtwidgets.QVBoxLayout = _Layout
    qtwidgets.QHBoxLayout = _Layout

    core.QgsProject = type("QgsProject", (), {})  # type: ignore[attr-defined]
    core.QgsVectorLayer = type("QgsVectorLayer", (), {})  # type: ignore[attr-defined]
    core.QgsRasterLayer = type("QgsRasterLayer", (), {})  # type: ignore[attr-defined]
    core.QgsRectangle = type("QgsRectangle", (), {"__init__": lambda *a, **k: None})  # type: ignore[attr-defined]
    core.QgsUnitTypes = type("QgsUnitTypes", (), {"LayoutMillimeters": 0})  # type: ignore[attr-defined]
    core.QgsPrintLayout = type(
        "QgsPrintLayout",
        (),
        {"setName": lambda *a, **k: None, "initializeDefaults": lambda *a, **k: None},
    )  # type: ignore[attr-defined]
    core.QgsLayoutManager = type("QgsLayoutManager", (), {})  # type: ignore[attr-defined]
    core.QgsLayoutItem = type("QgsLayoutItem", (), {})  # type: ignore[attr-defined]

    _layout_item = type(
        "_LayoutItem",
        (),
        {
            "__init__": lambda *a, **k: None,
            "setText": lambda *a, **k: None,
            "setFontSize": lambda *a, **k: None,
            "attemptMove": lambda *a, **k: None,
            "attemptResize": lambda *a, **k: None,
            "setExtent": lambda *a, **k: None,
            "setFrameEnabled": lambda *a, **k: None,
            "setBackgroundEnabled": lambda *a, **k: None,
            "setTitle": lambda *a, **k: None,
            "setLinkedMap": lambda *a, **k: None,
            "setStyle": lambda *a, **k: None,
            "setNumberOfSegments": lambda *a, **k: None,
            "setNumberOfSegmentsLeft": lambda *a, **k: None,
            "setPicturePath": lambda *a, **k: None,
            "addLayoutItem": lambda *a, **k: None,
            "applyDefaultSettings": lambda *a, **k: None,
            "zoomToExtent": lambda *a, **k: None,
            "setCrs": lambda *a, **k: None,
            "setFrameStrokeWidth": lambda *a, **k: None,
            "setLinkedMap": lambda *a, **k: None,
            "refresh": lambda *a, **k: None,
            "update": lambda *a, **k: None,
            "adjustBoxSize": lambda *a, **k: None,
            "setAutoUpdateModel": lambda *a, **k: None,
            "modelRootGroup": lambda *a, **k: type(
                "_RG",
                (),
                {
                    "children": lambda *a: [],
                    "removeChildNode": lambda *a, **k: None,
                    "addChildNode": lambda *a, **k: None,
                },
            )(),
        },
    )
    core.QgsLayoutItemMap = _layout_item
    core.QgsLayoutItemLabel = _layout_item
    core.QgsLayoutItemLegend = _layout_item
    core.QgsLayoutItemScaleBar = _layout_item
    core.QgsLayoutItemPicture = _layout_item

    core.QgsLayoutPoint = type("QgsLayoutPoint", (), {"__init__": lambda *a, **k: None})  # type: ignore[attr-defined]
    core.QgsLayoutSize = type("QgsLayoutSize", (), {"__init__": lambda *a, **k: None})  # type: ignore[attr-defined]
    core.QgsUnitTypes = type("QgsUnitTypes", (), {"LayoutMillimeters": 0})  # type: ignore[attr-defined]
    core.QgsLayerTreeLayer = type("QgsLayerTreeLayer", (), {"__init__": lambda *a, **k: None})  # type: ignore[attr-defined]
    core.QgsFillSymbol = type(
        "QgsFillSymbol", (), {"createSimple": staticmethod(lambda *a, **k: None)}
    )  # type: ignore[attr-defined]
    core.QgsLayoutItemShape = type(
        "QgsLayoutItemShape",
        (),
        {
            "__init__": lambda *a, **k: None,
            "setShapeType": lambda *a, **k: None,
            "setSymbol": lambda *a, **k: None,
            "Rectangle": 0,
            "attemptMove": lambda *a, **k: None,
            "attemptResize": lambda *a, **k: None,
        },
    )  # type: ignore[attr-defined]
    core.QgsCoordinateReferenceSystem = type(
        "QgsCoordinateReferenceSystem", (), {"__init__": lambda *a, **k: None}
    )  # type: ignore[attr-defined]
    core.QgsCoordinateTransform = type(
        "QgsCoordinateTransform", (), {"__init__": lambda *a, **k: None}
    )  # type: ignore[attr-defined]
    core.QgsLayoutItemMapGrid = type(
        "QgsLayoutItemMapGrid",
        (),
        {
            "__init__": lambda *a, **k: None,
            "setEnabled": lambda *a, **k: None,
            "setCrs": lambda *a, **k: None,
            "setIntervalX": lambda *a, **k: None,
            "setIntervalY": lambda *a, **k: None,
            "setAnnotationEnabled": lambda *a, **k: None,
            "setAnnotationFont": lambda *a, **k: None,
            "setAnnotationFormat": lambda *a, **k: None,
            "DecimalWithSuffix": 0,
        },
    )  # type: ignore[attr-defined]

    pyqt.QtCore = qtcore
    pyqt.QtWidgets = qtwidgets
    qgis_pkg.core = core
    qgis_pkg.PyQt = pyqt

    # qgis.PyQt.QtXml - need a QDomDocument stub.
    qtxml = types.ModuleType("qgis.PyQt.QtXml")

    class _QDomDocument:
        def setContent(self, *_a: object, **_k: object) -> bool:
            return True

    qtxml.QDomDocument = _QDomDocument
    pyqt.QtXml = qtxml

    # Need a QDomDocument stub accessible as qgis.PyQt.QtXml.QDomDocument.
    sys.modules["qgis.PyQt.QtXml"] = qtxml

    # Need a ReadWriteContext stub for the QGS Read context.
    core.QgsReadWriteContext = type("QgsReadWriteContext", (), {})  # type: ignore[attr-defined]

    sys.modules["qgis"] = qgis_pkg
    sys.modules["qgis.core"] = core
    sys.modules["qgis.PyQt"] = pyqt
    sys.modules["qgis.PyQt.QtCore"] = qtcore
    sys.modules["qgis.PyQt.QtWidgets"] = qtwidgets
