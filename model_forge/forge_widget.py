"""
Model Forge main widget: Generate / Model / Settings tabs.
- Editable color-coded JSON in Model tab
- Debug/Improve prompt
- Two-phase generation
- Validation + auto-repair
- Open in Model Designer
"""

import json
import re
import traceback

from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal, QSettings
from qgis.PyQt.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QPushButton, QCheckBox, QListWidget, QListWidgetItem,
    QTextEdit, QLineEdit, QComboBox,
    QMessageBox, QTabWidget, QAbstractItemView, QSlider,
    QSpinBox, QFileDialog, QGridLayout, QProgressBar,
)
from qgis.core import QgsProject

from .context_collector import ContextCollector
from .llm_backend import LLMBackend
from .model_builder import ModelBuilder
from .model_layout import compute_layout

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

        for m in re.finditer(r'\b(-?\d+\.?\d*)\b', text):
            self.setFormat(m.start(), m.end() - m.start(), self.fmt_number)

        for m in re.finditer(r'\b(true|false|null)\b', text):
            self.setFormat(m.start(), m.end() - m.start(), self.fmt_keyword)

        for m in re.finditer(r'[\[\]{}]', text):
            self.setFormat(m.start(), 1, self.fmt_bracket)


class GenerateWorker(QThread):
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, backend, description, model_name, model_group, context_text, two_phase=False):
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
            self.error.emit(str(e) + "\n\n" + traceback.format_exc())


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
        self.builder = ModelBuilder()
        self._current_model_json = None
        self.current_model = None
        self.current_context_text = ""
        self._designer_dlg = None
        self.worker = None

        self._load_settings()
        self._init_ui()
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

        layout.addWidget(QLabel("Model JSON (editable):"))
        self.txt_model_json = QTextEdit()
        self.txt_model_json.setStyleSheet(
            "QTextEdit { background-color: #282C34; color: #ABB2BF; "
            "font-family: Consolas, monospace; font-size: 11px; }"
        )
        font = QFont("Consolas", 10)
        font.setStyleHint(QFont.Monospace)
        self.txt_model_json.setFont(font)
        self.highlighter = JsonHighlighter(self.txt_model_json.document())
        layout.addWidget(self.txt_model_json, stretch=3)

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

        improve_group = QGroupBox("Debug / Improve")
        improve_layout = QVBoxLayout()
        improve_layout.setSpacing(4)

        self.txt_improve_prompt = QTextEdit()
        self.txt_improve_prompt.setPlaceholderText(
            "Describe what to fix, improve, or add to the current model...\n"
            "e.g. 'Add a dissolve step after the buffer' or 'Fix the field name to population'"
        )
        self.txt_improve_prompt.setMaximumHeight(100)
        improve_layout.addWidget(self.txt_improve_prompt)

        improve_btn_layout = QHBoxLayout()
        self.btn_auto_repair = QPushButton("Auto-Repair (validation)")
        self.btn_auto_repair.setToolTip("Validate the JSON and ask the LLM to fix any errors")
        self.btn_auto_repair.clicked.connect(self._on_auto_repair)
        self.btn_auto_repair.setEnabled(False)
        improve_btn_layout.addWidget(self.btn_auto_repair)

        self.btn_improve = QPushButton("Send repair prompt")
        self.btn_improve.setToolTip("Send the current JSON + your feedback to the LLM for improvement")
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
        self.spn_max_algos.setValue(60)
        self.spn_max_algos.setToolTip(
            "Maximum number of algorithm signatures to include in the LLM context."
        )
        count_layout.addWidget(self.spn_max_algos)
        algo_layout.addLayout(count_layout)

        self.chk_include_all_native = QCheckBox("Include all native: algorithms")
        self.chk_include_all_native.setChecked(True)
        algo_layout.addWidget(self.chk_include_all_native)

        self.chk_include_gdal = QCheckBox("Include gdal: algorithms")
        self.chk_include_gdal.setChecked(True)
        algo_layout.addWidget(self.chk_include_gdal)

        self.chk_include_grass = QCheckBox("Include grass: algorithms")
        self.chk_include_grass.setChecked(False)
        algo_layout.addWidget(self.chk_include_grass)

        self.chk_include_saga = QCheckBox("Include saga: algorithms")
        self.chk_include_saga.setChecked(False)
        algo_layout.addWidget(self.chk_include_saga)

        self.chk_include_all_providers = QCheckBox("Include ALL providers (full registry scan)")
        self.chk_include_all_providers.setChecked(False)
        self.chk_include_all_providers.setToolTip(
            "Scans the entire Processing registry. Slow and uses a lot of context."
        )
        algo_layout.addWidget(self.chk_include_all_providers)

        algo_group.setLayout(algo_layout)
        layout.addWidget(algo_group)

        btn_apply = QPushButton("Apply Settings")
        btn_apply.setStyleSheet("font-weight: bold;")
        btn_apply.clicked.connect(self._apply_settings)
        layout.addWidget(btn_apply)

        layout.addStretch()
        tab.setLayout(layout)

        idx = self.cmb_backend.findData(self.saved_backend)
        if idx >= 0:
            self.cmb_backend.setCurrentIndex(idx)
        self.txt_url.setText(self.saved_url)
        self.txt_api_key.setText(self.saved_api_key)
        self.txt_model.setText(self.saved_model)
        self.sld_temperature.setValue(int(self.saved_temperature * 10))

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
            QMessageBox.information(self, "Connection OK", "Successfully connected to the LLM backend.")
        else:
            QMessageBox.warning(self, "Connection Failed", "Could not connect. Check URL and API key.")

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
        return {
            "max_algorithms": self.spn_max_algos.value(),
            "include_native": self.chk_include_all_native.isChecked(),
            "include_gdal": self.chk_include_gdal.isChecked(),
            "include_grass": self.chk_include_grass.isChecked(),
            "include_saga": self.chk_include_saga.isChecked(),
            "include_all": self.chk_include_all_providers.isChecked(),
        }

    def _load_settings(self):
        s = QSettings()
        self.saved_backend = s.value(SETTINGS_PREFIX + "backend", "ollama")
        self.saved_url = s.value(SETTINGS_PREFIX + "url", "http://localhost:11434")
        self.saved_api_key = s.value(SETTINGS_PREFIX + "api_key", "")
        self.saved_model = s.value(SETTINGS_PREFIX + "model", "qwen2.5-coder:7b")
        try:
            self.saved_temperature = float(s.value(SETTINGS_PREFIX + "temperature", 0.2))
        except (TypeError, ValueError):
            self.saved_temperature = 0.2

    def _save_settings(self):
        s = QSettings()
        key = self.cmb_backend.itemData(self.cmb_backend.currentIndex())
        s.setValue(SETTINGS_PREFIX + "backend", key)
        s.setValue(SETTINGS_PREFIX + "url", self.txt_url.text())
        s.setValue(SETTINGS_PREFIX + "api_key", self.txt_api_key.text())
        s.setValue(SETTINGS_PREFIX + "model", self.txt_model.text())
        s.setValue(SETTINGS_PREFIX + "temperature", self.sld_temperature.value() / 10.0)

    def _on_generate(self):
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
            self.backend, description,
            self.txt_model_name.text(), self.txt_model_group.text(),
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

        plan = workflow.pop("_plan", None)

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

    def _on_generate_error(self, error_msg):
        self.progress_bar.hide()
        self.btn_generate.setEnabled(True)
        self.lbl_status.setText("Generation failed.")

        # Try to show raw LLM text if it's embedded in the error
        if "Raw response snippet:" in error_msg:
            parts = error_msg.split("Raw response snippet:\n\n", 1)
            msg = parts[0].strip()
            raw = parts[1] if len(parts) > 1 else ""
            if raw:
                self.txt_model_json.setPlainText(raw)
                self.lbl_validity.setText(
                    "\u26a0 Invalid JSON. Raw LLM response shown for debugging.\n" + msg
                )
                self.lbl_validity.setStyleSheet("color: orange; font-weight: bold;")
                self.tabs.setCurrentIndex(1)
                return

        QMessageBox.critical(self, "Generation Error", "Error generating model:\n" + error_msg)

    def _try_build_model(self, workflow):
        try:
            model = self.builder.build_model(
                workflow,
                model_name=self.txt_model_name.text(),
                model_group=self.txt_model_group.text(),
            )
            self.current_model = model
            self.btn_save.setEnabled(True)
            self.btn_open_designer.setEnabled(True)
        except Exception as e:
            self.current_model = None
            self.btn_save.setEnabled(False)
            self.btn_open_designer.setEnabled(False)
            self.status_label.setText("Build warning: " + str(e))

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
            QMessageBox.warning(self, "Model Forge",
                                "JSON must have 'inputs' and 'algorithms' keys.")
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
        from qgis.core import QgsProcessingModelAlgorithm
        from processing.modeler.ModelerDialog import ModelerDialog

        if not self._current_model_json:
            QMessageBox.warning(self, "Model Forge", "No current workflow to open.")
            return

        try:
            model = self.builder.build_model(
                self._current_model_json,
                self.txt_model_name.text(),
                self.txt_model_group.text(),
            )

            tmp = tempfile.NamedTemporaryFile(suffix=".model3", delete=False)
            tmp_path = tmp.name
            tmp.close()

            if not model.toFile(tmp_path):
                QMessageBox.warning(self, "Model Forge",
                                    "Could not create temporary model file.")
                return

            file_model = QgsProcessingModelAlgorithm()
            file_model.fromFile(tmp_path)

            dlg = ModelerDialog(file_model)
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()
            self._designer_dlg = dlg

        except Exception as e:
            QMessageBox.critical(
                self, "Model Forge",
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
            self.backend, workflow, errors, self.current_context_text,
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
        all_errors = errors + [f"USER FEEDBACK: {feedback}"]

        self.repair_worker = RepairWorker(
            self.backend, workflow, all_errors, self.current_context_text,
        )
        self.repair_worker.finished.connect(self._on_repair_finished)
        self.repair_worker.error.connect(self._on_repair_error)
        self.repair_worker.start()

    def _on_repair_finished(self, repaired):
        self.progress_bar.hide()
        self.btn_auto_repair.setEnabled(True)
        self.btn_improve.setEnabled(True)

        if "inputs" not in repaired or "algorithms" not in repaired:
            self.lbl_validity.setText("Repair response missing required keys.")
            self.lbl_validity.setStyleSheet("color: red;")
            raw_text = json.dumps(repaired, indent=2, ensure_ascii=False)
            self.txt_model_json.setPlainText(raw_text)
            return

        repaired = compute_layout(repaired)
        self._current_model_json = repaired
        self.txt_model_json.setPlainText(json.dumps(repaired, indent=2, ensure_ascii=False))
        self.lbl_validity.setText("\u2713 Model repaired/improved.")
        self.lbl_validity.setStyleSheet("color: #4CAF50; font-weight: bold;")
        self.status_label.setText("Model improved successfully.")
        self._try_build_model(repaired)

    def _on_repair_error(self, error_msg):
        self.progress_bar.hide()
        self.btn_auto_repair.setEnabled(True)
        self.btn_improve.setEnabled(True)
        self.lbl_validity.setText("Repair failed: " + error_msg)
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
