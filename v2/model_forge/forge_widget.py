"""
Model Forge main widget with three tabs:
- Generate: description input, layer selection, two-phase generation
- Model: color-coded JSON preview, debug prompt for repair
- Settings: backend config (Ollama / OpenAI-compatible)
"""

import json
import os
import traceback
from qgis.core import QgsProject, QgsApplication, Qgis
from qgis.PyQt.QtCore import QSettings, Qt, QThread, pyqtSignal
from qgis.PyQt.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat
from qgis.PyQt.QtWidgets import (
    QWidget, QTabWidget, QVBoxLayout, QHBoxLayout, QLabel, QTextEdit,
    QLineEdit, QComboBox, QPushButton, QListWidget, QListWidgetItem,
    QGroupBox, QFormLayout, QCheckBox, QMessageBox, QFileDialog,
    QProgressBar, QAbstractItemView, QSlider, QSplitter,
)
from .llm_backend import LLMBackend
from .model_builder import ModelBuilder
from .context_collector import ContextCollector

SETTINGS_PREFIX = "ModelForge/"


# ---- JSON Syntax Highlighter ----

class JsonHighlighter(QSyntaxHighlighter):
    """Color-codes JSON in the Model tab."""

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

        self.fmt_algorithm_id = QTextCharFormat()
        self.fmt_algorithm_id.setForeground(QColor("#61AFEF"))
        self.fmt_algorithm_id.setFontWeight(QFont.Bold)

    def highlightBlock(self, text):
        import re

        for m in re.finditer(r'"(native:|gdal:|qgis:|saga:)[^"]*"', text):
            self.setFormat(m.start(), m.end() - m.start(), self.fmt_algorithm_id)

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


# ---- Worker thread for LLM calls ----

class GenerateWorker(QThread):
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, backend, description, model_name, model_group, context_text, two_phase=True):
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
                self.progress.emit("Phase 1: Generating high-level plan...")
                plan = self.backend.generate_plan(self.description, self.context_text)
                self.progress.emit("Phase 2: Converting plan to model definition...")
                result = self.backend.generate_model_from_plan(plan, self.context_text)
                result["_plan"] = plan
            else:
                self.progress.emit("Generating model definition...")
                result = self.backend.generate_single_pass(
                    self.description, self.model_name, self.model_group, self.context_text
                )
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(f"{str(e)}\n\n{traceback.format_exc()}")


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


# ---- Main Widget ----

