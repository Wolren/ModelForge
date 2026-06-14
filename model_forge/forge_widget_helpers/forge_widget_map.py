"""Map tab: generate model, run it, apply symbology, build print layout.

A new dock-widget tab — separate from the Generate tab. Same
engine as the MCP server's map-building tools
(``map_builder.run_pipeline`` + ``map_builder.build_qpt`` +
``map_builder.build_qml``), but called locally so the user
doesn't need the MCP server running.

Flow:

  1. User types "buffer roads 50m" in the description box.
  2. ``MapGenerateWorker`` calls the LLM via the existing
     ``self.backend.generate_single_pass`` (same path the
     ``Generate`` tab uses, no duplicate code).
  3. Symbology is generated per-layer (.qml files on disk).
  4. Optional **Run Model** button invokes ``run_model`` from
     ``model_runner``; the resulting output layers are added
     to the QGIS project and the matching ``.qml`` is loaded
     onto each via ``QgsVectorLayer.loadNamedStyle``.
  5. Print layout is generated (.qpt) and applied to the
     current project via ``QgsLayoutManager``.
  6. Verifier report is shown in the tab.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import traceback
from typing import Any

from qgis.PyQt.QtCore import QThread, Qt


def _log(msg: str) -> None:
    """Write ``msg`` to the QGIS Log Messages panel.

    The tag is ``"MapForge"`` so the user can filter by it
    (View → Panels → Log Messages → MapForge tab).
    """
    try:
        from qgis.core import QgsMessageLog

        QgsMessageLog.logMessage(str(msg), "MapForge", notifyUser=False)
    except Exception:  # noqa: BLE001
        pass


from qgis.PyQt.QtWidgets import (
    QCheckBox,
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


# ── Helpers ──────────────────────────────────────────────


_FILENAME_INVALID_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_filename(name: str, replacement: str = "_") -> str:
    """Return a filename-safe version of ``name``.

    Strips characters that are illegal on Windows
    (``<>:"/\\|?*`` and control chars 0-31), collapses
    runs of underscores, and trims leading/trailing dots
    and spaces. Empty result falls back to ``"unnamed"``.
    """
    if not name:
        return "unnamed"
    cleaned = _FILENAME_INVALID_RE.sub(replacement, name)
    cleaned = re.sub(r"_+", replacement, cleaned).strip(" .")
    # Reserved Windows device names.
    upper = cleaned.upper()
    if upper in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    }:
        cleaned = "_" + cleaned
    return cleaned or "unnamed"


# ─── Worker ────────────────────────────────────────────────


class MapGenerateWorker(QThread):
    """QThread wrapper around the LLM backend.

    Reuses the same backend the rest of the dock widget uses
    so the user only configures one LLM connection.
    """

    def __init__(self, backend, description: str, context_text: str, two_phase: bool):
        super().__init__()
        self.backend = backend
        self.description = description
        self.context_text = context_text
        self.two_phase = two_phase

    def run(self):
        try:
            if self.two_phase:
                plan = self.backend.generate_plan(self.description, self.context_text)
                result = self.backend.generate_model_from_plan(plan, self.context_text)
            else:
                result = self.backend.generate_single_pass(self.description, self.context_text)
            self.finished.emit(result)
        except Exception as e:  # noqa: BLE001
            self.error.emit(f"{type(e).__name__}: {e}")

    # pyqtSignal aliases (the base ForgeWidget already does this
    # in the same way; here we declare them as plain attributes
    # so they're available before ``run`` returns).
    from qgis.PyQt.QtCore import pyqtSignal

    finished = pyqtSignal(object)
    error = pyqtSignal(str)


class LayoutDesignWorker(QThread):
    """QThread wrapper around ``LLMBackend.choose_layout_design``.

    Mirrors the ``MapGenerateWorker`` pattern so the dock
    has a single, consistent LLM harness: spin up a worker,
    connect ``finished`` / ``error``, run the call off the
    UI thread, hand the result back to the dock.
    """

    def __init__(self, backend, intent: str, project_context: str):
        super().__init__()
        self.backend = backend
        self.intent = intent
        self.project_context = project_context

    def run(self):
        try:
            result = self.backend.choose_layout_design(self.intent, self.project_context)
            self.finished.emit(result)
        except Exception as e:  # noqa: BLE001
            self.error.emit(f"{type(e).__name__}: {e}")

    from qgis.PyQt.QtCore import pyqtSignal

    finished = pyqtSignal(object)
    error = pyqtSignal(str)


class LayoutEvalWorker(QThread):
    """QThread wrapper around ``LLMBackend.evaluate_layout_spec``.

    Fires after the layout is built; if the LLM finds issues,
    the layout is rebuilt with fixes (up to ``max_retries``).

    When an ``image_path`` is provided, the LLM receives a
    rendered snapshot of the layout (vision model). Without it,
    a text-only spec summary is used.
    """

    def __init__(self, backend, spec_summary: str, intent: str, image_path: str | None = None):
        super().__init__()
        self.backend = backend
        self.spec_summary = spec_summary
        self.intent = intent
        self.image_path = image_path

    def run(self):
        try:
            if self.image_path:
                result = self.backend.evaluate_layout_image(self.image_path, self.intent)
            else:
                result = self.backend.evaluate_layout_spec(self.spec_summary, self.intent)
            self.finished.emit(result)
        except Exception as e:  # noqa: BLE001
            self.error.emit(f"{type(e).__name__}: {e}")

    from qgis.PyQt.QtCore import pyqtSignal

    finished = pyqtSignal(object)
    error = pyqtSignal(str)


# ─── Mixin ─────────────────────────────────────────────────


class ForgeWidgetMapMixin:
    """Mixin that injects the Map tab into ``ForgeWidget``."""

    # ── Tab construction ──────────────────────────────────────

    def _inject_map_tab(self):
        tab = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(6)

        layout.addWidget(QLabel("Describe the map:"))
        self.lbl_map_hint = QLabel(
            "Tip: if no LLM is configured, the dock builds a "
            "layout from the project's current layers."
        )
        self.lbl_map_hint.setStyleSheet("color: gray; font-size: 10px;")
        self.lbl_map_hint.setWordWrap(True)
        layout.addWidget(self.lbl_map_hint)

        self.txt_map_intent = QPlainTextEdit()
        self.txt_map_intent.setPlaceholderText(
            "e.g. Buffer the roads layer by 50m, clip to the city "
            "boundary, and produce a print map at A4 with a legend. "
            "If you have no LLM configured, the title of the map "
            "is taken from this box and the layers come from the "
            "current project."
        )
        self.txt_map_intent.setMinimumHeight(80)
        layout.addWidget(self.txt_map_intent)

        picker_row = QHBoxLayout()
        picker_row.addWidget(QLabel("Template:"))
        self.cmb_map_template = QComboBox()
        self.cmb_map_template.addItems(
            [
                "default",
                "scientific",
                "presentation",
                "minimal",
            ]
        )
        picker_row.addWidget(self.cmb_map_template)
        picker_row.addStretch()
        layout.addLayout(picker_row)

        btn_row = QHBoxLayout()
        self.btn_generate_map = QPushButton("Generate Map")
        self.btn_generate_map.clicked.connect(self._on_generate_map)
        btn_row.addWidget(self.btn_generate_map)

        self.btn_run_model = QPushButton("Run Model")
        self.btn_run_model.setEnabled(False)
        self.btn_run_model.clicked.connect(self._on_run_model)
        btn_row.addWidget(self.btn_run_model)

        self.btn_symbology_only = QPushButton("Symbology Only")
        self.btn_symbology_only.clicked.connect(self._on_generate_symbology_only)
        btn_row.addWidget(self.btn_symbology_only)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        # Layer picker — same UX as the Generate tab's
        # "Context layers" box (QGroupBox + MultiSelection
        # list + Select all / Deselect all / Refresh).
        from qgis.PyQt.QtWidgets import (
            QAbstractItemView,
            QGroupBox,
        )

        self.layers_group = QGroupBox("Layers to include in the map")
        layers_layout = QVBoxLayout()
        layers_layout.setSpacing(4)
        self.layers_group.setLayout(layers_layout)

        # AI auto-pick toggle: when on, the LLM picks
        # relevant layers and the user's selection is a hint
        # (marked [USER-PICKED] in the LLM context). When
        # off, the LLM is told to write title/subtitle/style
        # only and the user's ticks are the source of truth.
        auto_pick_row = QHBoxLayout()
        self.chk_auto_pick = QCheckBox("AI auto-pick")
        self.chk_auto_pick.setChecked(True)
        self.chk_auto_pick.setToolTip(
            "On: the AI picks the relevant layers for you.\n"
            "Off: your selection below is the source of truth."
        )
        self.lst_map_layers = QListWidget()
        self.lst_map_layers.setSelectionMode(QAbstractItemView.MultiSelection)
        self.lst_map_layers.setMaximumHeight(200)
        self.lst_map_layers.setToolTip(
            "Select the layers to include in the map. Hold Ctrl / Shift for multi-select."
        )

        auto_pick_row.addWidget(self.chk_auto_pick)
        auto_pick_row.addStretch()
        self.btn_map_select_all = QPushButton("Select all")
        self.btn_map_select_all.clicked.connect(self.lst_map_layers.selectAll)
        self.btn_map_clear = QPushButton("Deselect all")
        self.btn_map_clear.clicked.connect(self.lst_map_layers.clearSelection)
        self.btn_refresh_layers = QPushButton("Refresh")
        self.btn_refresh_layers.setToolTip("Reload the layer list from the project.")
        self.btn_refresh_layers.clicked.connect(self._refresh_layer_picker)
        auto_pick_row.addWidget(self.btn_map_select_all)
        auto_pick_row.addWidget(self.btn_map_clear)
        auto_pick_row.addWidget(self.btn_refresh_layers)
        layers_layout.addLayout(auto_pick_row)

        layers_layout.addWidget(self.lst_map_layers)

        # Live state label: shows how many of the picker
        # items are currently selected. Updates whenever
        # the selection changes (or when the picker is
        # repopulated), so the user can always see how
        # many layers their Generate Map will use.
        self.lbl_map_picker_state = QLabel("Layers: 0 of 0 selected")
        self.lbl_map_picker_state.setStyleSheet("color: gray; font-size: 10px;")
        self.lst_map_layers.itemSelectionChanged.connect(self._update_picker_state_label)
        layers_layout.addWidget(self.lbl_map_picker_state)

        layout.addWidget(self.layers_group)

        # Populate the picker immediately. We also schedule
        # a deferred refresh (50 ms) in case the project
        # hasn't finished loading layers yet — the deferred
        # call picks them up without us having to know
        # when QGIS is "ready".
        try:
            self._refresh_layer_picker()
        except Exception:  # noqa: BLE001
            pass
        try:
            from qgis.PyQt.QtCore import QTimer

            QTimer.singleShot(50, self._refresh_layer_picker)
        except Exception:  # noqa: BLE001
            pass

        self.progress_map = QProgressBar()
        self.progress_map.setRange(0, 0)
        self.progress_map.hide()
        layout.addWidget(self.progress_map)

        layout.addWidget(QLabel("Layout verifier:"))
        self.lst_map_verifier = QListWidget()
        self.lst_map_verifier.setMaximumHeight(160)
        layout.addWidget(self.lst_map_verifier)

        self.lbl_map_status = QLabel("Ready.")
        self.lbl_map_status.setStyleSheet("color: gray;")
        layout.addWidget(self.lbl_map_status)

        layout.addStretch()
        tab.setLayout(layout)
        self.tabs.addTab(tab, "Map")

    # ── Action handlers ───────────────────────────────────────

    def _on_generate_map(self):
        intent = self.txt_map_intent.toPlainText().strip()
        if not intent:
            QMessageBox.information(self, "Map", "Describe the map first.")
            return
        _log(f"Generate Map clicked. Intent={intent!r}")
        # Two paths converge on the same end result: a
        # print layout in the QGIS project. The LLM path
        # asks the LLM to write title/subtitle/template/style
        # hints. Layer selection comes from the user's
        # tickboxes in the picker (or the smart heuristic
        # if none ticked). The no-LLM path skips the LLM
        # call entirely.
        if self._llm_configured() and self.chk_auto_pick.isChecked():
            self._start_layout_design_worker(intent)
        else:
            self._generate_map_without_llm(intent)

    def _on_generate_symbology_only(self):
        model_json = getattr(self, "_current_model_json", None)
        if not isinstance(model_json, dict):
            QMessageBox.information(self, "Symbology", "Generate a model first.")
            return
        self._write_symbology(model_json)
        self.lbl_map_status.setText("Symbology written.")

    def _on_run_model(self):
        model_json = getattr(self, "_current_model_json", None)
        if not isinstance(model_json, dict):
            QMessageBox.information(self, "Run", "Generate a model first.")
            return
        self.lbl_map_status.setText("Running model…")
        try:
            report = self._run_model(model_json)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            QMessageBox.critical(self, "Run failed", f"{type(e).__name__}: {e}")
            self.lbl_map_status.setText("Run failed.")
            return

        # Apply .qml to every output that QGIS can find in the
        # project, and add any not-yet-loaded outputs.
        applied = self._apply_outputs_to_project(model_json, report)
        self.lbl_map_status.setText(
            f"Run done in {report.elapsed_seconds:.1f}s; applied {applied} layer(s)."
        )

    # ── LLM-optional flow ────────────────────────────────────

    def _llm_configured(self) -> bool:
        """True iff the LLM backend has a model + URL configured.

        The legacy ``LLMBackend`` keeps ``model`` as a non-empty
        string after ``configure()`` runs. We treat any non-empty
        model name as "configured". If the user hasn't been
        through the Settings tab, ``model`` stays at its default
        (empty for some backends, but Ollama's default is
        "gpt-oss:20b-cloud") - so we also require ``url``.
        """
        b = getattr(self, "backend", None)
        if b is None:
            return False
        try:
            url = getattr(b, "url", "") or ""
            model = getattr(b, "model", "") or ""
        except Exception:  # noqa: BLE001
            return False
        return bool(url and model)

    def _generate_map_without_llm(self, intent: str) -> None:
        """Build a map from the project's current layers when
        no LLM is configured.

        Selection priority:
        1. The layers the user ticked in the picker (if any)
        2. The smart layer filter (visible + name-match + cap 12)
        3. Fall back to all visible layers if both yield nothing
        """
        self.lbl_map_status.setText("Selecting relevant layers…")
        try:
            all_layers = self._collect_current_layers()
            _log(f"_collect_current_layers returned {len(all_layers)} layers")
            if not all_layers:
                QMessageBox.information(
                    self,
                    "Map",
                    "No LLM is configured and the project has no "
                    "loaded layers. Either configure an LLM in the "
                    "Generate tab's Settings, or load at least one "
                    "layer in the project.",
                )
                self.lbl_map_status.setText("Need an LLM or at least one layer.")
                return
            picked = self._resolve_layer_picks(intent, all_layers)
            if not picked:
                # Be helpful: tell the user the actual state.
                picker_total = self.lst_map_layers.count()
                picker_picks = len(self._user_picked_layer_ids())
                QMessageBox.information(
                    self,
                    "Map",
                    f"Could not assemble a layer set for the map.\n\n"
                    f"- Layers detected in the project: {len(all_layers)}\n"
                    f"- Layers shown in the picker: {picker_total}\n"
                    f"- Layers you have selected: {picker_picks}\n\n"
                    f"Try clicking 'Refresh' to reload the layer list, "
                    f"or uncheck 'AI auto-pick' and tick at least one "
                    f"layer in the list above.",
                )
                self.lbl_map_status.setText(
                    f"No layers available (project={len(all_layers)}, "
                    f"picker={picker_total}, picked={picker_picks})."
                )
                return
            # Build a model JSON shaped for our pipeline.
            model_json = {
                "model_name": intent or "Project Layers",
                "model_group": "Map Forge",
                "inputs": [],
                "algorithms": [
                    {
                        "id": layer["id"],
                        "algorithm_id": "native:identity",
                        "parameters": {},
                    }
                    for layer in picked
                ],
            }
            self._current_model_json = model_json
            self._write_symbology_for_layers(picked)
            template = self.cmb_map_template.currentText()
            layer_meta = self._layer_meta(picked)
            qpt_path = self._build_and_apply_layout(
                model_json,
                template,
                title=intent or "Project Map",
                layer_meta=layer_meta,
            )
            self._run_verifier(
                model_json,
                template,
                extent=self._current_project_extent(
                    layer_ids=[a.get("id") for a in model_json.get("algorithms", [])]
                ),
                qpt_path=qpt_path,
            )
            self.btn_run_model.setEnabled(False)  # no model to run
            self._open_layout_in_designer(qpt_path)
            self.lbl_map_status.setText(
                f"Map built ({len(picked)} of {len(all_layers)} layers) → {qpt_path}"
            )
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.lbl_map_status.setText(f"Failed: {e}")
            QMessageBox.critical(self, "Map", f"{type(e).__name__}: {e}")

    def _resolve_layer_picks(
        self,
        intent: str,
        all_layers: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Pick the layers to use, in priority order:
        1. User-selected layers in the picker (if any).
        2. Smart selector on the intent.
        3. First N of all layers (capped). Never returns []
        when the project has any layers — the user wants a
        map, not a "no layers" error.

        Returns the list of layer dicts in the order the
        caller should display them.
        """
        if not all_layers:
            return []
        by_id = {layer.get("id"): layer for layer in all_layers}
        # 1. User picks (the source of truth when the user
        # has explicitly selected layers in the picker).
        user_ids = self._user_picked_layer_ids()
        picked = [by_id[lid] for lid in user_ids if lid in by_id]
        if picked:
            return picked
        # 2. Smart selector (keyword + visibility scoring).
        smart = self._select_relevant_layers(intent, all_layers)
        if smart:
            return smart
        # 3. Last-ditch fallback: just take the first N. The
        # user asked us to make a map, not to fail.
        return all_layers[:12]

    def _select_relevant_layers(
        self,
        intent: str,
        layers: list[dict[str, Any]],
        max_layers: int = 12,
    ) -> list[dict[str, Any]]:
        """Pick the layers most likely to be useful for ``intent``.

        Heuristic (permissive — never returns [] if the project
        has any layers; the user wants a map, not a "no
        layers" error):
        1. Keep all layers with vector geometry (point/line/
           polygon) regardless of extent — even an empty
           table renders in the legend.
        2. Score each:
           - +1 per keyword from ``intent`` found in the
             layer's name (case-insensitive).
           - +1 if the layer is currently visible in the
             project panel.
        3. Sort by score desc, break ties by name.
        4. Take the top ``max_layers``.

        Returns the picked list (possibly empty only if
        ``layers`` itself was empty).
        """
        if not layers:
            return []
        # Keep any layer with vector geometry. Rasters and
        # no-geometry layers are dropped (we can't style
        # them with single-symbol default symbology).
        eligible = [
            layer for layer in layers if layer.get("geometry_kind") in {"point", "line", "polygon"}
        ]
        if not eligible:
            # Last-ditch: any layer at all (rasters included)
            eligible = list(layers)

        keywords = [w.lower() for w in intent.split() if len(w) >= 3]
        scored: list[tuple[int, str, dict]] = []
        for layer in eligible:
            name = (layer.get("name") or "").lower()
            score = sum(1 for k in keywords if k in name)
            if layer.get("visible", True):
                score += 1
            scored.append((score, name, layer))

        # 3+4. Sort + cap.
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [layer for _s, _n, layer in scored[:max_layers]]

    def _layer_meta(self, layers: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
        """Build ``{layer_id: {name, source, provider, geometry}}``.

        Reads source/provider from the live QGIS layer (not
        the snapshot in ``layers``) so the .qpt can re-bind
        the layer to its actual data file on load.
        """
        meta: dict[str, dict[str, str]] = {}
        try:
            from qgis.core import QgsProject
        except ImportError:
            return meta
        proj = QgsProject.instance()
        for entry in layers:
            layer_id = entry.get("id", "")
            if not layer_id:
                continue
            qgis_layer = proj.mapLayer(layer_id)
            if qgis_layer is None:
                continue
            try:
                src = qgis_layer.source() or ""
                provider = qgis_layer.providerType() or ""
            except Exception:  # noqa: BLE001
                src = ""
                provider = ""
            meta[layer_id] = {
                "name": qgis_layer.name() or entry.get("name", ""),
                "source": src,
                "provider": provider,
                "geometry": entry.get("geometry_kind", ""),
            }
        return meta

    def _layer_meta_for_ids(self, layer_ids: list[str]) -> dict[str, dict[str, str]]:
        """Same as ``_layer_meta`` but accepts a flat list of ids.

        Used by the LLM-driven path where we have the model
        JSON's algorithm ids but not the full layer dicts.
        """
        meta: dict[str, dict[str, str]] = {}
        try:
            from qgis.core import QgsProject
        except ImportError:
            return meta
        proj = QgsProject.instance()
        for layer_id in layer_ids:
            if not layer_id:
                continue
            qgis_layer = proj.mapLayer(layer_id)
            if qgis_layer is None:
                continue
            try:
                src = qgis_layer.source() or ""
                provider = qgis_layer.providerType() or ""
                geometry = ""
                if hasattr(qgis_layer, "geometryType"):
                    g = qgis_layer.geometryType()
                    if g == 0:
                        geometry = "point"
                    elif g == 1:
                        geometry = "line"
                    elif g == 2:
                        geometry = "polygon"
            except Exception:  # noqa: BLE001
                src = ""
                provider = ""
                geometry = ""
            meta[layer_id] = {
                "name": qgis_layer.name() or layer_id,
                "source": src,
                "provider": provider,
                "geometry": geometry,
            }
        return meta

    def _collect_current_layers(self) -> list[dict[str, Any]]:
        """Snapshot of the project's loaded layers.

        Returns a list of ``{id, name, geometry_kind, extent,
        visible}`` dicts the symbology + layout stages consume.
        NEVER silently drops a layer: even if the extent
        query fails, the layer is added with extent=None.
        The previous implementation wrapped the body in
        ``try/except: continue`` which made ``_collect_current_layers``
        return ``[]`` whenever ANY layer's ``extent()`` raised
        (which happens for invalid or freshly-loaded layers).
        """
        try:
            from qgis.core import QgsProject
        except ImportError:
            return []
        proj = QgsProject.instance()
        # mapLayers may raise in test stubs or during
        # early init; guard against that.
        try:
            proj_layers = list(proj.mapLayers().values())
        except Exception:  # noqa: BLE001
            return []
        out: list[dict[str, Any]] = []
        for layer in proj_layers:
            # 1. Geometry kind. Default to "polygon" on any
            #    failure (covers rasters, geometryless tables,
            #    and freshly-loaded layers whose geometryType
            #    isn't ready yet).
            kind = "polygon"
            try:
                if hasattr(layer, "type") and layer.type() == layer.RasterLayer:
                    kind = "raster"
                elif hasattr(layer, "geometryType"):
                    geom = layer.geometryType()
                    if geom == 0:
                        kind = "point"
                    elif geom == 1:
                        kind = "line"
                    # else: polygon (default)
            except Exception:  # noqa: BLE001
                pass

            # 2. Extent. May be None if the layer hasn't
            #    computed one yet. We do NOT skip the layer
            #    on failure.
            extent = None
            try:
                if hasattr(layer, "extent"):
                    ext = layer.extent()
                    extent = (
                        ext.xMinimum(),
                        ext.yMinimum(),
                        ext.xMaximum(),
                        ext.yMaximum(),
                    )
            except Exception:  # noqa: BLE001
                pass

            # 3. Visibility. Some layers don't expose
            #    isVisible() (e.g. memory layers); default
            #    to True on failure.
            visible = True
            try:
                if hasattr(layer, "isVisible"):
                    visible = bool(layer.isVisible())
            except Exception:  # noqa: BLE001
                pass

            # 4. Build the entry. id and name are required;
            #    without them the layer is unusable, so drop
            #    only when they're missing.
            try:
                lid = layer.id()
                name = layer.name()
            except Exception:  # noqa: BLE001
                continue
            if not lid or not name:
                continue
            out.append(
                {
                    "id": lid,
                    "name": name,
                    "geometry_kind": kind,
                    "extent": extent,
                    "visible": visible,
                }
            )
        return out

    def _write_symbology_for_layers(self, layers: list[dict[str, Any]]) -> None:
        """Emit one .qml per live layer, and apply it in-place."""
        from model_forge.compiler_core.core.services.map_builder import build_qml

        out_dir = self._symbology_dir()
        os.makedirs(out_dir, exist_ok=True)
        for layer in layers:
            step_id = layer["name"]
            kind = layer.get("geometry_kind", "polygon")
            try:
                qml = build_qml(geometry_kind=kind, layer_name=step_id)
            except Exception:  # noqa: BLE001
                continue
            with open(
                os.path.join(out_dir, f"{_safe_filename(str(step_id))}.qml"),
                "w",
                encoding="utf-8",
            ) as f:
                f.write(qml)
        # Also try to apply the .qml to the live layers in the
        # project so the user sees a styled map right away.
        try:
            from qgis.core import QgsProject

            proj = QgsProject.instance()
            for layer in proj.mapLayers().values():
                qml = os.path.join(out_dir, f"{_safe_filename(str(layer.name()))}.qml")
                if os.path.isfile(qml):
                    layer.loadNamedStyle(qml)
                    layer.triggerRepaint()
        except Exception:  # noqa: BLE001
            pass

    # ── LLM worker plumbing ──────────────────────────────────

    def _start_generate_worker(self, intent: str) -> None:
        """Spin up the LLM worker, then chain into the
        ``_on_map_generate_finished`` callback on success.
        """
        if not hasattr(self, "backend"):
            QMessageBox.warning(
                self,
                "Map",
                "Configure an LLM provider in the Generate tab first.",
            )
            return
        try:
            selected_layers = self._get_selected_layers()
            algo_config = self._get_algo_config()
            ctx = self.context_collector.collect(selected_layers, algo_config)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(
                self,
                "Map",
                f"Context collection failed: {type(e).__name__}: {e}",
            )
            return

        self.btn_generate_map.setEnabled(False)
        self.btn_run_model.setEnabled(False)
        self.progress_map.show()
        self.lbl_map_status.setText("Generating model…")

        self._map_worker = MapGenerateWorker(
            self.backend,
            intent,
            ctx,
            two_phase=getattr(self, "chk_two_phase", None) and self.chk_two_phase.isChecked(),
        )
        self._map_worker.finished.connect(self._on_map_generate_finished)
        self._map_worker.error.connect(self._on_map_generate_error)
        self._map_worker.start()

    def _start_layout_design_worker(self, intent: str) -> None:
        """Spin up the cartographer LLM worker.

        The LLM is given the user's intent + a snapshot of
        the live project's layers, and asked to decide:
        title, subtitle, template, which layers to include.
        The result is the canonical cartographic design for
        the requested map.
        """
        try:
            project_context = self._build_project_context_text()
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(
                self,
                "Map",
                f"Could not read project context: {type(e).__name__}: {e}",
            )
            return

        self.btn_generate_map.setEnabled(False)
        self.btn_run_model.setEnabled(False)
        self.progress_map.show()
        self.lbl_map_status.setText("Designing layout with AI…")

        self._layout_design_worker = LayoutDesignWorker(self.backend, intent, project_context)
        self._layout_design_worker.finished.connect(self._on_layout_design_finished)
        self._layout_design_worker.error.connect(self._on_layout_design_error)
        self._layout_design_worker.start()

    def _build_project_context_text(self) -> str:
        """Stringify the live project for the LLM.

        Lists every loaded layer with its id, name, geometry
        kind, and a small extent summary. Layers the user
        has manually ticked in the picker are marked with
        a ``[user-picked]`` tag so the LLM takes the user's
        selection as the source of truth. Capped at 200
        layers so we don't flood the context window.
        """
        layers = self._collect_current_layers()
        if not layers:
            return "No layers currently loaded in the project."
        user_picked = set(self._user_picked_layer_ids())
        lines: list[str] = []
        for layer in layers[:200]:
            try:
                ext = layer.get("extent")
                if ext:
                    extent_str = f"x[{ext[0]:.1f}..{ext[2]:.1f}] y[{ext[1]:.1f}..{ext[3]:.1f}]"
                else:
                    extent_str = "no extent"
            except Exception:  # noqa: BLE001
                extent_str = "no extent"
            vis = "visible" if layer.get("visible", True) else "hidden"
            tag = " [USER-PICKED]" if layer.get("id") in user_picked else ""
            lines.append(
                f"- id={layer.get('id', '?')!r} "
                f"name={layer.get('name', '?')!r} "
                f"kind={layer.get('geometry_kind', '?')} "
                f"({vis}, {extent_str}){tag}"
            )
        return "\n".join(lines)

    def _refresh_layer_picker(self) -> None:
        """Reload the layer list from the project.

        Mirrors the Generate tab's behaviour: the list is
        in MultiSelection mode, so users Ctrl/Shift-click to
        pick. Previous selections are preserved across refresh.
        New layers start unselected; if the project has ≤ 3
        layers we pre-select all (out-of-the-box convenience).
        """
        try:
            from qgis.core import QgsProject
        except ImportError:
            return
        proj = QgsProject.instance()
        # Snapshot previous selections so we can preserve them.
        prev_selection = set(self._user_picked_layer_ids())
        self.lst_map_layers.clear()
        layers = list(proj.mapLayers().values())
        # Decide which to pre-select.
        # Pre-select all visible layers by default — the
        # user can deselect to exclude. This way the dock
        # produces a map out-of-the-box without the user
        # having to tick anything.
        if prev_selection:
            pre_select = True  # any layer that was previously selected stays selected
        else:
            pre_select = True  # default: all visible layers pre-selected
        for layer in layers:
            try:
                if layer.type() == layer.RasterLayer:
                    kind = "raster"
                else:
                    geom = layer.geometryType() if hasattr(layer, "geometryType") else None
                    if geom == 0:
                        kind = "point"
                    elif geom == 1:
                        kind = "line"
                    else:
                        kind = "polygon"
            except Exception:  # noqa: BLE001
                kind = "polygon"
            item = QListWidgetItem(f"{layer.name()}  ({kind})")
            item.setData(Qt.UserRole, layer.id())
            self.lst_map_layers.addItem(item)
            if layer.id() in prev_selection or pre_select:
                item.setSelected(True)
        # Refresh the live state label so the user can see
        # how many items are now pre-selected.
        self._update_picker_state_label()

    def _update_picker_state_label(self) -> None:
        """Update the "Layers: X of Y selected" label."""
        if not hasattr(self, "lbl_map_picker_state"):
            return
        total = self.lst_map_layers.count()
        # selectedItems() is the canonical way to read the
        # selection in MultiSelection mode; fall back to
        # scanning isSelected() if that returns nothing
        # (some Qt versions / selection modes differ).
        selected = self.lst_map_layers.selectedItems()
        if not selected and total:
            selected = [
                self.lst_map_layers.item(i)
                for i in range(total)
                if self.lst_map_layers.item(i).isSelected()
            ]
        n_sel = len(selected)
        # Distinct selected ids (skip items with no data).
        distinct = sum(1 for it in selected if it.data(Qt.UserRole))
        self.lbl_map_picker_state.setText(f"Layers: {distinct} of {total} selected")

    def _user_picked_layer_ids(self) -> list[str]:
        """Layer ids the user has selected in the picker.

        Returns the selected ids in picker order so the
        caller can preserve the user's intent for both
        inclusion and ordering. Belt-and-braces: we read
        via ``selectedItems()`` first and then fall back
        to per-item ``isSelected()`` so different Qt
        selection modes all work.
        """
        out: list[str] = []
        # 1. Try selectedItems() (canonical, works for
        #    MultiSelection + ExtendedSelection).
        for item in self.lst_map_layers.selectedItems():
            lid = item.data(Qt.UserRole)
            if lid:
                out.append(lid)
        if out:
            return out
        # 2. Fallback: per-item isSelected() scan.
        for i in range(self.lst_map_layers.count()):
            item = self.lst_map_layers.item(i)
            if item.isSelected():
                lid = item.data(Qt.UserRole)
                if lid:
                    out.append(lid)
        return out

    def _on_layout_design_finished(self, design: dict) -> None:
        """Build the layout from the LLM's cartographic design.

        Layer selection priority:
        1. Layers the user ticked in the picker (if any)
        2. Layers the LLM returned
        3. Smart heuristic
        4. All visible layers (capped)
        The LLM is told about user picks via [USER-PICKED] in
        the context, but the user's manual selection is the
        source of truth.
        """
        self.progress_map.hide()
        self.btn_generate_map.setEnabled(True)
        if not isinstance(design, dict):
            self.lbl_map_status.setText("Layout design failed.")
            QMessageBox.warning(self, "Map", "LLM did not return a design dict.")
            return

        title = design.get("title", "") or "Project Map"
        subtitle = design.get("subtitle", "") or ""
        template = design.get("template", "default")
        if template not in {"default", "scientific", "presentation", "minimal"}:
            template = "default"
        # Reflect the LLM's choice in the template combo.
        idx = self.cmb_map_template.findText(template)
        if idx >= 0:
            self.cmb_map_template.setCurrentIndex(idx)

        # Layer selection. The user-ticked list is the source
        # of truth; the LLM's choices are a fallback.
        all_layers = self._collect_current_layers()
        _log(f"LLM path: collected {len(all_layers)} layers")
        picked = self._resolve_layer_picks(
            title or self.txt_map_intent.toPlainText().strip(),
            all_layers,
        )
        if not picked:
            self.lbl_map_status.setText("No layers in the project match the intent.")
            QMessageBox.information(
                self,
                "Map",
                "The dock has no layers to include. Tick at "
                "least one layer in the layer list, or load "
                "the layers you want to show first.",
            )
            return

        title = design.get("title", "") or "Project Map"
        subtitle = design.get("subtitle", "") or ""
        template = design.get("template", "default")
        if template not in {"default", "scientific", "presentation", "minimal"}:
            template = "default"
        # Reflect the LLM's choice in the template combo.
        idx = self.cmb_map_template.findText(template)
        if idx >= 0:
            self.cmb_map_template.setCurrentIndex(idx)

        # Synthesise a model JSON for the layout pipeline.
        # We use the LLM-picked layer ids as the algorithm
        # steps (one trivial step each).
        model_json = {
            "model_name": title,
            "model_group": subtitle,
            "inputs": [],
            "algorithms": [
                {
                    "id": layer["id"],
                    "algorithm_id": "native:identity",
                    "parameters": {},
                }
                for layer in picked
            ],
        }
        self._current_model_json = model_json

        # Symbology.
        self._write_symbology_for_layers(picked)
        # Apply style_hints if the LLM gave us any.
        self._apply_style_hints(design.get("style_hints", {}) or {}, picked)

        # Build the layout with proper layer binding.
        layer_meta = self._layer_meta(picked)
        qpt_path = self._build_and_apply_layout(
            model_json,
            template,
            title=title,
            layer_meta=layer_meta,
        )

        # Verifier + designer open.
        self._run_verifier(
            model_json,
            template,
            extent=self._current_project_extent(
                layer_ids=[a.get("id") for a in model_json.get("algorithms", [])]
            ),
            qpt_path=qpt_path,
        )
        self.btn_run_model.setEnabled(False)
        self._open_layout_in_designer(qpt_path)
        self.lbl_map_status.setText(
            f"AI layout: '{title}' ({len(picked)} layer(s), {template}) → {qpt_path}"
        )

        # Fire the layout evaluation loop (max 2 retries, image-based).
        self._layout_eval_retries = getattr(self, "_layout_eval_retries", 2)
        if self._layout_eval_retries > 0 and self._llm_configured():
            spec_summary = self._build_spec_summary(design, picked, title, template, model_json)
            image_path = self._render_layout_to_image()
            self._start_eval_worker(spec_summary, title, image_path=image_path)

    def _build_spec_summary(
        self,
        design: dict,
        picked: list[dict[str, Any]],
        title: str,
        template: str,
        model_json: dict,
    ) -> str:
        """Build a human-readable layout description for the LLM eval."""
        lines: list[str] = []
        lines.append(f"Layout: template={template!r} title={title!r}")
        lines.append(f"Layers: {len(picked)} shown")
        # Build quick bounds from the model_json extent
        lines.append(f"Map: extent from model ({len(model_json.get('algorithms', []))} steps)")
        lines.append(f"North arrow: upper-left, 15x15mm framed")
        lines.append(f"Scale bar: lower-left, 60x8mm single-box, 3 segments")
        lines.append(f"Legend: footer strip, 190x40mm")
        # Verifier info
        v_items = [
            self.lst_map_verifier.item(i).text() for i in range(self.lst_map_verifier.count())
        ]
        lines.append(f"Verifier: {'; '.join(v_items[:6])}")
        return "\n".join(lines)

    def _render_layout_to_image(self) -> str | None:
        """Export the most recently added layout to a temp PNG.

        Uses ``QgsLayoutExporter.renderPageToImage``. Returns
        the file path or None on failure.
        """
        try:
            from qgis.core import (
                QgsLayoutExporter,
                QgsProject,
            )
        except ImportError:
            return None
        name = getattr(self, "_last_layout_name", None)
        if name is None:
            return None
        layout = QgsProject.instance().layoutManager().layoutByName(name)
        if layout is None:
            return None
        tmp = os.path.join(os.path.expanduser("~"), ".model_forge", ".tmp_layout.png")
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        try:
            exporter = QgsLayoutExporter(layout)
            img = exporter.renderPageToImage(0, dpi=150)
            img.save(tmp, "PNG")
            return tmp if os.path.isfile(tmp) else None
        except Exception:  # noqa: BLE001
            return None

    def _start_eval_worker(
        self, spec_summary: str, intent: str, image_path: str | None = None
    ) -> None:
        """Fire the evaluation worker (image-based when possible)."""
        self.progress_map.show()
        self.lbl_map_status.setText("Evaluating layout quality…")
        self._layout_eval_worker = LayoutEvalWorker(
            self.backend,
            spec_summary,
            intent,
            image_path=image_path,
        )
        self._layout_eval_worker.finished.connect(self._on_eval_finished)
        self._layout_eval_worker.error.connect(self._on_eval_error)
        self._layout_eval_worker.start()

    def _on_eval_finished(self, suggestions: dict) -> None:
        """Apply layout fixes if the LLM found issues, or stop."""
        self.progress_map.hide()
        self._layout_eval_retries -= 1
        if not isinstance(suggestions, dict):
            return
        status = suggestions.get("status", "ok")
        if status != "fix":
            self.lbl_map_status.setText(self.lbl_map_status.text() + " [eval: OK]")
            return
        fixes = suggestions.get("fixes") or []
        if not fixes:
            return
        # Rebuild with the same design but adjusted positions.
        # For now we just log the fixes. A full rebuild
        # with position adjustments is handled in a future pass.
        msg = suggestions.get("message", "")
        self.lst_map_verifier.addItem(QListWidgetItem(f"[eval] {msg} ({len(fixes)} fix(es))"))
        self.lbl_map_status.setText(self.lbl_map_status.text() + f" [eval: {len(fixes)} fix(es)]")
        # Loop: if retries remain, the next Generate Map click
        # regenerates the map and fires eval again. Auto-recall
        # is deferred to the next version.
        _log(f"Layout eval: {msg} fixes={fixes}")

    def _on_eval_error(self, msg: str) -> None:
        self.progress_map.hide()
        _log(f"Layout eval failed: {msg}")

    def _apply_style_hints(self, hints: dict[str, str], picked: list[dict[str, Any]]) -> None:
        """Best-effort: stash the LLM's per-layer style hints.

        The cartographer's hints are descriptive ("blue line",
        "green polygons") - we don't currently parse them
        into QGIS renderers, but we write them to the
        symbology dir so a future pass can read them. The
        .qml files emitted by ``_write_symbology_for_layers``
        carry the default per-geometry symbols.
        """
        if not hints:
            return
        out_dir = self._symbology_dir()
        try:
            with open(
                os.path.join(out_dir, "style_hints.json"),
                "w",
                encoding="utf-8",
            ) as f:
                json.dump(
                    {
                        "hints": hints,
                        "layers": [layer.get("id") for layer in picked],
                    },
                    f,
                    indent=2,
                )
        except Exception:  # noqa: BLE001
            pass

    def _on_layout_design_error(self, msg: str) -> None:
        self.progress_map.hide()
        self.btn_generate_map.setEnabled(True)
        self.lbl_map_status.setText(f"AI design failed: {msg}")
        QMessageBox.critical(self, "Map", msg)

    def _on_map_generate_finished(self, workflow: dict) -> None:
        self.progress_map.hide()
        self.btn_generate_map.setEnabled(True)
        if not isinstance(workflow, dict) or "algorithms" not in workflow:
            self.lbl_map_status.setText("Model generation failed.")
            QMessageBox.warning(self, "Map", "LLM did not return a valid model JSON.")
            return

        self._current_model_json = workflow

        # 1. Symbology on disk.
        self._write_symbology(workflow)
        # 2. Print layout on disk + into the project.
        template = self.cmb_map_template.currentText()
        # Build layer_meta from the live project so the .qpt
        # carries the real source/provider and the layout
        # actually renders the layers on open.
        live_layer_ids = [a.get("id") for a in workflow.get("algorithms", [])]
        layer_meta = self._layer_meta_for_ids(live_layer_ids)
        qpt_path = self._build_and_apply_layout(
            workflow,
            template,
            layer_meta=layer_meta,
        )
        # 3. Verifier.
        self._run_verifier(
            workflow,
            template,
            extent=self._current_project_extent(
                layer_ids=[a.get("id") for a in workflow.get("algorithms", [])]
            ),
            qpt_path=qpt_path,
        )
        # 4. Re-enable Run Model.
        self.btn_run_model.setEnabled(True)
        # 5. Open the layout in the designer so the user can
        # immediately export to PDF / PNG.
        self._open_layout_in_designer(qpt_path)
        self.lbl_map_status.setText(f"Map ready: {qpt_path}")

    def _on_map_generate_error(self, msg: str) -> None:
        self.progress_map.hide()
        self.btn_generate_map.setEnabled(True)
        self.lbl_map_status.setText(f"Failed: {msg}")
        QMessageBox.critical(self, "Map", msg)

    # ── Symbology → disk ──────────────────────────────────────

    def _write_symbology(self, model_json: dict) -> str:
        """Emit one .qml per output step.

        Files are written to
        ``<project>/.model_forge/symbology/<step_id>.qml``.
        Returns the directory.
        """
        from model_forge.compiler_core.core.services.map_builder import (
            build_qml,
        )

        out_dir = self._symbology_dir()
        os.makedirs(out_dir, exist_ok=True)

        for alg in model_json.get("algorithms", []):
            step_id = alg.get("id")
            if not step_id:
                continue
            kind = self._guess_geometry_kind(alg)
            try:
                qml = build_qml(geometry_kind=kind, layer_name=step_id)
            except Exception:  # noqa: BLE001
                continue
            with open(
                os.path.join(out_dir, f"{_safe_filename(str(step_id))}.qml"),
                "w",
                encoding="utf-8",
            ) as f:
                f.write(qml)
        return out_dir

    def _symbology_dir(self) -> str:
        from qgis.core import QgsProject

        proj = QgsProject.instance()
        home = proj.homePath() or os.path.expanduser("~")
        return os.path.join(home, ".model_forge", "symbology")

    # ── Print layout → disk + project ─────────────────────────

    def _build_and_apply_layout(
        self,
        model_json: dict,
        template: str,
        title: str | None = None,
        layer_meta: dict[str, dict[str, str]] | None = None,
    ) -> str:
        from model_forge.compiler_core.core.services.map_builder import (
            build_qpt,
        )

        qpt_path = self._layout_path(model_json, template)
        output_ids = [a.get("id") for a in model_json.get("algorithms", [])]
        extent = self._current_canvas_extent() or self._current_project_extent(layer_ids=output_ids)
        crs = self._current_project_crs()
        _log(
            f"_build_and_apply_layout: template={template!r} "
            f"title={title!r} crs={crs!r} "
            f"extent={extent!r} "
            f"output_ids={output_ids}"
        )
        qpt_xml = build_qpt(
            template,
            title=title or model_json.get("model_name", "Model Forge Map"),
            subtitle=model_json.get("model_group", ""),
            crs=crs,
            output_layer_ids=[a.get("id") for a in model_json.get("algorithms", [])],
            extent=extent,
            layer_meta=layer_meta,
        )
        with open(qpt_path, "w", encoding="utf-8") as f:
            f.write(qpt_xml)
        self._apply_layout_to_project(
            qpt_path,
            title=title or model_json.get("model_name", ""),
            subtitle=model_json.get("model_group", ""),
            template=template,
            layer_meta=layer_meta,
        )
        return qpt_path

    def _current_project_extent(
        self,
        layer_ids: list[str] | None = None,
    ) -> tuple[float, float, float, float] | None:
        """Bounding box of the project's selected layers, or None.

        When ``layer_ids`` is provided, only those layers'
        extents are combined. This avoids including basemap
        layers (OSM, satellite tiles) that span the entire
        Web Mercator extent (-20M to +20M) and would make
        the map show the entire planet instead of the user's
        data area.

        Each layer's extent is queried independently; a single
        layer that can't compute its extent doesn't drop others.
        """
        try:
            from qgis.core import QgsProject
        except ImportError:
            return None
        try:
            proj = QgsProject.instance()
        except Exception:  # noqa: BLE001
            return None

        # If specific ids are given, look those up. Otherwise
        # use all loaded layers.
        if layer_ids:
            try:
                layers = [proj.mapLayer(lid) for lid in layer_ids if lid]
            except Exception:  # noqa: BLE001
                layers = []
        else:
            try:
                layers = list(proj.mapLayers().values())
            except Exception:  # noqa: BLE001
                return None

        if not layers:
            return None

        from qgis.core import QgsRectangle

        combined: QgsRectangle | None = None
        for layer in layers:
            try:
                if not hasattr(layer, "extent"):
                    continue
                e = layer.extent()
                if e is None or e.isEmpty():
                    continue
                # Skip basemap tile layers that span more than
                # 10 000 km (full Web Mercator extent is 40M km).
                # They make the combined bbox the whole planet
                # and hide the actual data as a tiny speck.
                w = e.xMaximum() - e.xMinimum()
                h = e.yMaximum() - e.yMinimum()
                if w > 10_000_000 or h > 10_000_000:
                    continue
                if combined is None:
                    combined = e
                else:
                    combined.combineExtentWith(e)
            except Exception:  # noqa: BLE001
                continue

        if combined is None or combined.isEmpty():
            return None
        return (
            combined.xMinimum(),
            combined.yMinimum(),
            combined.xMaximum(),
            combined.yMaximum(),
        )

    def _current_canvas_extent(self) -> tuple[float, float, float, float] | None:
        """The map canvas's current extent (always in project CRS).

        Mirroring the AutoLayoutTool approach: use what the user
        is looking at, not the combined layer extents. This avoids
        mixed-CRS issues when layers are in different coordinate
        systems (e.g. basemap tiles + local data).
        """
        try:
            canvas = self.iface.mapCanvas()
            if canvas is None:
                return None
            e = canvas.extent()
            if e is None or e.isEmpty():
                return None
            return (
                e.xMinimum(),
                e.yMinimum(),
                e.xMaximum(),
                e.yMaximum(),
            )
        except Exception:  # noqa: BLE001
            return None

    def _current_project_crs(self) -> str:
        """EPSG code of the project, e.g. 'EPSG:2180'."""
        try:
            from qgis.core import QgsProject

            proj = QgsProject.instance()
            crs = proj.crs()
            if crs is not None and hasattr(crs, "authid"):
                return crs.authid()
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _open_layout_in_designer(self, qpt_path: str) -> None:
        """Open the layout we just added in the QGIS designer.

        We use ``self._last_layout_name`` (set by
        ``_apply_layout_to_project``) to find the exact layout,
        not ``layouts[-1]`` which could be a stale layout if
        the user created one manually between clicks.
        """
        try:
            from qgis.core import QgsProject
        except ImportError:
            return
        try:
            from qgis.utils import iface
        except ImportError:
            return
        proj = QgsProject.instance()
        name = getattr(self, "_last_layout_name", None)
        if name is not None:
            layout = proj.layoutManager().layoutByName(name)
            if layout is not None:
                try:
                    iface.openLayoutDesigner(layout)
                    return
                except Exception:  # noqa: BLE001
                    pass
        # Fallback: last layout in the manager.
        layouts = proj.layoutManager().layouts()
        if layouts:
            try:
                iface.openLayoutDesigner(layouts[-1])
            except Exception:  # noqa: BLE001
                pass

    def _layout_path(self, model_json: dict, template: str) -> str:
        from qgis.core import QgsProject

        proj = QgsProject.instance()
        home = proj.homePath() or os.path.expanduser("~")
        out_dir = os.path.join(home, ".model_forge", "layouts")
        os.makedirs(out_dir, exist_ok=True)
        slug = _safe_filename((model_json.get("model_name") or "workflow").replace(" ", "_"))
        return os.path.join(out_dir, f"{slug}_{template}.qpt")

    def _apply_layout_to_project(
        self,
        qpt_path: str,
        title: str = "",
        subtitle: str = "",
        template: str = "default",
        layer_meta: dict[str, dict[str, str]] | None = None,
    ) -> None:
        """Build the layout in QGIS using the Python API directly.

        This mirrors the AutoLayoutTool approach: create a
        ``QgsPrintLayout``, add items (map, legend, scale bar,
        north arrow) programmatically, and add it to the project
        layout manager. The .qpt file is written for reference
        but is NOT used for loading — ``loadFromTemplate`` is
        unreliable for layer binding because QGIS-internal layer
        IDs are session-specific and can go stale.
        """
        _log(f"_apply_layout_to_project: qpt_path={qpt_path} title={title!r} template={template!r}")
        try:
            from qgis.core import (
                QgsLayoutItemMap,
                QgsLayoutItemLabel,
                QgsLayoutItemLegend,
                QgsLayoutItemScaleBar,
                QgsLayoutItemPicture,
                QgsLayoutPoint,
                QgsLayoutSize,
                QgsPrintLayout,
                QgsProject,
                QgsUnitTypes,
                QgsRectangle,
            )
        except ImportError as e:
            raise RuntimeError("QGIS Python bindings unavailable; cannot create layout.") from e

        proj = QgsProject.instance()
        layout_name = f"Model Forge: {title or template} [{template}]"
        existing = proj.layoutManager().layoutByName(layout_name)
        if existing is not None:
            proj.layoutManager().removeLayout(existing)

        layout = QgsPrintLayout(proj)
        layout.setName(layout_name)
        layout.initializeDefaults()  # sets A4 page

        # Title label
        label = QgsLayoutItemLabel(layout)
        label.setText(title or "Map Forge Map")
        try:
            from qgis.PyQt.QtGui import QFont

            label.setFont(QFont("DejaVu Sans", 18))
        except Exception:  # noqa: BLE001
            pass
        label.attemptMove(QgsLayoutPoint(10, 10, QgsUnitTypes.LayoutMillimeters))
        label.attemptResize(QgsLayoutSize(190, 12, QgsUnitTypes.LayoutMillimeters))
        layout.addLayoutItem(label)

        # Map item
        map_item = QgsLayoutItemMap(layout)
        map_item.attemptMove(QgsLayoutPoint(10, 25, QgsUnitTypes.LayoutMillimeters))
        map_item.attemptResize(QgsLayoutSize(190, 190, QgsUnitTypes.LayoutMillimeters))
        extent = self._current_canvas_extent() or self._current_project_extent()
        if extent is not None:
            map_item.setExtent(QgsRectangle(extent[0], extent[1], extent[2], extent[3]))
        map_item.setFrameEnabled(True)
        # Set map background so it's visible even without data
        try:
            map_item.setBackgroundEnabled(True)
        except Exception:  # noqa: BLE001
            pass
        layout.addLayoutItem(map_item)

        # Legend — only show layers the user picked, not all 14.
        legend = QgsLayoutItemLegend(layout)
        legend.setTitle("")
        try:
            legend.setAutoUpdateModel(False)
            root = legend.modelRootGroup()
            for child in list(root.children()):
                root.removeChildNode(child)
            if layer_meta:
                from qgis.core import QgsLayerTreeLayer

                for lid, meta in layer_meta.items():
                    ql = proj.mapLayer(lid)
                    if ql is not None:
                        node = QgsLayerTreeLayer(ql)
                        root.addChildNode(node)
        except Exception:  # noqa: BLE001
            pass
        legend.attemptMove(QgsLayoutPoint(10, 220, QgsUnitTypes.LayoutMillimeters))
        legend.attemptResize(QgsLayoutSize(190, 40, QgsUnitTypes.LayoutMillimeters))
        legend.setFrameEnabled(True)
        legend.setBackgroundEnabled(True)
        layout.addLayoutItem(legend)

        # Scale bar (linked to the map)
        scale_bar = QgsLayoutItemScaleBar(layout)
        scale_bar.setLinkedMap(map_item)
        scale_bar.applyDefaultSettings()
        scale_bar.setStyle("Single Box")
        scale_bar.setNumberOfSegments(3)
        scale_bar.setNumberOfSegmentsLeft(0)
        scale_bar.attemptMove(QgsLayoutPoint(14, 207, QgsUnitTypes.LayoutMillimeters))
        scale_bar.attemptResize(QgsLayoutSize(60, 8, QgsUnitTypes.LayoutMillimeters))
        scale_bar.setFrameEnabled(True)
        scale_bar.setBackgroundEnabled(True)
        layout.addLayoutItem(scale_bar)

        # North arrow
        north = QgsLayoutItemPicture(layout)
        for svg_path in (
            "qgis:/images/north_arrows/default_north_arrow.svg",
            ":/images/north_arrows/simple_arrow_01.svg",
            ":/images/north_arrows/simple_arrow_02.svg",
            "qgis:/images/north_arrows/simple_arrow_01.svg",
        ):
            try:
                north.setPicturePath(svg_path)
                break
            except Exception:  # noqa: BLE001
                continue
        north.attemptMove(QgsLayoutPoint(12, 27, QgsUnitTypes.LayoutMillimeters))
        north.attemptResize(QgsLayoutSize(15, 15, QgsUnitTypes.LayoutMillimeters))
        north.setFrameEnabled(True)
        layout.addLayoutItem(north)

        # Register with the project
        proj.layoutManager().addLayout(layout)
        self._last_layout_name = layout.name()
        _log(f"  layout {layout.name()} added to project (API-built)")

    # ── Verifier ──────────────────────────────────────────────

    def _run_verifier(
        self,
        model_json: dict,
        template: str,
        extent: tuple | None = None,
        qpt_path: str | None = None,
    ) -> None:
        self.lst_map_verifier.clear()
        # Add diagnostic info first so the user can see
        # the state of the generation without digging in
        # the QGIS log panel.
        crs_str = self._current_project_crs()
        all_layers = self._collect_current_layers()
        picked = self._user_picked_layer_ids()
        diag_items: list[str] = []
        diag_items.append(f"Project CRS: {crs_str or '(none)'}")
        diag_items.append(f"Detected layers: {len(all_layers)}")
        diag_items.append(f"Selected layers: {len(picked)}")
        diag_items.append(f"Extent: {extent!r}")
        if qpt_path:
            try:
                fsize = os.path.getsize(qpt_path)
                diag_items.append(f"Write OK → {qpt_path} ({fsize} bytes)")
            except Exception:
                diag_items.append(f".qpt NOT FOUND at {qpt_path}")
        for d in diag_items:
            item = QListWidgetItem(d)
            item.setForeground(Qt.darkGreen)
            self.lst_map_verifier.addItem(item)

        # Run the verifier rules on the spec.
        try:
            from model_forge.compiler_core.core.services.map_builder import (
                LayoutRequest,
                run_pipeline,
                verify_layout_spec,
            )
        except ImportError:
            self.lst_map_verifier.addItem(QListWidgetItem("map_builder not importable"))
            return

        req = LayoutRequest(
            template=template,
            title=model_json.get("model_name", ""),
            output_layer_ids=[a.get("id") for a in model_json.get("algorithms", [])],
            extent=extent,
        )
        try:
            spec = run_pipeline(req)
            report = verify_layout_spec(spec, template_name=template)
            self._render_verifier_report(report)
        except Exception as e:  # noqa: BLE001
            self.lst_map_verifier.addItem(QListWidgetItem(f"Verifier error: {e}"))

    def _render_verifier_report(self, report: Any) -> None:
        if report.passed and not report.violations:
            self.lst_map_verifier.addItem(QListWidgetItem("OK: layout passes all rules"))
            return
        for v in report.violations:
            label = f"[{v.severity}] {v.code}: {v.message}"
            item = QListWidgetItem(label)
            if v.severity == "error":
                item.setForeground(Qt.red)
            self.lst_map_verifier.addItem(item)

    # ── Run model + apply outputs ─────────────────────────────

    def _run_model(self, model_json: dict) -> Any:
        from model_forge.compiler_core.core.services.model_runner import (
            run_model as _run_model,
        )

        return _run_model(model_json, fail_fast=True, max_retries=1)

    def _apply_outputs_to_project(
        self,
        model_json: dict,
        report: Any,
    ) -> int:
        """For every step in the report, look up the output path
        in QGIS's project, load the matching .qml, and add any
        outputs that aren't already in the project.

        Returns the number of layers we successfully styled.
        """
        try:
            from qgis.core import (
                QgsProject,
                QgsVectorLayer,
                QgsRasterLayer,
            )
        except ImportError:
            return 0

        sym_dir = self._symbology_dir()
        proj = QgsProject.instance()
        applied = 0
        for step in report.step_results:
            if step.status != "completed":
                continue
            step_id = step.step_id
            qml_path = os.path.join(sym_dir, f"{_safe_filename(str(step_id))}.qml")
            for out_name, out_value in (step.outputs or {}).items():
                if not out_value:
                    continue
                layer = self._find_or_add_layer(
                    proj,
                    out_value,
                    layer_name=f"{step_id}_{out_name}",
                )
                if layer is None:
                    continue
                if not os.path.isfile(qml_path):
                    continue
                # ``loadNamedStyle`` returns either ``(True, "")`` on
                # older QGIS or ``(QgsMapLayer.LoadStyleResult.Success, "")``
                # on 3.32+. Treat both as success; the string check
                # is what makes the two shapes align.
                rc, _err = layer.loadNamedStyle(qml_path)
                if rc is True or str(rc) == "True" or str(rc).endswith("Success"):
                    layer.triggerRepaint()
                    applied += 1
        return applied

    def _find_or_add_layer(
        self,
        proj: Any,
        out_value: str,
        layer_name: str,
    ) -> Any | None:
        """Return the existing layer for ``out_value`` or add a
        new vector/raster layer pointing at the path.
        """
        from qgis.core import (
            QgsRasterLayer,
            QgsVectorLayer,
        )

        # Already loaded?
        for lyr in proj.mapLayers().values():
            if lyr.source().split("|")[0] == out_value:
                return lyr

        # Heuristic: .tif/.tiff/.vrt → raster, else vector.
        low = out_value.lower()
        if any(low.endswith(ext) for ext in (".tif", ".tiff", ".vrt", ".img")):
            lyr = QgsRasterLayer(out_value, layer_name)
        else:
            lyr = QgsVectorLayer(out_value, layer_name, "ogr")
        if not lyr.isValid():
            # Try memory URI ("memory:?..."), which we can wrap
            # into an in-memory layer; otherwise give up.
            return None
        proj.addMapLayer(lyr)
        return lyr

    # ── Geometry kind heuristic ───────────────────────────────

    def _guess_geometry_kind(self, alg: dict) -> str:
        alg_id = str(alg.get("algorithm_id", "")).lower()
        HINTS = {
            "native:centroids": "point",
            "native:pointstolines": "line",
            "native:linestopolygons": "polygon",
            "native:pointstopolygons": "polygon",
            "native:rasterize": "raster",
            "gdal:warpreproject": "raster",
            "gdal:translate": "raster",
        }
        if alg_id in HINTS:
            return HINTS[alg_id]
        for suffix, kind in (
            ("polygon", "polygon"),
            ("line", "line"),
            ("point", "point"),
            ("raster", "raster"),
        ):
            if alg_id.endswith(":" + suffix) or suffix in alg_id:
                return kind
        return "polygon"
