"""
Main widget for Model Forge plugin.
Two tabs: Generate (create models) and Model (view/debug/improve).
"""

import os
import json
import traceback

from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal, QObject
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QPushButton, QCheckBox, QListWidget, QListWidgetItem,
    QTextEdit, QPlainTextEdit, QLineEdit, QComboBox,
    QMessageBox, QTabWidget, QAbstractItemView, QSlider,
    QSpinBox, QFileDialog, QGridLayout,
)
from qgis.core import QgsProject

from .llm_backend import LLMBackend
from .model_layout import compute_layout
from .context_collector import ContextCollector


# ── Background worker for LLM calls ──────────────────────────

class LLMWorker(QObject):
    finished = pyqtSignal(object)
    error = pyqtSignal(str, str)  # (message, raw_response_or_empty)

    def __init__(self, func, *args):
        super().__init__()
        self.func = func
        self.args = args
        self._cancelled = False

    def run(self):
        if self._cancelled:
            return
        try:
            result = self.func(*self.args)
            if not self._cancelled:
                self.finished.emit(result)
        except ValueError as e:
            if not self._cancelled:
                msg = str(e)
                raw = ""
                if "Raw response snippet:" in msg:
                    parts = msg.split("Raw response snippet:\n\n", 1)
                    msg = parts[0].strip()
                    raw = parts[1] if len(parts) > 1 else ""
                self.error.emit(msg, raw)
        except Exception as e:
            if not self._cancelled:
                self.error.emit(str(e), "")

    def cancel(self):
        self._cancelled = True