class ForgeWidget(QWidget):
    """Main Model Forge widget with three tabs."""

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.backend = LLMBackend()
        self.builder = ModelBuilder()
        self.collector = ContextCollector()
        self.current_workflow = None
        self.current_model = None
        self.current_context_text = ""
        self.validation_errors = []
        self.worker = None

        self._load_settings()
        self._build_ui()
        self._connect_signals()
        self._refresh_layers()

    # ======== UI BUILDING ========

    def _build_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_generate_tab(), "Generate")
        self.tabs.addTab(self._build_model_tab(), "Model")
        self.tabs.addTab(self._build_settings_tab(), "Settings")

        layout.addWidget(self.tabs)
        self.setLayout(layout)

    def _build_generate_tab(self):
        tab = QWidget()
        layout = QVBoxLayout()

        # Description
        layout.addWidget(QLabel("Describe your workflow:"))
        self.txt_description = QTextEdit()
        self.txt_description.setPlaceholderText(
            "Example: Take a polygon layer and a clip area. "
            "Clip the polygons to the area, dissolve by class field, "
            "then calculate total area per class."
        )
        self.txt_description.setMaximumHeight(120)
        layout.addWidget(self.txt_description)

        # Model name / group
        name_group = QHBoxLayout()
        name_group.addWidget(QLabel("Name:"))
        self.txt_model_name = QLineEdit("my_model")
        name_group.addWidget(self.txt_model_name)
        name_group.addWidget(QLabel("Group:"))
        self.txt_model_group = QLineEdit("Model Forge")
        name_group.addWidget(self.txt_model_group)
        layout.addLayout(name_group)

        # Layer selection
        grp_layers = QGroupBox("Context layers (optional)")
        grp_layout = QVBoxLayout()
        self.lst_layers = QListWidget()
        self.lst_layers.setSelectionMode(QAbstractItemView.MultiSelection)
        self.lst_layers.setMaximumHeight(100)
        grp_layout.addWidget(self.lst_layers)

        btn_refresh = QPushButton("Refresh layers")
        btn_refresh.clicked.connect(self._refresh_layers)
        grp_layout.addWidget(btn_refresh)
        grp_layers.setLayout(grp_layout)
        layout.addWidget(grp_layers)

        # Two-phase checkbox
        self.chk_two_phase = QCheckBox("Two-phase generation (plan then build, more accurate)")
        self.chk_two_phase.setChecked(True)
        layout.addWidget(self.chk_two_phase)

        # Auto-validate checkbox
        self.chk_auto_validate = QCheckBox("Auto-validate and attempt repair")
        self.chk_auto_validate.setChecked(True)
        layout.addWidget(self.chk_auto_validate)

        # Progress
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)

        self.lbl_status = QLabel("")
        layout.addWidget(self.lbl_status)

        # Generate button
        self.btn_generate = QPushButton("Generate Model")
        self.btn_generate.setStyleSheet(
            "QPushButton { background-color: #4CAF50; color: white; "
            "font-weight: bold; padding: 8px; border-radius: 4px; } "
            "QPushButton:hover { background-color: #45a049; }"
        )
        self.btn_generate.clicked.connect(self._on_generate)
        layout.addWidget(self.btn_generate)

        # Context info
        self.lbl_context_info = QLabel("")
        self.lbl_context_info.setWordWrap(True)
        self.lbl_context_info.setStyleSheet("color: gray; font-size: 10px;")
        layout.addWidget(self.lbl_context_info)

        layout.addStretch()
        tab.setLayout(layout)
        return tab

    def _build_model_tab(self):
        tab = QWidget()
        layout = QVBoxLayout()

        # Validation status
        self.lbl_validation = QLabel("No model generated yet.")
        self.lbl_validation.setWordWrap(True)
        layout.addWidget(self.lbl_validation)

        # Color-coded JSON preview
        layout.addWidget(QLabel("Model JSON:"))
        self.txt_model_json = QTextEdit()
        self.txt_model_json.setReadOnly(True)
        self.txt_model_json.setStyleSheet(
            "QTextEdit { background-color: #282C34; color: #ABB2BF; "
            "font-family: Consolas, monospace; font-size: 11px; }"
        )
        font = QFont("Consolas", 10)
        font.setStyleHint(QFont.Monospace)
        self.txt_model_json.setFont(font)
        self.highlighter = JsonHighlighter(self.txt_model_json.document())
        layout.addWidget(self.txt_model_json, stretch=3)

        # Debug / repair section
        grp_debug = QGroupBox("Debug / Repair")
        debug_layout = QVBoxLayout()

        self.txt_debug_prompt = QTextEdit()
        self.txt_debug_prompt.setPlaceholderText(
            "Describe what is wrong or what to change, "
            "e.g. 'The dissolve field should come from the input layer' "
            "or 'Add a buffer step before the clip'"
        )
        self.txt_debug_prompt.setMaximumHeight(70)
        debug_layout.addWidget(self.txt_debug_prompt)

        debug_btns = QHBoxLayout()
        self.btn_auto_repair = QPushButton("Auto-Repair (from validation)")
        self.btn_auto_repair.clicked.connect(self._on_auto_repair)
        self.btn_auto_repair.setEnabled(False)
        debug_btns.addWidget(self.btn_auto_repair)

        self.btn_debug_send = QPushButton("Send repair prompt")
        self.btn_debug_send.clicked.connect(self._on_debug_repair)
        self.btn_debug_send.setEnabled(False)
        debug_btns.addWidget(self.btn_debug_send)
        debug_layout.addLayout(debug_btns)

        grp_debug.setLayout(debug_layout)
        layout.addWidget(grp_debug)

        # Action buttons
        btn_row = QHBoxLayout()
        self.btn_save = QPushButton("Save .model3")
        self.btn_save.clicked.connect(self._on_save)
        self.btn_save.setEnabled(False)
        btn_row.addWidget(self.btn_save)

        self.btn_open_designer = QPushButton("Open in Designer")
        self.btn_open_designer.clicked.connect(self._on_open_designer)
        self.btn_open_designer.setEnabled(False)
        btn_row.addWidget(self.btn_open_designer)
        layout.addLayout(btn_row)

        tab.setLayout(layout)
        return tab

    def _build_settings_tab(self):
        tab = QWidget()
        layout = QVBoxLayout()

        # Backend type
        grp_backend = QGroupBox("LLM Backend")
        form = QFormLayout()

        self.cmb_backend = QComboBox()
        self.cmb_backend.addItems(["ollama", "openai"])
        self.cmb_backend.currentTextChanged.connect(self._on_backend_changed)
        form.addRow("Backend:", self.cmb_backend)

        self.txt_url = QLineEdit()
        self.txt_url.setPlaceholderText("http://localhost:11434")
        form.addRow("API URL:", self.txt_url)

        self.txt_api_key = QLineEdit()
        self.txt_api_key.setEchoMode(QLineEdit.Password)
        self.txt_api_key.setPlaceholderText("sk-... (OpenAI) or leave empty (Ollama)")
        form.addRow("API Key:", self.txt_api_key)

        self.txt_model = QLineEdit()
        self.txt_model.setPlaceholderText("qwen2.5-coder:7b")
        form.addRow("Model:", self.txt_model)

        self.sld_temperature = QSlider(Qt.Horizontal)
        self.sld_temperature.setRange(0, 100)
        self.sld_temperature.setValue(20)
        self.lbl_temp = QLabel("0.20")
        self.sld_temperature.valueChanged.connect(
            lambda v: self.lbl_temp.setText(f"{v / 100:.2f}")
        )
        temp_row = QHBoxLayout()
        temp_row.addWidget(self.sld_temperature)
        temp_row.addWidget(self.lbl_temp)
        form.addRow("Temperature:", temp_row)

        grp_backend.setLayout(form)
        layout.addWidget(grp_backend)

        # Test + save
        btn_row = QHBoxLayout()
        self.btn_test = QPushButton("Test Connection")
        self.btn_test.clicked.connect(self._on_test_connection)
        btn_row.addWidget(self.btn_test)

        self.btn_save_settings = QPushButton("Save Settings")
        self.btn_save_settings.clicked.connect(self._save_settings)
        btn_row.addWidget(self.btn_save_settings)
        layout.addLayout(btn_row)

        self.lbl_test_result = QLabel("")
        layout.addWidget(self.lbl_test_result)

        # Context info
        grp_context = QGroupBox("Session Context")
        ctx_layout = QVBoxLayout()
        self.lbl_qgis_version = QLabel("")
        ctx_layout.addWidget(self.lbl_qgis_version)
        self.lbl_alg_count = QLabel("")
        ctx_layout.addWidget(self.lbl_alg_count)

        self.btn_refresh_catalog = QPushButton("Refresh algorithm catalog")
        self.btn_refresh_catalog.clicked.connect(self._refresh_catalog_info)
        ctx_layout.addWidget(self.btn_refresh_catalog)
        grp_context.setLayout(ctx_layout)
        layout.addWidget(grp_context)

        layout.addStretch()
        tab.setLayout(layout)
        return tab

    # ======== SIGNALS ========

    def _connect_signals(self):
        QgsProject.instance().layersAdded.connect(self._refresh_layers)
        QgsProject.instance().layersRemoved.connect(self._refresh_layers)

    def disconnect_signals(self):
        try:
            QgsProject.instance().layersAdded.disconnect(self._refresh_layers)
            QgsProject.instance().layersRemoved.disconnect(self._refresh_layers)
        except:
            pass

    # ======== LAYER + CATALOG ========

    def _refresh_layers(self, *args):
        self.lst_layers.clear()
        for layer in QgsProject.instance().mapLayers().values():
            item = QListWidgetItem(layer.name())
            item.setData(Qt.UserRole, layer.id())
            self.lst_layers.addItem(item)
        self._refresh_catalog_info()

    def _refresh_catalog_info(self):
        try:
            ver = self.collector.get_qgis_version()
            self.lbl_qgis_version.setText(f"QGIS: {ver}")
            catalog = self.collector.get_algorithm_catalog()
            self.lbl_alg_count.setText(f"Curated algorithms: {len(catalog)} available")
            providers = self.collector.get_providers_summary()
            total = sum(p["algorithm_count"] for p in providers)
            self.lbl_alg_count.setText(
                f"Curated algorithms: {len(catalog)} | Total in registry: {total}"
            )
        except Exception as e:
            self.lbl_qgis_version.setText(f"Error reading context: {e}")

    # ======== GENERATION ========

    def _get_selected_layer_ids(self):
        ids = []
        for item in self.lst_layers.selectedItems():
            ids.append(item.data(Qt.UserRole))
        return ids

    def _on_generate(self):
        description = self.txt_description.toPlainText().strip()
        if not description:
            QMessageBox.warning(self, "Model Forge", "Please enter a workflow description.")
            return

        self._apply_backend_settings()

        selected_ids = self._get_selected_layer_ids()
        context = self.collector.build_full_context(selected_ids if selected_ids else None)
        self.current_context_text = self.collector.context_to_prompt_text(context)

        n_layers = len(context.get("selected_layers", context.get("layers", [])))
        n_algs = len(context.get("algorithms", {}))
        self.lbl_context_info.setText(
            f"Context sent: {n_layers} layers, {n_algs} algorithms, QGIS {context['qgis_version']}"
        )

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

        plan = workflow.pop("_plan", None)

        if "inputs" not in workflow or "algorithms" not in workflow:
            self.lbl_status.setText("LLM response missing 'inputs' or 'algorithms' keys.")
            QMessageBox.warning(self, "Generation Error",
                                f"LLM response missing required keys. Got: {list(workflow.keys())}")
            return

        self.current_workflow = workflow
        self.lbl_status.setText("Model generated! See Model tab.")

        json_text = json.dumps(workflow, indent=2)
        self.txt_model_json.setPlainText(json_text)

        self.tabs.setCurrentIndex(1)

        try:
            model = self.builder.build_model(
                workflow,
                model_name=self.txt_model_name.text(),
                model_group=self.txt_model_group.text(),
            )
            self.current_model = model

            self.btn_save.setEnabled(True)
            self.btn_open_designer.setEnabled(True)
            self.btn_debug_send.setEnabled(True)

            if self.chk_auto_validate.isChecked():
                self._run_validation_and_repair(model, workflow)
            else:
                self.lbl_validation.setText("Model built. Validation not run.")
                self.lbl_validation.setStyleSheet("color: orange;")

        except Exception as e:
            self.current_model = None
            self.lbl_validation.setText(f"Build error: {e}")
            self.lbl_validation.setStyleSheet("color: red;")
            self.btn_save.setEnabled(False)
            self.btn_open_designer.setEnabled(False)
            self.btn_debug_send.setEnabled(True)

    def _on_generate_error(self, error_msg):
        self.progress_bar.hide()
        self.btn_generate.setEnabled(True)
        self.lbl_status.setText("Generation failed.")
        QMessageBox.critical(self, "Generation Error", f"Error generating model:\n{error_msg}")

    # ======== VALIDATION + REPAIR ========

    def _run_validation_and_repair(self, model, workflow):
        try:
            errors = self.builder.validate_model(model)
        except Exception as e:
            errors = [f"Validation call failed: {e}"]

        self.validation_errors = errors

        if not errors:
            self.lbl_validation.setText("✓ Model passed validation.")
            self.lbl_validation.setStyleSheet("color: #4CAF50; font-weight: bold;")
            self.btn_auto_repair.setEnabled(False)
        else:
            error_text = "\n".join(f"• {e}" for e in errors[:10])
            self.lbl_validation.setText(f"⚠ Validation issues ({len(errors)}):\n{error_text}")
            self.lbl_validation.setStyleSheet("color: #FF9800;")
            self.btn_auto_repair.setEnabled(True)

    def _on_auto_repair(self):
        if not self.current_workflow or not self.validation_errors:
            return

        self.btn_auto_repair.setEnabled(False)
        self.lbl_validation.setText("Sending repair request to LLM...")
        self.lbl_validation.setStyleSheet("color: #2196F3;")
        self.progress_bar.show()

        self._apply_backend_settings()

        self.repair_worker = RepairWorker(
            self.backend,
            self.current_workflow,
            self.validation_errors,
            self.current_context_text,
        )
        self.repair_worker.finished.connect(self._on_repair_finished)
        self.repair_worker.error.connect(self._on_repair_error)
        self.repair_worker.start()

    def _on_debug_repair(self):
        debug_text = self.txt_debug_prompt.toPlainText().strip()
        if not debug_text and not self.validation_errors:
            QMessageBox.information(self, "Model Forge",
                                    "Enter a description of what to fix, or run validation first.")
            return

        errors = []
        if debug_text:
            errors.append(f"User feedback: {debug_text}")
        errors.extend(self.validation_errors)

        if not self.current_workflow:
            QMessageBox.warning(self, "Model Forge", "No model to repair. Generate one first.")
            return

        self.btn_debug_send.setEnabled(False)
        self.lbl_validation.setText("Sending debug/repair request to LLM...")
        self.lbl_validation.setStyleSheet("color: #2196F3;")
        self.progress_bar.show()

        self._apply_backend_settings()

        self.repair_worker = RepairWorker(
            self.backend,
            self.current_workflow,
            errors,
            self.current_context_text,
        )
        self.repair_worker.finished.connect(self._on_repair_finished)
        self.repair_worker.error.connect(self._on_repair_error)
        self.repair_worker.start()

    def _on_repair_finished(self, repaired_workflow):
        self.progress_bar.hide()
        self.btn_auto_repair.setEnabled(True)
        self.btn_debug_send.setEnabled(True)

        if "inputs" not in repaired_workflow or "algorithms" not in repaired_workflow:
            self.lbl_validation.setText("Repair response missing required keys.")
            self.lbl_validation.setStyleSheet("color: red;")
            return

        self.current_workflow = repaired_workflow
        self.txt_model_json.setPlainText(json.dumps(repaired_workflow, indent=2))

        try:
            model = self.builder.build_model(
                repaired_workflow,
                model_name=self.txt_model_name.text(),
                model_group=self.txt_model_group.text(),
            )
            self.current_model = model
            self.btn_save.setEnabled(True)
            self.btn_open_designer.setEnabled(True)

            errors = self.builder.validate_model(model)
            self.validation_errors = errors
            if not errors:
                self.lbl_validation.setText("✓ Repaired model passed validation.")
                self.lbl_validation.setStyleSheet("color: #4CAF50; font-weight: bold;")
                self.btn_auto_repair.setEnabled(False)
            else:
                error_text = "\n".join(f"• {e}" for e in errors[:10])
                self.lbl_validation.setText(
                    f"⚠ Repaired, but {len(errors)} issues remain:\n{error_text}"
                )
                self.lbl_validation.setStyleSheet("color: #FF9800;")
                self.btn_auto_repair.setEnabled(True)

        except Exception as e:
            self.lbl_validation.setText(f"Rebuild after repair failed: {e}")
            self.lbl_validation.setStyleSheet("color: red;")

    def _on_repair_error(self, error_msg):
        self.progress_bar.hide()
        self.btn_auto_repair.setEnabled(True)
        self.btn_debug_send.setEnabled(True)
        self.lbl_validation.setText(f"Repair failed: {error_msg}")
        self.lbl_validation.setStyleSheet("color: red;")

    # ======== SAVE / OPEN ========

    def _on_save(self):
        if not self.current_model:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Model", "", "QGIS Models (*.model3)"
        )
        if path:
            if not path.endswith(".model3"):
                path += ".model3"
            self.current_model.setSourceFilePath(path)
            if self.current_model.toFile(path):
                QMessageBox.information(self, "Model Forge", f"Model saved to:\n{path}")
            else:
                QMessageBox.warning(self, "Model Forge", "Failed to save model.")

    def _on_open_designer(self):
        if not self.current_model:
            return
        try:
            import tempfile
            tmp = os.path.join(tempfile.gettempdir(), "model_forge_temp.model3")
            self.current_model.setSourceFilePath(tmp)
            self.current_model.toFile(tmp)

            self.iface.openModelDesigner(tmp)
        except AttributeError:
            try:
                from processing.modeler.ModelerDialog import ModelerDialog
                dlg = ModelerDialog()
                self.current_model.toFile(tmp)
                dlg.loadModel(tmp)
                dlg.show()
            except Exception as e2:
                QMessageBox.warning(
                    self, "Model Forge",
                    f"Could not open Model Designer:\n{e2}\n\n"
                    f"Model saved to {tmp}. Open it manually from Processing Toolbox."
                )

    # ======== SETTINGS ========

    def _on_backend_changed(self, backend):
        is_openai = (backend == "openai")
        self.txt_api_key.setEnabled(is_openai)
        if not is_openai:
            self.txt_url.setPlaceholderText("http://localhost:11434")
        else:
            self.txt_url.setPlaceholderText("https://api.openai.com/v1")

    def _on_test_connection(self):
        self._apply_backend_settings()
        ok = self.backend.test_connection()
        if ok:
            self.lbl_test_result.setText("✓ Connection successful!")
            self.lbl_test_result.setStyleSheet("color: green;")
        else:
            self.lbl_test_result.setText("✗ Connection failed. Check URL and settings.")
            self.lbl_test_result.setStyleSheet("color: red;")

    def _apply_backend_settings(self):
        self.backend.configure(
            backend=self.cmb_backend.currentText(),
            url=self.txt_url.text().strip(),
            api_key=self.txt_api_key.text().strip(),
            model=self.txt_model.text().strip(),
            temperature=self.sld_temperature.value() / 100.0,
        )

    def _save_settings(self):
        s = QSettings()
        s.setValue(SETTINGS_PREFIX + "backend", self.cmb_backend.currentText())
        s.setValue(SETTINGS_PREFIX + "url", self.txt_url.text())
        s.setValue(SETTINGS_PREFIX + "api_key", self.txt_api_key.text())
        s.setValue(SETTINGS_PREFIX + "model", self.txt_model.text())
        s.setValue(SETTINGS_PREFIX + "temperature", self.sld_temperature.value())
        QMessageBox.information(self, "Model Forge", "Settings saved.")

    def _load_settings(self):
        s = QSettings()
        self.saved_backend = s.value(SETTINGS_PREFIX + "backend", "ollama")
        self.saved_url = s.value(SETTINGS_PREFIX + "url", "")
        self.saved_api_key = s.value(SETTINGS_PREFIX + "api_key", "")
        self.saved_model = s.value(SETTINGS_PREFIX + "model", "")
        val = s.value(SETTINGS_PREFIX + "temperature", 20)
        try:
            self.saved_temperature = int(val)
        except (TypeError, ValueError):
            # older versions stored temperature as 0.2 float/string
            try:
                self.saved_temperature = int(float(val) * 100)
            except Exception:
                self.saved_temperature = 20

    def _build_settings_tab(self):
        tab = self.__build_settings_tab_inner()
        self.cmb_backend.setCurrentText(self.saved_backend)
        self.txt_url.setText(self.saved_url)
        self.txt_api_key.setText(self.saved_api_key)
        self.txt_model.setText(self.saved_model)
        self.sld_temperature.setValue(self.saved_temperature)
        self._on_backend_changed(self.saved_backend)
        return tab

    def __build_settings_tab_inner(self):
        tab = QWidget()
        layout = QVBoxLayout()

        grp_backend = QGroupBox("LLM Backend")
        form = QFormLayout()

        self.cmb_backend = QComboBox()
        self.cmb_backend.addItems(["ollama", "openai"])
        self.cmb_backend.currentTextChanged.connect(self._on_backend_changed)
        form.addRow("Backend:", self.cmb_backend)

        self.txt_url = QLineEdit()
        self.txt_url.setPlaceholderText("http://localhost:11434")
        form.addRow("API URL:", self.txt_url)

        self.txt_api_key = QLineEdit()
        self.txt_api_key.setEchoMode(QLineEdit.Password)
        self.txt_api_key.setPlaceholderText("sk-... or leave empty for Ollama")
        form.addRow("API Key:", self.txt_api_key)

        self.txt_model = QLineEdit()
        self.txt_model.setPlaceholderText("qwen2.5-coder:7b")
        form.addRow("Model:", self.txt_model)

        self.sld_temperature = QSlider(Qt.Horizontal)
        self.sld_temperature.setRange(0, 100)
        self.sld_temperature.setValue(20)
        self.lbl_temp = QLabel("0.20")
        self.sld_temperature.valueChanged.connect(
            lambda v: self.lbl_temp.setText(f"{v / 100:.2f}")
        )
        temp_row = QHBoxLayout()
        temp_row.addWidget(self.sld_temperature)
        temp_row.addWidget(self.lbl_temp)
        form.addRow("Temperature:", temp_row)

        grp_backend.setLayout(form)
        layout.addWidget(grp_backend)

        btn_row = QHBoxLayout()
        self.btn_test = QPushButton("Test Connection")
        self.btn_test.clicked.connect(self._on_test_connection)
        btn_row.addWidget(self.btn_test)

        self.btn_save_settings = QPushButton("Save Settings")
        self.btn_save_settings.clicked.connect(self._save_settings)
        btn_row.addWidget(self.btn_save_settings)
        layout.addLayout(btn_row)

        self.lbl_test_result = QLabel("")
        layout.addWidget(self.lbl_test_result)

        grp_context = QGroupBox("Session Context")
        ctx_layout = QVBoxLayout()
        self.lbl_qgis_version = QLabel("")
        ctx_layout.addWidget(self.lbl_qgis_version)
        self.lbl_alg_count = QLabel("")
        ctx_layout.addWidget(self.lbl_alg_count)
        self.btn_refresh_catalog = QPushButton("Refresh algorithm catalog")
        self.btn_refresh_catalog.clicked.connect(self._refresh_catalog_info)
        ctx_layout.addWidget(self.btn_refresh_catalog)
        grp_context.setLayout(ctx_layout)
        layout.addWidget(grp_context)

        layout.addStretch()
        tab.setLayout(layout)
        return tab
