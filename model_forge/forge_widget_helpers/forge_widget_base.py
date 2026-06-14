"""
Model Forge main widget: Generate / Model / Settings tabs.
- Editable color-coded JSON in Model tab
- Debug/Improve prompt
- Two-phase generation
- Validation + auto-repair
- Open in Model Designer
"""

import json
import logging
import os
import re
import sys

log = logging.getLogger(__name__)

from qgis.core import QgsProject
from qgis.PyQt.QtCore import QSettings, Qt, QThread, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat

from model_forge.compiler_core.core.services.secure_storage import (
    delete_api_key,
    get_api_key,
    set_api_key,
)
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from model_forge.compiler_core.core.context_collector import ContextCollector
from model_forge.compiler_core.core.services.mermaid_renderer import to_mermaid
from model_forge.compiler_core.ui.model_builder_bridge import ModelBuilderBridge
from model_forge.legacy_base.llm_backend import LLMBackend
from model_forge.legacy_base.model_layout import compute_layout

SETTINGS_PREFIX = "ModelForge/"


class JsonHighlighter(QSyntaxHighlighter):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.fmt_key = QTextCharFormat()
        self.fmt_key.setForeground(QColor("#56B6C2"))
        self.fmt_key.setFontWeight(QFont.Bold)

        self.fmt_string = QTextCharFormat()
        self.fmt_string.setForeground(QColor("#98C379"))

        self.fmt_number = QTextCharFormat()
        self.fmt_number.setForeground(QColor("#D19A66"))

        self.fmt_keyword = QTextCharFormat()
        self.fmt_keyword.setForeground(QColor("#C678DD"))

        self.fmt_bracket = QTextCharFormat()
        self.fmt_bracket.setForeground(QColor("#E06C75"))

        self.fmt_alg_id = QTextCharFormat()
        self.fmt_alg_id.setForeground(QColor("#61AFEF"))
        self.fmt_alg_id.setFontWeight(QFont.Bold)

    def highlightBlock(self, text):
        for m in re.finditer(r'"(native:|gdal:|qgis:|saga:)[^"]*"', text):
            self.setFormat(m.start(), m.end() - m.start(), self.fmt_alg_id)

        for m in re.finditer(r'"([^"]*)"\s*:', text):
            self.setFormat(m.start(), m.end() - m.start(), self.fmt_key)

        for m in re.finditer(r':\s*"([^"]*)"', text):
            already = False
            for m2 in re.finditer(r'"(native:|gdal:|qgis:|saga:)[^"]*"', text):
                if m.start(1) >= m2.start() and m.end(1) <= m2.end():
                    already = True
                    break
            if not already:
                self.setFormat(m.start(1) - 1, m.end(1) - m.start(1) + 2, self.fmt_string)

        for m in re.finditer(r"\b(-?\d+\.?\d*)\b", text):
            self.setFormat(m.start(), m.end() - m.start(), self.fmt_number)

        for m in re.finditer(r"\b(true|false|null)\b", text):
            self.setFormat(m.start(), m.end() - m.start(), self.fmt_keyword)

        for m in re.finditer(r"[\[\]{}]", text):
            self.setFormat(m.start(), 1, self.fmt_bracket)


class GenerateWorker(QThread):
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(
        self, backend, description, model_name, model_group, context_text, two_phase=False
    ):
        super().__init__()
        self.backend = backend
        self.description = description
        self.model_name = model_name
        self.model_group = model_group
        self.context_text = context_text
        self.two_phase = two_phase

    def run(self):
        try:
            if self.two_phase:
                self.progress.emit("Phase 1/2: Generating high-level plan...")
                plan = self.backend.generate_plan(self.description, self.context_text)
                self.progress.emit("Phase 2/2: Converting plan to model definition...")
                result = self.backend.generate_model_from_plan(plan, self.context_text)
                result["_plan"] = plan
            else:
                self.progress.emit("Generating model definition (single pass)...")
                result = self.backend.generate_single_pass(
                    self.description, self.model_name, self.model_group, self.context_text
                )
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e) or "Model generation failed.")


class RepairWorker(QThread):
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, backend, workflow_json, errors, context_text):
        super().__init__()
        self.backend = backend
        self.workflow_json = workflow_json
        self.errors = errors
        self.context_text = context_text

    def run(self):
        try:
            result = self.backend.repair_model(self.workflow_json, self.errors, self.context_text)
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(str(e))