class ForgeWidget(QWidget):
    """Main Model Forge widget with Generate + Model tabs."""

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.context_collector = ContextCollector()
        self.backend = LLMBackend()
        self.worker = None
        self.thread = None
        self._current_model_json = None
        self._init_ui()
        self._load_layers()
        self._connect_project_signals()

    # ── UI Construction ───────────────────────────────────────

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

    # ── Generate Tab ──────────────────────────────────────────

    def _create_generate_tab(self):
        tab = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(6)

        # Workflow description
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

        # Model metadata
        meta_layout = QHBoxLayout()
        meta_layout.addWidget(QLabel("Name:"))
        self.txt_model_name = QLineEdit("my_workflow")
        meta_layout.addWidget(self.txt_model_name)
        meta_layout.addWidget(QLabel("Group:"))
        self.txt_model_group = QLineEdit("Model Forge")
        meta_layout.addWidget(self.txt_model_group)
        layout.addLayout(meta_layout)

        # Context layers
        layers_group = QGroupBox("Context layers")
        layers_layout = QVBoxLayout()
        layers_layout.setSpacing(4)
        self.layer_list = QListWidget()
        self.layer_list.setSelectionMode(QAbstractItemView.MultiSelection)
        self.layer_list.setMaximumHeight(120)
        layers_layout.addWidget(self.layer_list)

        # Select all / Deselect all buttons
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

        # Options
        self.chk_two_phase = QCheckBox("Two-phase generation (plan then build)")
        self.chk_two_phase.setChecked(False)  # unchecked by default
        self.chk_two_phase.setToolTip(
            "When checked, the LLM first creates a plan, then builds the model. "
            "More reliable for complex workflows, but slower."
        )
        layout.addWidget(self.chk_two_phase)

        # Generate / Cancel buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.btn_generate = QPushButton("Generate Model")
        self.btn_generate.setStyleSheet("font-weight: bold;")
        self.btn_generate.clicked.connect(self._on_generate)
        btn_layout.addWidget(self.btn_generate)

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._on_cancel)
        btn_layout.addWidget(self.btn_cancel)
        layout.addLayout(btn_layout)

        layout.addStretch()
        tab.setLayout(layout)
        return tab

    # ── Model Tab (Debug / Improve) ───────────────────────────

    def _create_model_tab(self):
        tab = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(6)

        # JSON editor
        json_group = QGroupBox("Model JSON")
        json_layout = QVBoxLayout()
        self.txt_model_json = QPlainTextEdit()
        self.txt_model_json.setPlaceholderText("Model JSON will appear here after generation...")
        json_layout.addWidget(self.txt_model_json)

        json_btn_layout = QHBoxLayout()
        btn_copy = QPushButton("Copy")
        btn_copy.clicked.connect(self._copy_json)
        json_btn_layout.addWidget(btn_copy)
        btn_save = QPushButton("Save .model3")
        btn_save.clicked.connect(self._save_model)
        json_btn_layout.addWidget(btn_save)
        btn_load = QPushButton("Load .model3")
        btn_load.clicked.connect(self._load_model)
        json_btn_layout.addWidget(btn_load)
        json_layout.addLayout(json_btn_layout)
        json_group.setLayout(json_layout)
        layout.addWidget(json_group)

        # Validity indicator
        self.lbl_validity = QLabel("")
        self.lbl_validity.setWordWrap(True)
        layout.addWidget(self.lbl_validity)

        # Debug / Improve section
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
        btn_auto_repair = QPushButton("Auto-Repair")
        btn_auto_repair.setToolTip("Validate the JSON and ask the LLM to fix any errors")
        btn_auto_repair.clicked.connect(self._on_auto_repair)
        improve_btn_layout.addWidget(btn_auto_repair)

        btn_improve = QPushButton("Send to LLM")
        btn_improve.setToolTip("Send the current JSON + your feedback to the LLM for improvement")
        btn_improve.clicked.connect(self._on_improve)
        improve_btn_layout.addWidget(btn_improve)
        improve_layout.addLayout(improve_btn_layout)

        improve_group.setLayout(improve_layout)
        layout.addWidget(improve_group)

        layout.addStretch()
        tab.setLayout(layout)
        return tab

    # ── Settings Tab ──────────────────────────────────────────

    def _create_settings_tab(self):
        tab = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(6)

        # Backend selection
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

        # Temperature / thinking level
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

        # Algorithm catalog configuration
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
            "Maximum number of algorithm signatures to include in the LLM context. "
            "Higher = more tools available but slower and may exceed context window."
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
            "Scans the entire Processing registry. Slow and uses a lot of context. "
            "Enable only for specialized tools from third-party plugins."
        )
        algo_layout.addWidget(self.chk_include_all_providers)

        algo_group.setLayout(algo_layout)
        layout.addWidget(algo_group)

        # Apply settings button
        btn_apply = QPushButton("Apply Settings")
        btn_apply.setStyleSheet("font-weight: bold;")
        btn_apply.clicked.connect(self._apply_settings)
        layout.addWidget(btn_apply)

        layout.addStretch()
        tab.setLayout(layout)
        return tab

    # ── Layer management ──────────────────────────────────────

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

    # ── Settings handlers ─────────────────────────────────────

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
        self.status_label.setText("Settings applied.")

    def _get_algo_config(self):
        """Return a dict describing which algorithm providers to include."""
        return {
            "max_algorithms": self.spn_max_algos.value(),
            "include_native": self.chk_include_all_native.isChecked(),
            "include_gdal": self.chk_include_gdal.isChecked(),
            "include_grass": self.chk_include_grass.isChecked(),
            "include_saga": self.chk_include_saga.isChecked(),
            "include_all": self.chk_include_all_providers.isChecked(),
        }

    # ── Generate handlers ─────────────────────────────────────

    def _on_generate(self):
        description = self.txt_description.toPlainText().strip()
        if not description:
            QMessageBox.warning(self, "No Description", "Please describe your workflow.")
            return

        self._apply_settings()
        self.btn_generate.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.status_label.setText("Generating model...")

        layers = self._get_selected_layers()
        algo_config = self._get_algo_config()
        context_text = self.context_collector.collect(layers, algo_config)

        if self.chk_two_phase.isChecked():
            worker_func = self._two_phase_generate
            args = (description, context_text)
        else:
            worker_func = self.backend.generate_single_pass
            args = (
                description,
                self.txt_model_name.text().strip(),
                self.txt_model_group.text().strip(),
                context_text,
            )

        self._start_worker(worker_func, args, self._on_generate_success, self._on_generate_error)

    def _two_phase_generate(self, description, context_text):
        plan = self.backend.generate_plan(description, context_text)
        return self.backend.generate_model_from_plan(plan, context_text)

    def _on_generate_success(self, result):
        self._finish_worker()
        # Apply auto-layout
        result = compute_layout(result)
        self._current_model_json = result
        pretty = json.dumps(result, indent=2, ensure_ascii=False)
        self.txt_model_json.setPlainText(pretty)
        self.lbl_validity.setText("✅ Valid JSON structure received.")
        self.lbl_validity.setStyleSheet("color: green; font-weight: bold;")
        self.tabs.setCurrentIndex(1)  # switch to Model tab
        self.status_label.setText("Model generated successfully.")

    def _on_generate_error(self, message, raw_response):
        self._finish_worker()
        if raw_response:
            # Show the raw response in the JSON editor so the user can inspect/debug it
            self.txt_model_json.setPlainText(raw_response)
            self.lbl_validity.setText(
                "⚠️ Invalid JSON. The raw LLM response is shown below for debugging.\n"
                + message
            )
            self.lbl_validity.setStyleSheet("color: orange; font-weight: bold;")
            self.tabs.setCurrentIndex(1)
            self.status_label.setText("Generation returned invalid JSON — shown for debugging.")
        else:
            QMessageBox.warning(
                self, "Generation Error",
                message + "\n\nTry:\n"
                "• A shorter or simpler description\n"
                "• Lowering the thinking level\n"
                "• Switching to another model"
            )
            self.status_label.setText("Generation failed.")

    def _on_cancel(self):
        if self.worker:
            self.worker.cancel()
        self._finish_worker()
        self.status_label.setText("Generation cancelled.")

    def _finish_worker(self):
        self.btn_generate.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        if self.thread and self.thread.isRunning():
            self.thread.quit()
            self.thread.wait(3000)
        self.worker = None
        self.thread = None

    def _start_worker(self, func, args, on_success, on_error):
        self.thread = QThread()
        self.worker = LLMWorker(func, *args)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.finished.connect(on_success)
        self.worker.error.connect(on_error)
        self.worker.finished.connect(self.thread.quit)
        self.worker.error.connect(self.thread.quit)
        self.thread.start()

    # ── Model tab handlers ────────────────────────────────────

    def _copy_json(self):
        from qgis.PyQt.QtWidgets import QApplication
        QApplication.clipboard().setText(self.txt_model_json.toPlainText())
        self.status_label.setText("JSON copied to clipboard.")

    def _save_model(self):
        text = self.txt_model_json.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Empty", "No model JSON to save.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Model", "", "QGIS Model (*.model3)"
        )
        if path:
            if not path.endswith(".model3"):
                path += ".model3"
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            self.status_label.setText(f"Model saved to {path}")

    def _load_model(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Model", "", "QGIS Model (*.model3);;JSON (*.json)"
        )
        if path:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            self.txt_model_json.setPlainText(content)
            try:
                self._current_model_json = json.loads(content)
                self.lbl_validity.setText("✅ Loaded valid JSON.")
                self.lbl_validity.setStyleSheet("color: green; font-weight: bold;")
            except json.JSONDecodeError as e:
                self.lbl_validity.setText(f"⚠️ Loaded file but JSON is invalid: {e}")
                self.lbl_validity.setStyleSheet("color: orange; font-weight: bold;")
            self.status_label.setText(f"Loaded model from {path}")

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
        self.status_label.setText("Auto-repairing...")
        self.btn_generate.setEnabled(False)
        self.btn_cancel.setEnabled(True)

        layers = self._get_selected_layers()
        algo_config = self._get_algo_config()
        context_text = self.context_collector.collect(layers, algo_config)

        self._start_worker(
            self.backend.repair_model, (workflow, errors, context_text),
            self._on_repair_success, self._on_generate_error
        )

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
        self.status_label.setText("Improving model...")
        self.btn_generate.setEnabled(False)
        self.btn_cancel.setEnabled(True)

        layers = self._get_selected_layers()
        algo_config = self._get_algo_config()
        context_text = self.context_collector.collect(layers, algo_config)

        errors = self._validate_model(workflow)
        all_feedback = errors + [f"USER FEEDBACK: {feedback}"]

        self._start_worker(
            self.backend.repair_model, (workflow, all_feedback, context_text),
            self._on_repair_success, self._on_generate_error
        )

    def _on_repair_success(self, result):
        self._finish_worker()
        result = compute_layout(result)
        self._current_model_json = result
        pretty = json.dumps(result, indent=2, ensure_ascii=False)
        self.txt_model_json.setPlainText(pretty)
        self.lbl_validity.setText("✅ Model repaired/improved.")
        self.lbl_validity.setStyleSheet("color: green; font-weight: bold;")
        self.status_label.setText("Model improved successfully.")

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