class ForgeWidget(QWidget):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.context_collector = ContextCollector()
        self.backend = LLMBackend()
        self.builder = ModelBuilderBridge()
        self._current_model_json = None
        self.current_model = None
        self.current_context_text = ""
        self._designer_dlg = None
        # Auto-test the LLM connection on init so the dock
        # shows "connected" or "not reachable" without the
        # user having to click "Test Connection" first. The
        # backend's defaults are Ollama at localhost:11434.
        # If the user has saved settings, those are loaded
        # by ``_load_compiler_settings``; this probe just
        # tries the configured endpoint and reports.
        try:
            from qgis.PyQt.QtCore import QTimer

            QTimer.singleShot(500, self._auto_probe_llm)
        except Exception:  # noqa: BLE001
            pass
        self.worker = None

        self._init_ui()
        self._load_settings()
        self._load_layers()
        self._connect_project_signals()

    def _init_ui(self):
        main_layout = QVBoxLayout()
        main_layout.setSpacing(6)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._create_generate_tab(), "Generate")
        self.tabs.addTab(self._create_model_tab(), "Model")
        self.tabs.addTab(self._create_settings_tab(), "Settings")
        main_layout.addWidget(self.tabs)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("font-size: 9pt; color: gray;")
        main_layout.addWidget(self.status_label)

        self.setLayout(main_layout)

    def _create_generate_tab(self):
        tab = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(6)

        desc_group = QGroupBox("Describe your workflow")
        desc_layout = QVBoxLayout()
        self.txt_description = QTextEdit()
        self.txt_description.setPlaceholderText(
            "e.g. Buffer input points by 500m, clip with boundary polygon, "
            "then calculate area statistics..."
        )
        self.txt_description.setMinimumHeight(160)
        self.txt_description.setMaximumHeight(220)
        desc_layout.addWidget(self.txt_description)
        desc_group.setLayout(desc_layout)
        layout.addWidget(desc_group)

        meta_layout = QHBoxLayout()
        meta_layout.addWidget(QLabel("Name:"))
        self.txt_model_name = QLineEdit("my_workflow")
        meta_layout.addWidget(self.txt_model_name)
        meta_layout.addWidget(QLabel("Group:"))
        self.txt_model_group = QLineEdit("Model Forge")
        meta_layout.addWidget(self.txt_model_group)
        layout.addLayout(meta_layout)

        layers_group = QGroupBox("Context layers")
        layers_layout = QVBoxLayout()
        layers_layout.setSpacing(4)
        self.layer_list = QListWidget()
        self.layer_list.setSelectionMode(QAbstractItemView.MultiSelection)
        self.layer_list.setMaximumHeight(120)
        layers_layout.addWidget(self.layer_list)

        sel_btn_layout = QHBoxLayout()
        btn_select_all = QPushButton("Select all")
        btn_select_all.clicked.connect(self.layer_list.selectAll)
        sel_btn_layout.addWidget(btn_select_all)
        btn_deselect_all = QPushButton("Deselect all")
        btn_deselect_all.clicked.connect(self.layer_list.clearSelection)
        sel_btn_layout.addWidget(btn_deselect_all)
        layers_layout.addLayout(sel_btn_layout)

        layers_group.setLayout(layers_layout)
        layout.addWidget(layers_group)

        self.chk_two_phase = QCheckBox("Two-phase generation (plan then build)")
        self.chk_two_phase.setChecked(False)
        self.chk_two_phase.setToolTip(
            "When checked, the LLM first creates a plan, then builds the model. "
            "More reliable for complex workflows, but slower."
        )
        layout.addWidget(self.chk_two_phase)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)

        self.lbl_status = QLabel("")
        layout.addWidget(self.lbl_status)

        # Generate button (no cancel — v3 style)
        self.btn_generate = QPushButton("Generate Model")
        self.btn_generate.setStyleSheet(
            "QPushButton { background-color: #4CAF50; color: white; "
            "font-weight: bold; padding: 8px; border-radius: 4px; } "
            "QPushButton:hover { background-color: #45a049; }"
        )
        self.btn_generate.clicked.connect(self._on_generate)
        layout.addWidget(self.btn_generate)

        # Cancel button (initially hidden, replaces Generate during generation)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setStyleSheet(
            "QPushButton { background-color: #f44336; color: white; "
            "font-weight: bold; padding: 8px; border-radius: 4px; } "
            "QPushButton:hover { background-color: #d32f2f; }"
        )
        self.btn_cancel.setVisible(False)
        layout.addWidget(self.btn_cancel)

        self.lbl_context_info = QLabel("")
        self.lbl_context_info.setWordWrap(True)
        self.lbl_context_info.setStyleSheet("color: gray; font-size: 10px;")
        layout.addWidget(self.lbl_context_info)

        layout.addStretch()
        tab.setLayout(layout)
        return tab

    def _create_model_tab(self):
        tab = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(6)

        self.lbl_validity = QLabel("No model generated yet.")
        self.lbl_validity.setWordWrap(True)
        layout.addWidget(self.lbl_validity)

        self._model_tabs = QTabWidget()

        # ---- JSON tab ----
        json_tab = QWidget()
        json_layout = QVBoxLayout()
        json_layout.setContentsMargins(0, 4, 0, 0)

        json_layout.addWidget(QLabel("Model JSON (editable):"))
        self.txt_model_json = QTextEdit()
        self.txt_model_json.setStyleSheet(
            "QTextEdit { background-color: #282C34; color: #ABB2BF; "
            "font-family: Consolas, monospace; font-size: 11px; }"
        )
        font = QFont("Consolas", 10)
        font.setStyleHint(QFont.Monospace)
        self.txt_model_json.setFont(font)
        self.highlighter = JsonHighlighter(self.txt_model_json.document())
        json_layout.addWidget(self.txt_model_json, stretch=1)

        json_tab.setLayout(json_layout)
        self._model_tabs.addTab(json_tab, "JSON")

        # ---- Graph tab ----
        graph_tab = QWidget()
        graph_layout = QVBoxLayout()
        graph_layout.setContentsMargins(0, 4, 0, 0)

        graph_layout.addWidget(QLabel("Mermaid flowchart:"))
        self._mermaid_view = None
        from model_forge.compiler_core.ui.mermaid_view import MermaidGraphView

        self._mermaid_view = MermaidGraphView()
        if self._mermaid_view.is_available:
            graph_layout.addWidget(self._mermaid_view, stretch=1)

        self.txt_mermaid = QTextEdit()
        self.txt_mermaid.setReadOnly(True)
        self.txt_mermaid.setStyleSheet(
            "QTextEdit { background-color: #1e1e1e; color: #d4d4d4; "
            "font-family: Consolas, monospace; font-size: 11px; }"
        )
        self.txt_mermaid.setFont(font)
        if not self._mermaid_view.is_available:
            graph_layout.addWidget(self.txt_mermaid, stretch=1)
        else:
            self.txt_mermaid.setMaximumHeight(200)
            graph_layout.addWidget(self.txt_mermaid)

        mermaid_btn_row = QHBoxLayout()
        self.btn_copy_mermaid = QPushButton("Copy Mermaid")
        self.btn_copy_mermaid.setToolTip("Copy mermaid markdown to clipboard")
        self.btn_copy_mermaid.clicked.connect(self._copy_mermaid)
        mermaid_btn_row.addWidget(self.btn_copy_mermaid)

        self.btn_refresh_mermaid = QPushButton("Refresh Graph")
        self.btn_refresh_mermaid.clicked.connect(self._refresh_mermaid)
        mermaid_btn_row.addWidget(self.btn_refresh_mermaid)
        mermaid_btn_row.addStretch()
        graph_layout.addLayout(mermaid_btn_row)

        graph_tab.setLayout(graph_layout)
        self._model_tabs.addTab(graph_tab, "Graph")

        layout.addWidget(self._model_tabs, stretch=3)

        # ---- Buttons ----
        btn_row = QHBoxLayout()

        self.btn_rebuild = QPushButton("Rebuild model from JSON above")
        self.btn_rebuild.clicked.connect(self._on_rebuild_from_json)
        self.btn_rebuild.setEnabled(False)
        btn_row.addWidget(self.btn_rebuild)

        self.btn_save = QPushButton("Save .model3")
        self.btn_save.clicked.connect(self._save_model)
        self.btn_save.setEnabled(False)
        btn_row.addWidget(self.btn_save)

        self.btn_open_designer = QPushButton("Open in Designer")
        self.btn_open_designer.clicked.connect(self._on_open_designer)
        self.btn_open_designer.setEnabled(False)
        btn_row.addWidget(self.btn_open_designer)

        layout.addLayout(btn_row)

        # ---- Debug / Improve ----
        improve_group = QGroupBox("Debug / Improve")
        improve_layout = QVBoxLayout()
        improve_layout.setSpacing(4)

        self.txt_improve_prompt = QTextEdit()
        self.txt_improve_prompt.setPlaceholderText(
            "Describe what to fix, improve, or add to the current model...\n"
            "e.g. 'Add a dissolve step after the buffer' or 'Fix the field name to population'"
        )
        self.txt_improve_prompt.setMaximumHeight(80)
        improve_layout.addWidget(self.txt_improve_prompt)

        improve_btn_layout = QHBoxLayout()
        self.btn_auto_repair = QPushButton("Auto-Repair (validation)")
        self.btn_auto_repair.setToolTip("Validate the JSON and ask the LLM to fix any errors")
        self.btn_auto_repair.clicked.connect(self._on_auto_repair)
        self.btn_auto_repair.setEnabled(False)
        improve_btn_layout.addWidget(self.btn_auto_repair)

        self.btn_improve = QPushButton("Improve")
        self.btn_improve.setToolTip(
            "Send the current JSON + your feedback to the LLM for improvement"
        )
        self.btn_improve.clicked.connect(self._on_improve)
        self.btn_improve.setEnabled(False)
        improve_btn_layout.addWidget(self.btn_improve)
        improve_layout.addLayout(improve_btn_layout)

        improve_group.setLayout(improve_layout)
        layout.addWidget(improve_group)

        tab.setLayout(layout)
        return tab

    def _create_settings_tab(self):
        tab = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(6)

        backend_group = QGroupBox("LLM Backend")
        bg_layout = QGridLayout()
        bg_layout.setSpacing(4)

        bg_layout.addWidget(QLabel("Provider:"), 0, 0)
        self.cmb_backend = QComboBox()
        for key, info in LLMBackend.BACKENDS.items():
            self.cmb_backend.addItem(info["label"], key)
        self.cmb_backend.currentIndexChanged.connect(self._on_backend_changed)
        bg_layout.addWidget(self.cmb_backend, 0, 1)

        bg_layout.addWidget(QLabel("URL:"), 1, 0)
        self.txt_url = QLineEdit("http://localhost:11434")
        bg_layout.addWidget(self.txt_url, 1, 1)

        bg_layout.addWidget(QLabel("API Key:"), 2, 0)
        self.txt_api_key = QLineEdit()
        self.txt_api_key.setEchoMode(QLineEdit.Password)
        self.txt_api_key.setPlaceholderText("Not needed for Ollama")
        bg_layout.addWidget(self.txt_api_key, 2, 1)

        bg_layout.addWidget(QLabel("Model:"), 3, 0)
        self.txt_model = QLineEdit("qwen2.5-coder:7b")
        bg_layout.addWidget(self.txt_model, 3, 1)

        bg_layout.addWidget(QLabel("Thinking level:"), 4, 0)
        thinking_layout = QHBoxLayout()
        self.sld_temperature = QSlider(Qt.Horizontal)
        self.sld_temperature.setMinimum(0)
        self.sld_temperature.setMaximum(10)
        self.sld_temperature.setValue(2)
        self.sld_temperature.setTickPosition(QSlider.TicksBelow)
        self.sld_temperature.setTickInterval(1)
        self.sld_temperature.valueChanged.connect(self._on_temperature_changed)
        thinking_layout.addWidget(self.sld_temperature)
        self.lbl_temperature = QLabel("0.2")
        self.lbl_temperature.setMinimumWidth(30)
        thinking_layout.addWidget(self.lbl_temperature)
        bg_layout.addLayout(thinking_layout, 4, 1)

        btn_test = QPushButton("Test Connection")
        btn_test.clicked.connect(self._on_test_connection)
        bg_layout.addWidget(btn_test, 5, 0, 1, 2)

        backend_group.setLayout(bg_layout)
        layout.addWidget(backend_group)

        algo_group = QGroupBox("Algorithm Catalog")
        algo_layout = QVBoxLayout()
        algo_layout.setSpacing(4)

        count_layout = QHBoxLayout()
        count_layout.addWidget(QLabel("Max curated algorithms:"))
        self.spn_max_algos = QSpinBox()
        self.spn_max_algos.setMinimum(10)
        self.spn_max_algos.setMaximum(500)
        self.spn_max_algos.setValue(100)
        self.spn_max_algos.setToolTip(
            "Maximum number of algorithm signatures to include in the LLM context."
        )
        count_layout.addWidget(self.spn_max_algos)
        algo_layout.addLayout(count_layout)

        self.chk_include_all_providers = QCheckBox("Include ALL providers (full registry scan)")
        self.chk_include_all_providers.setChecked(False)
        self.chk_include_all_providers.setToolTip(
            "Scans the entire Processing registry. Slow and uses a lot of context."
        )
        algo_layout.addWidget(self.chk_include_all_providers)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setMaximumHeight(200)

        provider_container = QWidget()
        provider_grid = QGridLayout()
        provider_grid.setSpacing(2)
        provider_grid.setContentsMargins(0, 0, 0, 0)

        self._provider_widgets: dict[str, dict] = {}

        try:
            from qgis.core import QgsApplication

            registry = QgsApplication.processingRegistry()
            providers = sorted(
                registry.providers(),
                key=lambda p: (0 if p.id() == "native" else 1 if p.id() == "gdal" else 2, p.id()),
            )
        except Exception:
            log.warning("Failed to load QGIS processing providers")
            providers = []

        for i, provider in enumerate(providers):
            pid = provider.id()
            pname = provider.name() or pid
            default_on = pid in ("native", "gdal")
            alg_count = len(list(provider.algorithms()))

            chk = QCheckBox(f"{pid} ({alg_count})")
            chk.setChecked(default_on)
            chk.setToolTip(pname)
            row, col = divmod(i, 2)
            provider_grid.addWidget(chk, row, col)
            self._provider_widgets[pid] = {"chk": chk}

        provider_container.setLayout(provider_grid)
        scroll.setWidget(provider_container)

        algo_layout.addWidget(scroll)
        algo_group.setLayout(algo_layout)
        layout.addWidget(algo_group)

        # ── MCP Server section ──────────────────────────────────────────
        mcp_group = QGroupBox("MCP Server")
        mcp_layout = QGridLayout()
        mcp_layout.setSpacing(4)

        mcp_layout.addWidget(QLabel("Port:"), 0, 0)
        self.spn_mcp_port = QSpinBox()
        self.spn_mcp_port.setRange(1024, 65535)
        self.spn_mcp_port.setValue(9090)
        self.spn_mcp_port.setToolTip("Port for MCP SSE server (connect your MCP client here)")
        mcp_layout.addWidget(self.spn_mcp_port, 0, 1)

        self.btn_mcp_start = QPushButton("Start MCP Server")
        self.btn_mcp_start.setStyleSheet("font-weight: bold;")
        self.btn_mcp_start.clicked.connect(self._on_mcp_toggle)
        mcp_layout.addWidget(self.btn_mcp_start, 1, 0)

        self.lbl_mcp_status = QLabel("Stopped")
        self.lbl_mcp_status.setStyleSheet("color: gray;")
        mcp_layout.addWidget(self.lbl_mcp_status, 1, 1)

        btn_mcp_config = QPushButton("Copy Claude Config")
        btn_mcp_config.setToolTip("Copy the Claude Desktop config entry to clipboard")
        btn_mcp_config.clicked.connect(self._on_copy_mcp_config)
        mcp_layout.addWidget(btn_mcp_config, 2, 0, 1, 2)

        mcp_group.setLayout(mcp_layout)
        layout.addWidget(mcp_group)

        btn_apply = QPushButton("Apply Settings")
        btn_apply.setStyleSheet("font-weight: bold;")
        btn_apply.clicked.connect(self._apply_settings)
        layout.addWidget(btn_apply)

        layout.addStretch()
        tab.setLayout(layout)

        return tab

    def _load_layers(self):
        self.layer_list.clear()
        for layer in QgsProject.instance().mapLayers().values():
            item = QListWidgetItem(layer.name())
            item.setData(Qt.UserRole, layer)
            self.layer_list.addItem(item)
            item.setSelected(True)

    def _connect_project_signals(self):
        project = QgsProject.instance()
        project.layersAdded.connect(lambda _: self._load_layers())
        project.layersRemoved.connect(lambda _: self._load_layers())

    def disconnect_signals(self):
        try:
            project = QgsProject.instance()
            project.layersAdded.disconnect()
            project.layersRemoved.disconnect()
        except Exception:
            pass

    def _get_selected_layers(self):
        return [
            self.layer_list.item(i).data(Qt.UserRole)
            for i in range(self.layer_list.count())
            if self.layer_list.item(i).isSelected()
        ]

    def _on_backend_changed(self, index):
        key = self.cmb_backend.itemData(index)
        profile = LLMBackend.BACKENDS.get(key, {})
        self.txt_url.setText(profile.get("default_url", ""))
        self.txt_model.setText(profile.get("default_model", ""))
        needs_key = key != "ollama"
        self.txt_api_key.setEnabled(needs_key)
        self.txt_api_key.setPlaceholderText(
            "Not needed for Ollama" if not needs_key else "Enter your API key"
        )

    def _on_temperature_changed(self, value):
        temp = value / 10.0
        self.lbl_temperature.setText(f"{temp:.1f}")

    def _on_test_connection(self):
        self._apply_settings()
        if self.backend.test_connection():
            self._set_llm_status(
                f"✓ Connected: {self.backend.backend} @ {self.backend.url}",
                ok=True,
            )
            QMessageBox.information(
                self, "Connection OK", "Successfully connected to the LLM backend."
            )
        else:
            self._set_llm_status(
                f"✗ Not reachable: {self.backend.backend} @ {self.backend.url}",
                ok=False,
            )
            QMessageBox.warning(
                self, "Connection Failed", "Could not connect. Check URL and API key."
            )

    def _auto_probe_llm(self) -> None:
        """Run on init: try the saved LLM endpoint once.

        Updates the status label so the user immediately
        sees "connected" or "not reachable" without having
        to click Test Connection. Runs after a 500 ms
        delay so the QSettings save/load is settled.
        """
        try:
            ok = self.backend.test_connection()
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            self._set_llm_status(
                f"✓ Connected: {self.backend.backend} @ {self.backend.url}",
                ok=True,
            )
        else:
            self._set_llm_status(
                f"○ LLM not reachable ({self.backend.backend} @ {self.backend.url}). "
                f"Settings → Test Connection to retry.",
                ok=False,
            )

    def _set_llm_status(self, text: str, ok: bool) -> None:
        """Update the status label with the LLM connection state."""
        if not hasattr(self, "status_label") or self.status_label is None:
            return
        self.status_label.setText(text)
        if ok:
            self.status_label.setStyleSheet("color: #2c7a2c; font-weight: bold;")
        else:
            self.status_label.setStyleSheet("color: #b06a00; font-style: italic;")

    def _apply_settings(self):
        key = self.cmb_backend.itemData(self.cmb_backend.currentIndex())
        self.backend.configure(
            backend=key,
            url=self.txt_url.text().strip(),
            api_key=self.txt_api_key.text().strip(),
            model=self.txt_model.text().strip(),
            temperature=self.sld_temperature.value() / 10.0,
        )
        self._save_settings()
        self.status_label.setText("Settings applied.")

    def _get_algo_config(self):
        config: dict = {
            "max_algorithms": self.spn_max_algos.value(),
            "include_all": self.chk_include_all_providers.isChecked(),
            "provider_ids": [],
        }
        for pid, w in self._provider_widgets.items():
            if w["chk"].isChecked():
                config["provider_ids"].append(pid)

        return config

    def _restore_provider_settings(self):
        s = QSettings()
        s.beginGroup(SETTINGS_PREFIX + "providers")
        for pid, w in self._provider_widgets.items():
            enabled = s.value(f"{pid}/enabled")
            if enabled is not None:
                try:
                    w["chk"].setChecked(str(enabled).lower() == "true")
                except Exception:
                    pass
        s.endGroup()

    def _load_settings(self):
        s = QSettings()
        key = s.value(SETTINGS_PREFIX + "backend")
        if key is not None:
            idx = self.cmb_backend.findData(key)
            if idx >= 0:
                self.cmb_backend.setCurrentIndex(idx)
        url = s.value(SETTINGS_PREFIX + "url")
        if url:
            self.txt_url.setText(url)
        api_key = s.value(SETTINGS_PREFIX + "api_key")
        if api_key:
            self.txt_api_key.setText(api_key)
        else:
            stored = get_api_key()
            if stored:
                self.txt_api_key.setText(stored)
        model = s.value(SETTINGS_PREFIX + "model")
        if model:
            self.txt_model.setText(model)
        temp = s.value(SETTINGS_PREFIX + "temperature")
        if temp is not None:
            try:
                self.sld_temperature.setValue(int(float(temp) * 10))
            except (ValueError, TypeError):
                pass
        self._restore_provider_settings()

    def _save_settings(self):
        s = QSettings()
        key = self.cmb_backend.itemData(self.cmb_backend.currentIndex())
        s.setValue(SETTINGS_PREFIX + "backend", key)
        s.setValue(SETTINGS_PREFIX + "url", self.txt_url.text())
        api_key_text = self.txt_api_key.text()
        s.setValue(SETTINGS_PREFIX + "api_key", api_key_text)
        set_api_key(api_key_text)
        s.setValue(SETTINGS_PREFIX + "model", self.txt_model.text())
        s.setValue(SETTINGS_PREFIX + "temperature", self.sld_temperature.value() / 10.0)

        # Persist provider selection
        s.beginGroup(SETTINGS_PREFIX + "providers")
        for pid, w in self._provider_widgets.items():
            s.setValue(f"{pid}/enabled", w["chk"].isChecked())
        s.endGroup()

    def _on_generate(self):
        self._stop_worker("worker")
        description = self.txt_description.toPlainText().strip()
        if not description:
            QMessageBox.warning(self, "Model Forge", "Please enter a workflow description.")
            return

        self._apply_settings()

        selected_layers = self._get_selected_layers()
        algo_config = self._get_algo_config()
        self.current_context_text = self.context_collector.collect(selected_layers, algo_config)

        self.btn_generate.setEnabled(False)
        self.progress_bar.show()
        self.lbl_status.setText("Starting generation...")

        self.worker = GenerateWorker(
            self.backend,
            description,
            self.txt_model_name.text(),
            self.txt_model_group.text(),
            self.current_context_text,
            two_phase=self.chk_two_phase.isChecked(),
        )
        self.worker.progress.connect(lambda msg: self.lbl_status.setText(msg))
        self.worker.finished.connect(self._on_generate_finished)
        self.worker.error.connect(self._on_generate_error)
        self.worker.start()

    def _on_generate_finished(self, workflow):
        self.progress_bar.hide()
        self.btn_generate.setEnabled(True)

        workflow.pop("_plan", None)

        if "inputs" not in workflow or "algorithms" not in workflow:
            self.lbl_status.setText("LLM response missing required keys.")
            raw_text = json.dumps(workflow, indent=2, ensure_ascii=False)
            self.txt_model_json.setPlainText(raw_text)
            self.lbl_validity.setText(
                "\u26a0 Missing 'inputs' or 'algorithms'. Keys: " + str(list(workflow.keys()))
            )
            self.lbl_validity.setStyleSheet("color: orange; font-weight: bold;")
            self.tabs.setCurrentIndex(1)
            return

        workflow = compute_layout(workflow)
        self._current_model_json = workflow
        self.lbl_status.setText("Model generated! See Model tab.")

        self.txt_model_json.setPlainText(json.dumps(workflow, indent=2, ensure_ascii=False))
        self.lbl_validity.setText("\u2713 Valid JSON structure received.")
        self.lbl_validity.setStyleSheet("color: #4CAF50; font-weight: bold;")
        self.btn_rebuild.setEnabled(True)
        self.btn_improve.setEnabled(True)
        self.btn_auto_repair.setEnabled(True)
        self.tabs.setCurrentIndex(1)

        self._try_build_model(workflow)
        self._refresh_context_for_improve()
        self._refresh_mermaid()

    def _on_generate_error(self, error_msg):
        self.progress_bar.hide()
        self.btn_generate.setEnabled(True)
        self.lbl_status.setText("Generation failed.")

    def _refresh_context_for_improve(self):
        """Refresh context for improve/repair operations."""
        selected_layers = self._get_selected_layers()
        algo_config = self._get_algo_config()
        self.current_context_text = self.context_collector.collect(selected_layers, algo_config)

    def _try_build_model(self, workflow):
        try:
            model = self.builder.load_model_json(
                workflow,
                open_designer=False,
            )
            self.current_model = model
            self.btn_save.setEnabled(True)
            self.btn_open_designer.setEnabled(True)
        except Exception as e:
            self.current_model = None
            self.btn_save.setEnabled(False)
            self.btn_open_designer.setEnabled(False)
            self.status_label.setText("Build warning: " + str(e))

    def _refresh_mermaid(self):
        workflow = self._current_model_json
        if not workflow:
            self.txt_mermaid.setPlainText("flowchart TD\n  empty[No model data]")
            return
        try:
            mermaid_text = to_mermaid(workflow)
            self.txt_mermaid.setPlainText(mermaid_text)
            if self._mermaid_view and self._mermaid_view.is_available:
                self._mermaid_view.set_mermaid(mermaid_text)
        except Exception as e:
            self.txt_mermaid.setPlainText(f'flowchart TD\n  error["Mermaid error: {e}"]')

    def _copy_mermaid(self):
        from qgis.PyQt.QtWidgets import QApplication

        QApplication.clipboard().setText(self.txt_mermaid.toPlainText())

    def _on_rebuild_from_json(self):
        text = self.txt_model_json.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Model Forge", "JSON is empty.")
            return
        try:
            workflow = json.loads(text)
        except json.JSONDecodeError as e:
            QMessageBox.warning(self, "JSON Error", "Invalid JSON:\n" + str(e))
            return

        if "inputs" not in workflow or "algorithms" not in workflow:
            QMessageBox.warning(
                self, "Model Forge", "JSON must have 'inputs' and 'algorithms' keys."
            )
            return

        self._current_model_json = workflow
        self._try_build_model(workflow)
        self.lbl_validity.setText("\u2713 Model rebuilt from edited JSON.")
        self.lbl_validity.setStyleSheet("color: #4CAF50; font-weight: bold;")

    def _copy_json(self):
        from qgis.PyQt.QtWidgets import QApplication

        QApplication.clipboard().setText(self.txt_model_json.toPlainText())
        self.status_label.setText("JSON copied to clipboard.")

    def _save_model(self):
        if not self.current_model:
            text = self.txt_model_json.toPlainText().strip()
            if not text:
                QMessageBox.warning(self, "Empty", "No model JSON to save.")
                return
            path, _ = QFileDialog.getSaveFileName(self, "Save Model", "", "QGIS Model (*.model3)")
            if path:
                if not path.endswith(".model3"):
                    path += ".model3"
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
                self.status_label.setText(f"JSON saved to {path}")
            return

        path, _ = QFileDialog.getSaveFileName(self, "Save Model", "", "QGIS Models (*.model3)")
        if path:
            if not path.endswith(".model3"):
                path += ".model3"
            self.current_model.setSourceFilePath(path)
            if self.current_model.toFile(path):
                QMessageBox.information(self, "Model Forge", "Model saved to:\n" + path)
            else:
                QMessageBox.warning(self, "Model Forge", "Failed to save model.")

    def _on_open_designer(self):
        """Open the current model in QGIS Model Designer."""
        import tempfile

        from processing.modeler.ModelerDialog import ModelerDialog
        from qgis.core import QgsProcessingModelAlgorithm

        if not self._current_model_json:
            QMessageBox.warning(self, "Model Forge", "No current workflow to open.")
            return

        try:
            model = self.builder.load_model_json(
                self._current_model_json,
                open_designer=False,
            )

            tmp = tempfile.NamedTemporaryFile(suffix=".model3", delete=False)
            tmp_path = tmp.name
            tmp.close()

            try:
                if not model.toFile(tmp_path):
                    QMessageBox.warning(
                        self, "Model Forge", "Could not create temporary model file."
                    )
                    return

                file_model = QgsProcessingModelAlgorithm()
                file_model.fromFile(tmp_path)
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

            dlg = ModelerDialog(file_model)
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()
            self._designer_dlg = dlg

        except Exception as e:
            QMessageBox.critical(
                self,
                "Model Forge",
                "Could not open Designer:\n" + str(e),
            )

    def _on_auto_repair(self):
        text = self.txt_model_json.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Empty", "No model JSON to repair.")
            return
        try:
            workflow = json.loads(text)
        except json.JSONDecodeError as e:
            QMessageBox.warning(self, "Invalid JSON", f"Cannot parse JSON: {e}")
            return

        errors = self._validate_model(workflow)
        if not errors:
            QMessageBox.information(self, "Valid", "No validation errors found.")
            return

        self._apply_settings()
        self.lbl_validity.setText("Sending repair request to LLM...")
        self.lbl_validity.setStyleSheet("color: #2196F3;")
        self.progress_bar.show()
        self.btn_auto_repair.setEnabled(False)
        self.btn_improve.setEnabled(False)

        self.repair_worker = RepairWorker(
            self.backend,
            workflow,
            list(errors),
            self.current_context_text,
        )
        self.repair_worker.finished.connect(self._on_repair_finished)
        self.repair_worker.error.connect(self._on_repair_error)
        self.repair_worker.start()

    def _on_improve(self):
        text = self.txt_model_json.toPlainText().strip()
        feedback = self.txt_improve_prompt.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Empty", "No model JSON to improve.")
            return
        if not feedback:
            QMessageBox.warning(self, "No Feedback", "Please describe what to fix or improve.")
            return

        try:
            workflow = json.loads(text)
        except json.JSONDecodeError as e:
            QMessageBox.warning(self, "Invalid JSON", f"Cannot parse current JSON: {e}")
            return

        self._apply_settings()
        self.lbl_validity.setText("Improving model...")
        self.lbl_validity.setStyleSheet("color: #2196F3;")
        self.progress_bar.show()
        self.btn_auto_repair.setEnabled(False)
        self.btn_improve.setEnabled(False)

        errors = self._validate_model(workflow)
        all_errors = list(errors) + [f"USER FEEDBACK: {feedback}"]

        self.repair_worker = RepairWorker(
            self.backend,
            workflow,
            all_errors,
            self.current_context_text,
        )
        self.repair_worker.finished.connect(self._on_repair_finished)
        self.repair_worker.error.connect(self._on_repair_error)
        self.repair_worker.start()

    def _on_repair_finished(self, repaired):
        self.progress_bar.hide()
        self.btn_auto_repair.setEnabled(True)
        self.btn_improve.setEnabled(True)

        if not isinstance(repaired, dict):
            self.lbl_validity.setText(
                f"Repair failed: expected dict, got {type(repaired).__name__}"
            )
            self.lbl_validity.setStyleSheet("color: red;")
            return

        if "inputs" not in repaired or "algorithms" not in repaired:
            self.lbl_validity.setText("Repair response missing required keys.")
            self.lbl_validity.setStyleSheet("color: red;")
            raw_text = json.dumps(repaired, indent=2, ensure_ascii=False)
            self.txt_model_json.setPlainText(raw_text)
            return

        layouted = compute_layout(repaired)
        self._current_model_json = layouted
        self.txt_model_json.setPlainText(json.dumps(layouted, indent=2, ensure_ascii=False))
        self.lbl_validity.setText("\u2713 Model repaired/improved.")
        self.lbl_validity.setStyleSheet("color: #4CAF50; font-weight: bold;")
        self.status_label.setText("Model improved successfully.")
        self._try_build_model(layouted)

    def _on_repair_error(self, error_msg):
        self.progress_bar.hide()
        self.btn_auto_repair.setEnabled(True)
        self.btn_improve.setEnabled(True)
        if not isinstance(error_msg, str):
            error_msg = repr(error_msg)
        self.lbl_validity.setText(f"Repair failed: {error_msg}")
        self.lbl_validity.setStyleSheet("color: red;")

    def _validate_model(self, workflow):
        """Basic structural validation. Returns list of error strings."""
        errors = []
        if "inputs" not in workflow:
            errors.append("Missing 'inputs' key.")
        if "algorithms" not in workflow:
            errors.append("Missing 'algorithms' key.")
        algos = workflow.get("algorithms", [])
        ids = set()
        for a in algos:
            if "id" not in a:
                errors.append(f"Algorithm missing 'id': {a.get('description', '?')}")
            elif a["id"] in ids:
                errors.append(f"Duplicate algorithm id: {a['id']}")
            else:
                ids.add(a["id"])
            if "algorithm_id" not in a:
                errors.append(f"Algorithm '{a.get('id', '?')}' missing 'algorithm_id'.")
            for pname, pval in a.get("parameters", {}).items():
                if isinstance(pval, dict) and pval.get("type") == "child_output":
                    ref = pval.get("child_id")
                    if ref and ref not in ids and ref not in [x["id"] for x in algos]:
                        errors.append(
                            f"Algorithm '{a.get('id')}' param '{pname}' references "
                            f"unknown child_id '{ref}'."
                        )
        return errors

    # ── MCP Server handlers ────────────────────────────────────────────────

    def _on_mcp_toggle(self):
        from model_forge.mcp_server.server import is_running, start_server, stop_server

        if is_running():
            stop_server()
            self.btn_mcp_start.setText("Start MCP Server")
            self.lbl_mcp_status.setText("Stopped")
            self.lbl_mcp_status.setStyleSheet("color: gray;")
            return

        port = self.spn_mcp_port.value()
        llm_config = self._get_mcp_llm_config()

        try:
            start_server(host="127.0.0.1", port=port, llm_config=llm_config)
            self.btn_mcp_start.setText("Stop MCP Server")
            self.lbl_mcp_status.setText(f"Running on port {port}")
            self.lbl_mcp_status.setStyleSheet("color: green; font-weight: bold;")
        except Exception as e:
            QMessageBox.critical(self, "MCP Server", f"Failed to start: {e}")
            self.lbl_mcp_status.setText("Error")
            self.lbl_mcp_status.setStyleSheet("color: red;")

    def _get_mcp_llm_config(self) -> dict:
        backend = self.cmb_backend.currentData() or "ollama"
        backend_info = LLMBackend.BACKENDS.get(backend, {})
        return {
            "provider": backend_info.get("type", backend),
            "model": self.txt_model.text().strip() or "qwen2.5-coder:7b",
            "base_url": self.txt_url.text().strip() or "http://localhost:11434",
            "api_key": self.txt_api_key.text().strip() or "",
            "temperature": self.sld_temperature.value() / 10.0,
            "timeout": 120,
        }

    def _on_copy_mcp_config(self):
        port = self.spn_mcp_port.value()
        python = sys.executable or "python3"
        config = {
            "mcpServers": {
                "model-forge": {
                    "command": python,
                    "args": ["-m", "model_forge.mcp_server"],
                    "url": f"http://127.0.0.1:{port}/sse",
                }
            }
        }
        from qgis.PyQt.QtWidgets import QApplication

        QApplication.clipboard().setText(json.dumps(config, indent=2))
        QMessageBox.information(
            self,
            "MCP Config",
            "Claude Desktop config copied to clipboard.\n\n"
            "Paste it into: %%APPDATA%%\\Claude\\claude_desktop_config.json\n"
            "Then start the MCP server here first.",
        )

    def _stop_worker(self, attr: str = "worker"):
        w = getattr(self, attr, None)
        if w is None:
            return
        if w.isRunning():
            w.quit()
            w.wait(3000)
        setattr(self, attr, None)
