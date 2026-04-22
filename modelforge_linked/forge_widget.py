import json
from datetime import datetime

from qgis.PyQt.QtCore import QThread, pyqtSignal, Qt, QSettings, QSize
from qgis.PyQt.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QComboBox,
    QPushButton,
    QMessageBox,
    QCheckBox,
    QWidget,
    QVBoxLayout,
    QListWidget,
    QListWidgetItem,
    QDialogButtonBox,
    QInputDialog,
)

from .legacy_ui.forge_widget import ForgeWidget as LegacyForgeWidget
from .compiler_core.ui.model_builder_bridge import ModelBuilderBridge
from .compiler_core.ui.custom_step_dialog import CustomStepDialog
from .compiler_core.core.context_collector import ContextCollector as CompilerContextCollector
from .compiler_core.core.llm.factory import create_backend as create_compiler_backend
from .compiler_core.core.mcp.client import DirectMCPClient
from .compiler_core.core.mcp.tool_registry import build_server
from .compiler_core.core.compiler.algorithm_resolver import AlgorithmResolver
from .compiler_core.core.compiler.expression_validator import ExpressionValidator
from .compiler_core.core.compiler.intent_parser import IntentParser
from .compiler_core.core.compiler.ir_validator import IRValidator
from .compiler_core.core.compiler.model_emitter import ModelEmitter
from .compiler_core.core.compiler.pipeline import CompilerPipeline
from .compiler_core.core.compiler.semantic_planner import SemanticPlanner
from .compiler_core.core.ir import IssueLevel
from .compiler_core.core.llm.base import LLMBackendError, LLMTimeoutError
from .compiler_core.core.services.graph_layout import GraphLayoutService


class LinkedGenerateWorker(QThread):
    finished = pyqtSignal(dict, list)
    error = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(
        self,
        description,
        model_name,
        model_group,
        llm_config,
        layout_profile,
        layout_orientation,
        layout_algorithm,
        selected_layer_ids,
        algo_config,
        optimize_generation,
    ):
        super().__init__()
        self.description = description
        self.model_name = model_name
        self.model_group = model_group
        self.llm_config = llm_config
        self.layout_profile = layout_profile
        self.layout_orientation = layout_orientation
        self.layout_algorithm = layout_algorithm
        self.selected_layer_ids = set(selected_layer_ids or [])
        self.algo_config = algo_config or {}
        self.optimize_generation = bool(optimize_generation)

    def run(self):
        try:
            self.progress.emit("Connecting to LLM backend...")
            llm = create_compiler_backend(self.llm_config)

            self.progress.emit("Collecting QGIS context...")
            max_algorithms = int(self.algo_config.get("max_algorithms", 60))
            ctx = CompilerContextCollector().collect(max_algorithms=max_algorithms)
            ctx["layers"] = self._filter_layers(ctx.get("layers", []))
            ctx["algorithms"] = self._filter_algorithms(ctx.get("algorithms", {}))

            server = build_server(llm)
            client = DirectMCPClient(server)
            pipeline = CompilerPipeline(
                intent_parser=IntentParser(),
                semantic_planner=SemanticPlanner(),
                algorithm_resolver=AlgorithmResolver(),
                expression_validator=ExpressionValidator(),
                ir_validator=IRValidator(),
                model_emitter=ModelEmitter(),
            )

            plan, model_json = self._run_optimized_pipeline(
                pipeline=pipeline,
                client=client,
                full_context=ctx,
            )

            model_json = GraphLayoutService().layout_model_json(
                model_json,
                mode=self.layout_profile,
                orientation=self.layout_orientation,
                strategy=self.layout_algorithm,
            )
            self.finished.emit(model_json, plan.issues)
        except Exception as e:
            self.error.emit(self._friendly_error_text(e))

    def _filter_layers(self, layers):
        if not self.selected_layer_ids:
            return layers
        return [layer for layer in layers if layer.get("id") in self.selected_layer_ids]

    def _filter_algorithms(self, algorithms):
        if self.algo_config.get("include_all"):
            return algorithms

        enabled = set()
        if self.algo_config.get("include_native"):
            enabled.add("native")
        if self.algo_config.get("include_gdal"):
            enabled.add("gdal")
        if self.algo_config.get("include_grass"):
            enabled.add("grass")
        if self.algo_config.get("include_saga"):
            enabled.add("saga")

        if not enabled:
            return {}

        return {
            alg_id: alg
            for alg_id, alg in algorithms.items()
            if alg_id.split(":", 1)[0] in enabled
        }

    def _run_optimized_pipeline(self, pipeline, client, full_context):
        max_attempts = 3 if self.optimize_generation else 1
        last_error = None
        base_text = " ".join((self.description or "").split())

        for attempt in range(max_attempts):
            retry_index = attempt + 1
            attempt_context = self._build_attempt_context(full_context, attempt)
            attempt_text = self._build_attempt_prompt(base_text, last_error, attempt)

            if attempt > 0:
                self.progress.emit(f"Optimizing prompt and retrying ({retry_index}/{max_attempts})...")

            try:
                plan, model_json = pipeline.run(
                    raw_text=attempt_text,
                    model_name=self.model_name,
                    model_group=self.model_group,
                    qgis_context=attempt_context,
                    mcp_client=client,
                    progress_callback=lambda msg: self.progress.emit(msg),
                )
            except Exception as e:
                last_error = e
                if retry_index < max_attempts:
                    continue
                raise

            error_issues = [i for i in plan.issues if i.level == IssueLevel.ERROR]
            if error_issues and retry_index < max_attempts and self.optimize_generation:
                last_error = RuntimeError(
                    "; ".join(issue.message for issue in error_issues[:2])
                )
                continue

            return plan, model_json

        if last_error:
            raise last_error
        raise RuntimeError("Generation failed before a model could be produced.")

    def _build_attempt_context(self, full_context, attempt):
        if attempt <= 0:
            return full_context

        layers = list(full_context.get("layers", []))
        slim_layers = []
        for layer in layers[:8]:
            item = dict(layer)
            fields = list(item.get("fields", []))
            if fields:
                item["fields"] = fields[:8]
            slim_layers.append(item)

        algorithms = full_context.get("algorithms", {})
        algo_items = list(algorithms.items())
        if attempt == 1:
            limit = min(len(algo_items), 40)
        else:
            limit = min(len(algo_items), 20)
        slim_algorithms = dict(algo_items[:limit])

        return {
            "layers": slim_layers,
            "algorithms": slim_algorithms,
            "project_crs": full_context.get("project_crs"),
            "canvas_extent": full_context.get("canvas_extent"),
        }

    def _build_attempt_prompt(self, base_text, last_error, attempt):
        if attempt <= 0 or not last_error:
            return base_text
        return (
            f"{base_text}\n\n"
            f"Retry notes: {last_error}. "
            "Use only algorithms present in the catalog. "
            "Return a compact, valid plan."
        )

    def _friendly_error_text(self, error):
        if isinstance(error, LLMTimeoutError):
            return (
                "The LLM request timed out after optimized retries. "
                "Try a smaller model context or increase backend timeout."
            )
        if isinstance(error, LLMBackendError):
            return str(error)
        message = str(error).strip()
        if not message:
            return "Generation failed due to an unexpected error."
        return message


class ForgeWidget(LegacyForgeWidget):
    def __init__(self, iface, plugin=None, parent=None):
        super().__init__(iface, parent)
        self.plugin = plugin
        self.builder = ModelBuilderBridge()
        self.current_plan_issues = []
        self._history_entries = []
        self._layout_syncing = False
        self._load_linked_settings()
        self._inject_linked_controls()

    def _inject_linked_controls(self):
        self.chk_two_phase.setText("MCP compiler pipeline (enabled)")
        self.chk_two_phase.setChecked(True)
        self.chk_two_phase.setEnabled(False)
        self.chk_two_phase.setToolTip(
            "Uses the MCP compiler pipeline stages (parse, plan, resolve, validate, emit)."
        )

        layout_algorithms = ["sugiyama", "topological", "axis_pack", "radial_shell", "ancestor_weighted"]
        compiler_controls = QHBoxLayout()
        compiler_controls.setSpacing(4)
        compiler_controls.addWidget(QLabel("Profile:"))
        self.cmb_layout_profile_generate = QComboBox()
        self.cmb_layout_profile_generate.addItems(["balanced", "compact", "dense", "spacious", "debug"])
        self._configure_compact_combo(self.cmb_layout_profile_generate, 92, 7)
        compiler_controls.addWidget(self.cmb_layout_profile_generate)
        compiler_controls.addWidget(QLabel("Org:"))
        self.cmb_layout_orientation_generate = QComboBox()
        self.cmb_layout_orientation_generate.addItems(["horizontal", "vertical", "axis"])
        self._configure_compact_combo(self.cmb_layout_orientation_generate, 90, 6)
        compiler_controls.addWidget(self.cmb_layout_orientation_generate)
        compiler_controls.addWidget(QLabel("Algo:"))
        self.cmb_layout_algorithm_generate = QComboBox()
        self.cmb_layout_algorithm_generate.addItems(layout_algorithms)
        self._configure_compact_combo(self.cmb_layout_algorithm_generate, 130, 10)
        compiler_controls.addWidget(self.cmb_layout_algorithm_generate)
        compiler_controls.addStretch()

        compiler_actions = QHBoxLayout()
        compiler_actions.setSpacing(4)
        self.chk_optimize = QCheckBox("Optimize auto-fix")
        self.chk_optimize.setChecked(True)
        self.chk_optimize.setToolTip(
            "Automatically retries with tighter prompts and reduced context to avoid timeouts/errors."
        )
        compiler_actions.addWidget(self.chk_optimize)
        compiler_actions.addStretch()
        self.btn_custom_step = QPushButton("Custom Step...")
        self.btn_custom_step.setMaximumWidth(110)
        self.btn_custom_step.clicked.connect(self._on_open_custom_step_dialog)
        compiler_actions.addWidget(self.btn_custom_step)

        generate_layout = self.tabs.widget(0).layout()
        progress_idx = generate_layout.indexOf(self.progress_bar)
        if progress_idx >= 0:
            generate_layout.insertLayout(progress_idx, compiler_actions)
            generate_layout.insertLayout(progress_idx, compiler_controls)
        else:
            generate_layout.addLayout(compiler_controls)
            generate_layout.addLayout(compiler_actions)

        model_layout = self.tabs.widget(1).layout()
        self.lbl_pipeline_issues = QLabel("")
        self.lbl_pipeline_issues.setWordWrap(True)
        self.lbl_pipeline_issues.setStyleSheet("color: gray;")
        model_layout.insertWidget(1, self.lbl_pipeline_issues)

        relayout_controls = QHBoxLayout()
        relayout_controls.setSpacing(4)
        relayout_controls.addWidget(QLabel("Profile:"))
        self.cmb_layout_profile_model = QComboBox()
        self.cmb_layout_profile_model.addItems(["balanced", "compact", "dense", "spacious", "debug"])
        self._configure_compact_combo(self.cmb_layout_profile_model, 92, 7)
        relayout_controls.addWidget(self.cmb_layout_profile_model)
        relayout_controls.addWidget(QLabel("Org:"))
        self.cmb_layout_orientation_model = QComboBox()
        self.cmb_layout_orientation_model.addItems(["horizontal", "vertical", "axis"])
        self._configure_compact_combo(self.cmb_layout_orientation_model, 90, 6)
        relayout_controls.addWidget(self.cmb_layout_orientation_model)
        relayout_controls.addWidget(QLabel("Algo:"))
        self.cmb_layout_algorithm_model = QComboBox()
        self.cmb_layout_algorithm_model.addItems(layout_algorithms)
        self._configure_compact_combo(self.cmb_layout_algorithm_model, 130, 10)
        relayout_controls.addWidget(self.cmb_layout_algorithm_model)
        relayout_controls.addStretch()

        relayout_actions = QHBoxLayout()
        relayout_actions.setSpacing(4)
        self.btn_auto_wire_steps = QPushButton("Auto-wire")
        self.btn_auto_wire_steps.setMaximumWidth(100)
        self.btn_auto_wire_steps.setToolTip(
            "Autonomously wire missing algorithm parameters and output destinations."
        )
        self.btn_auto_wire_steps.clicked.connect(self._on_auto_wire_model)
        self.btn_auto_wire_steps.setEnabled(False)
        relayout_actions.addWidget(self.btn_auto_wire_steps)
        self.btn_relayout_json = QPushButton("Re-layout")
        self.btn_relayout_json.setMaximumWidth(90)
        self.btn_relayout_json.setToolTip("Apply the selected layout mode without regenerating the model.")
        self.btn_relayout_json.clicked.connect(self._on_relayout_json)
        self.btn_relayout_json.setEnabled(False)
        relayout_actions.addWidget(self.btn_relayout_json)
        relayout_actions.addStretch()
        model_layout.insertLayout(2, relayout_controls)
        model_layout.insertLayout(3, relayout_actions)

        self._inject_history_tab()
        self._bind_layout_control_sync()
        self._apply_saved_layout_settings()
        self._refresh_history_list()

    def _compiler_llm_config(self):
        key = self.cmb_backend.itemData(self.cmb_backend.currentIndex())
        return {
            "provider": key,
            "base_url": self.txt_url.text().strip(),
            "api_key": self.txt_api_key.text().strip(),
            "model": self.txt_model.text().strip(),
            "temperature": self.sld_temperature.value() / 10.0,
            "timeout": 240 if key == "ollama" else 180,
        }

    def _bind_layout_control_sync(self):
        self.cmb_layout_profile_generate.currentIndexChanged.connect(self._on_layout_control_changed)
        self.cmb_layout_orientation_generate.currentIndexChanged.connect(self._on_layout_control_changed)
        self.cmb_layout_algorithm_generate.currentIndexChanged.connect(self._on_layout_control_changed)
        self.cmb_layout_profile_model.currentIndexChanged.connect(self._on_layout_control_changed)
        self.cmb_layout_orientation_model.currentIndexChanged.connect(self._on_layout_control_changed)
        self.cmb_layout_algorithm_model.currentIndexChanged.connect(self._on_layout_control_changed)

    def _on_layout_control_changed(self):
        if self._layout_syncing:
            return

        sender = self.sender()
        profile = self._layout_control_value("profile", sender)
        orientation = self._layout_control_value("orientation", sender)
        algorithm = self._layout_control_value("algorithm", sender)

        self._layout_syncing = True
        try:
            self._set_combo_value(self.cmb_layout_profile_generate, profile)
            self._set_combo_value(self.cmb_layout_profile_model, profile)
            self._set_combo_value(self.cmb_layout_orientation_generate, orientation)
            self._set_combo_value(self.cmb_layout_orientation_model, orientation)
            self._set_combo_value(self.cmb_layout_algorithm_generate, algorithm)
            self._set_combo_value(self.cmb_layout_algorithm_model, algorithm)
        finally:
            self._layout_syncing = False
        self._save_linked_settings()

    def _layout_control_value(self, kind, sender):
        model_controls = (
            self.cmb_layout_profile_model,
            self.cmb_layout_orientation_model,
            self.cmb_layout_algorithm_model,
        )
        if kind == "profile":
            return self.cmb_layout_profile_model.currentText() if sender in model_controls else self.cmb_layout_profile_generate.currentText()
        if kind == "orientation":
            return self.cmb_layout_orientation_model.currentText() if sender in model_controls else self.cmb_layout_orientation_generate.currentText()
        return self.cmb_layout_algorithm_model.currentText() if sender in model_controls else self.cmb_layout_algorithm_generate.currentText()

    @staticmethod
    def _set_combo_value(combo, value):
        idx = combo.findText(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    @staticmethod
    def _configure_compact_combo(combo, max_width, min_chars):
        combo.setMaximumWidth(int(max_width))
        combo.setMinimumContentsLength(int(min_chars))
        combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)

    def _current_layout_options(self):
        return {
            "profile": self.cmb_layout_profile_generate.currentText(),
            "orientation": self.cmb_layout_orientation_generate.currentText(),
            "algorithm": self.cmb_layout_algorithm_generate.currentText(),
        }

    def _apply_layout(self, workflow):
        options = self._current_layout_options()
        return GraphLayoutService().layout_model_json(
            workflow,
            mode=options["profile"],
            orientation=options["orientation"],
            strategy=options["algorithm"],
        )

    def _load_linked_settings(self):
        s = QSettings()
        self._saved_layout_profile = s.value("ModelForge/Linked/layout_profile", "balanced")
        self._saved_layout_orientation = s.value("ModelForge/Linked/layout_orientation", "horizontal")
        self._saved_layout_algorithm = s.value("ModelForge/Linked/layout_algorithm", "sugiyama")
        history_raw = s.value("ModelForge/Linked/generation_history", "[]")
        try:
            self._history_entries = json.loads(history_raw) if history_raw else []
            if not isinstance(self._history_entries, list):
                self._history_entries = []
        except Exception:
            self._history_entries = []

    def _save_linked_settings(self):
        s = QSettings()
        s.setValue("ModelForge/Linked/layout_profile", self.cmb_layout_profile_generate.currentText())
        s.setValue("ModelForge/Linked/layout_orientation", self.cmb_layout_orientation_generate.currentText())
        s.setValue("ModelForge/Linked/layout_algorithm", self.cmb_layout_algorithm_generate.currentText())
        s.setValue("ModelForge/Linked/generation_history", json.dumps(self._history_entries, ensure_ascii=False))

    def _apply_saved_layout_settings(self):
        self._layout_syncing = True
        try:
            self._set_combo_value(self.cmb_layout_profile_generate, self._saved_layout_profile)
            self._set_combo_value(self.cmb_layout_profile_model, self._saved_layout_profile)
            self._set_combo_value(self.cmb_layout_orientation_generate, self._saved_layout_orientation)
            self._set_combo_value(self.cmb_layout_orientation_model, self._saved_layout_orientation)
            self._set_combo_value(self.cmb_layout_algorithm_generate, self._saved_layout_algorithm)
            self._set_combo_value(self.cmb_layout_algorithm_model, self._saved_layout_algorithm)
        finally:
            self._layout_syncing = False

    def _inject_history_tab(self):
        history_tab = QWidget()
        history_layout = QVBoxLayout()
        history_layout.setSpacing(6)
        history_layout.addWidget(QLabel("Past generation attempts"))
        self.lst_history = QListWidget()
        self.lst_history.setSelectionMode(QListWidget.SingleSelection)
        self.lst_history.setStyleSheet(
            "QListWidget::item { padding: 8px; margin: 4px; border: 1px solid #4a4a4a; border-radius: 6px; }"
            "QListWidget::item:selected { background: #2b3a55; border: 1px solid #6a8ac7; }"
        )
        history_layout.addWidget(self.lst_history, stretch=1)

        btn_row = QHBoxLayout()
        self.btn_history_load = QPushButton("Load selected")
        self.btn_history_load.clicked.connect(self._on_load_history_entry)
        btn_row.addWidget(self.btn_history_load)
        self.btn_history_delete = QPushButton("Delete selected")
        self.btn_history_delete.clicked.connect(self._on_delete_history_entry)
        btn_row.addWidget(self.btn_history_delete)
        self.btn_history_rename = QPushButton("Rename selected")
        self.btn_history_rename.clicked.connect(self._on_rename_history_entry)
        btn_row.addWidget(self.btn_history_rename)
        self.btn_history_clear = QPushButton("Clear history")
        self.btn_history_clear.clicked.connect(self._on_clear_history)
        btn_row.addWidget(self.btn_history_clear)
        btn_row.addStretch()
        history_layout.addLayout(btn_row)

        history_tab.setLayout(history_layout)
        self.tabs.insertTab(2, history_tab, "History")

    def _on_generate(self):
        description = self.txt_description.toPlainText().strip()
        if not description:
            QMessageBox.warning(self, "Model Forge Linked", "Please enter a workflow description.")
            return

        self._apply_settings()

        selected_layers = self._get_selected_layers()
        selected_layer_ids = [layer.id() for layer in selected_layers if layer is not None]
        algo_config = self._get_algo_config()
        self.current_context_text = self.context_collector.collect(selected_layers, algo_config)

        self.btn_generate.setEnabled(False)
        self.progress_bar.show()
        self.lbl_status.setText("Starting generation...")
        self.lbl_pipeline_issues.setText("")

        self.worker = LinkedGenerateWorker(
            description=description,
            model_name=self.txt_model_name.text(),
            model_group=self.txt_model_group.text(),
            llm_config=self._compiler_llm_config(),
            layout_profile=self.cmb_layout_profile_generate.currentText(),
            layout_orientation=self.cmb_layout_orientation_generate.currentText(),
            layout_algorithm=self.cmb_layout_algorithm_generate.currentText(),
            selected_layer_ids=selected_layer_ids,
            algo_config=algo_config,
            optimize_generation=self.chk_optimize.isChecked(),
        )
        self.worker.progress.connect(lambda msg: self.lbl_status.setText(msg))
        self.worker.finished.connect(self._on_generate_finished)
        self.worker.error.connect(self._on_generate_error)
        self.worker.start()

    def _on_generate_finished(self, workflow, issues):
        self.progress_bar.hide()
        self.btn_generate.setEnabled(True)
        self.current_plan_issues = issues or []
        self._update_issue_summary()

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

        workflow = self.builder.auto_wire_model_json(
            workflow,
            prefer_project_outputs=True,
        )
        self._current_model_json = workflow
        self.lbl_status.setText("Model generated! See Model tab.")

        self.txt_model_json.setPlainText(json.dumps(workflow, indent=2, ensure_ascii=False))
        self.lbl_validity.setText("\u2713 Valid JSON structure received.")
        self.lbl_validity.setStyleSheet("color: #4CAF50; font-weight: bold;")
        self.btn_rebuild.setEnabled(True)
        self.btn_improve.setEnabled(True)
        self.btn_auto_repair.setEnabled(True)
        self.btn_auto_wire_steps.setEnabled(True)
        self.btn_relayout_json.setEnabled(True)
        self.tabs.setCurrentIndex(1)

        self._record_history_entry(workflow)
        self._try_build_model(workflow)

    def _on_generate_error(self, error_msg):
        self.progress_bar.hide()
        self.btn_generate.setEnabled(True)
        self.btn_auto_wire_steps.setEnabled(bool(self._current_model_json))
        self.btn_relayout_json.setEnabled(bool(self._current_model_json))
        self.lbl_status.setText("Generation failed.")
        QMessageBox.critical(self, "Generation Error", error_msg)

    def _update_issue_summary(self):
        if not self.current_plan_issues:
            self.lbl_pipeline_issues.setText("Compiler issues: none.")
            self.lbl_pipeline_issues.setStyleSheet("color: #4CAF50;")
            return

        counts = {"error": 0, "warning": 0, "info": 0}
        lines = []
        for issue in self.current_plan_issues:
            level = issue.level.value if hasattr(issue.level, "value") else str(issue.level)
            counts[level] = counts.get(level, 0) + 1
            icon = "❌" if issue.level == IssueLevel.ERROR else ("⚠️" if issue.level == IssueLevel.WARNING else "ℹ️")
            lines.append(f"{icon} [{issue.code}] {issue.message}")

        summary = (
            f"Compiler issues — errors: {counts.get('error', 0)}, "
            f"warnings: {counts.get('warning', 0)}, "
            f"info: {counts.get('info', 0)}"
        )
        details = "\n".join(lines[:4])
        if len(lines) > 4:
            details += f"\n...and {len(lines) - 4} more."

        self.lbl_pipeline_issues.setText(f"{summary}\n{details}")
        self.lbl_pipeline_issues.setStyleSheet(
            "color: red;" if counts.get("error", 0) else "color: #b58900;"
        )

    def _ensure_model_from_editor(self):
        if self.current_model is not None:
            return self.current_model

        text = self.txt_model_json.toPlainText().strip()
        if not text:
            raise ValueError("No model JSON available.")

        workflow = json.loads(text)
        if "inputs" not in workflow or "algorithms" not in workflow:
            raise ValueError("JSON must contain 'inputs' and 'algorithms'.")

        workflow = self.builder.auto_wire_model_json(
            workflow,
            prefer_project_outputs=True,
        )
        self._current_model_json = workflow
        self.current_model = self.builder.load_model_json(workflow, open_designer=False)
        return self.current_model

    def _save_model(self):
        from qgis.PyQt.QtWidgets import QFileDialog

        try:
            model = self._ensure_model_from_editor()
        except json.JSONDecodeError as e:
            QMessageBox.warning(self, "JSON Error", "Invalid JSON:\n" + str(e))
            return
        except Exception as e:
            QMessageBox.warning(self, "Model Forge Linked", str(e))
            return

        path, _ = QFileDialog.getSaveFileName(self, "Save Model", "", "QGIS Models (*.model3)")
        if not path:
            return
        if not path.endswith(".model3"):
            path += ".model3"

        model.setSourceFilePath(path)
        if model.toFile(path):
            QMessageBox.information(self, "Model Forge Linked", "Model saved to:\n" + path)
        else:
            QMessageBox.warning(self, "Model Forge Linked", "Failed to save model.")

    def _on_open_designer(self):
        try:
            workflow = self._workflow_from_editor_for_designer()
            self._open_designer_dialog(workflow)
        except json.JSONDecodeError as e:
            QMessageBox.warning(self, "JSON Error", "Invalid JSON:\n" + str(e))
        except Exception as e:
            QMessageBox.critical(self, "Model Forge Linked", "Could not open Designer:\n" + str(e))

    def _workflow_from_editor_for_designer(self):
        text = self.txt_model_json.toPlainText().strip()
        if not text:
            raise ValueError("No model JSON available.")
        workflow = json.loads(text)
        if "inputs" not in workflow or "algorithms" not in workflow:
            raise ValueError("JSON must contain 'inputs' and 'algorithms'.")
        workflow = self.builder.auto_wire_model_json(
            workflow,
            prefer_project_outputs=True,
        )
        workflow = self._apply_layout(workflow)
        self._current_model_json = workflow
        self.txt_model_json.setPlainText(json.dumps(workflow, indent=2, ensure_ascii=False))
        return workflow

    def _open_designer_dialog(self, workflow):
        from processing.modeler.ModelerDialog import ModelerDialog

        model = self.builder.load_model_json(workflow, open_designer=False)
        self.current_model = model
        dlg = ModelerDialog.create(model)
        self._attach_designer_autolayout_button(dlg)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        self._designer_dlg = dlg

    def _attach_designer_autolayout_button(self, dlg):
        if getattr(dlg, "_mf_autolayout_btn", None) is not None:
            return
        btn = QPushButton("Auto-layout (Model Forge)", dlg)
        btn.setToolTip("Re-apply selected layout and reopen Model Designer.")
        btn.clicked.connect(self._on_designer_autolayout)
        inserted = False

        # Prefer native dialog button rows for clean placement and behavior.
        for box in dlg.findChildren(QDialogButtonBox):
            box.addButton(btn, QDialogButtonBox.ActionRole)
            inserted = True
            break

        # Fallback if there is no button box.
        if not inserted:
            root_layout = dlg.layout()
            if root_layout is not None and hasattr(root_layout, "addWidget"):
                row_widget = QWidget(dlg)
                row = QHBoxLayout(row_widget)
                row.setContentsMargins(0, 0, 0, 0)
                row.addStretch()
                row.addWidget(btn)
                root_layout.addWidget(row_widget)
                inserted = True

        if not inserted:
            btn.adjustSize()
            btn.move(12, 12)
            btn.raise_()
            btn.show()

        dlg._mf_autolayout_btn = btn

    def _on_designer_autolayout(self):
        try:
            workflow = self._workflow_from_editor_for_designer()
            old = self._designer_dlg
            self._open_designer_dialog(workflow)
            if old is not None and old is not self._designer_dlg:
                old.close()
            self.lbl_status.setText("Auto-layout applied in Model Designer.")
        except Exception as e:
            QMessageBox.warning(self, "Model Forge Linked", f"Could not auto-layout in designer:\n{e}")

    def _on_open_custom_step_dialog(self):
        try:
            dlg = CustomStepDialog(parent=self)
            if dlg.exec():
                py_path = getattr(dlg, "generated_py_path", None)
                if py_path and self.plugin is not None:
                    alg_id = self.plugin.register_generated_step(py_path)
                    self.lbl_status.setText(f"Registered custom step: {alg_id}")
        except Exception as e:
            QMessageBox.critical(self, "Model Forge Linked", f"Could not open Custom Step Author:\n{e}")

    def _on_relayout_json(self):
        text = self.txt_model_json.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Model Forge Linked", "No model JSON available to re-layout.")
            return
        try:
            workflow = json.loads(text)
        except json.JSONDecodeError as e:
            QMessageBox.warning(self, "JSON Error", "Invalid JSON:\n" + str(e))
            return

        if "inputs" not in workflow or "algorithms" not in workflow:
            QMessageBox.warning(
                self,
                "Model Forge Linked",
                "JSON must contain 'inputs' and 'algorithms' before layout can be applied.",
            )
            return

        self._on_layout_control_changed()
        workflow = self._apply_layout(workflow)
        workflow = self.builder.auto_wire_model_json(
            workflow,
            prefer_project_outputs=True,
        )
        self._current_model_json = workflow
        self.txt_model_json.setPlainText(json.dumps(workflow, indent=2, ensure_ascii=False))
        self.lbl_status.setText("Applied layout to current JSON.")
        self.lbl_validity.setText("\u2713 Layout updated without regeneration.")
        self.lbl_validity.setStyleSheet("color: #4CAF50; font-weight: bold;")
        self._try_build_model(workflow)

    def _on_auto_wire_model(self):
        text = self.txt_model_json.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Model Forge Linked", "No model JSON available to auto-wire.")
            return
        try:
            workflow = json.loads(text)
        except json.JSONDecodeError as e:
            QMessageBox.warning(self, "JSON Error", "Invalid JSON:\n" + str(e))
            return

        workflow = self.builder.auto_wire_model_json(
            workflow,
            prefer_project_outputs=True,
        )
        workflow = self._apply_layout(workflow)
        self._current_model_json = workflow
        self.txt_model_json.setPlainText(json.dumps(workflow, indent=2, ensure_ascii=False))
        self.lbl_status.setText("Auto-wired model steps and output destinations.")
        self.lbl_validity.setText("\u2713 Autonomous wiring applied.")
        self.lbl_validity.setStyleSheet("color: #4CAF50; font-weight: bold;")
        self._try_build_model(workflow)

    def _record_history_entry(self, workflow):
        entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "title": workflow.get("model_name", "workflow"),
            "model_name": workflow.get("model_name", "workflow"),
            "model_group": workflow.get("model_group", "ModelForge"),
            "algorithm_count": len(workflow.get("algorithms", [])),
            "input_count": len(workflow.get("inputs", [])),
            "layout_profile": self.cmb_layout_profile_generate.currentText(),
            "layout_orientation": self.cmb_layout_orientation_generate.currentText(),
            "layout_algorithm": self.cmb_layout_algorithm_generate.currentText(),
            "workflow": workflow,
        }
        self._history_entries.insert(0, entry)
        self._history_entries = self._history_entries[:30]
        self._save_linked_settings()
        self._refresh_history_list()

    def _refresh_history_list(self):
        if not hasattr(self, "lst_history"):
            return
        self.lst_history.clear()
        for idx, entry in enumerate(self._history_entries):
            title = entry.get("title") or entry.get("model_name", "workflow")
            text = (
                f"{title}\n"
                f"{entry.get('timestamp', '?')} | "
                f"{entry.get('model_name', 'workflow')} "
                f"({entry.get('algorithm_count', 0)} steps)\n"
                f"layout={entry.get('layout_profile', 'balanced')}/{entry.get('layout_orientation', 'horizontal')}/"
                f"{entry.get('layout_algorithm', 'sugiyama')}"
            )
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, idx)
            item.setSizeHint(QSize(0, 68))
            self.lst_history.addItem(item)

    def _selected_history_index(self):
        item = self.lst_history.currentItem() if hasattr(self, "lst_history") else None
        if item is None:
            return None
        idx = item.data(Qt.UserRole)
        if idx is None or idx < 0 or idx >= len(self._history_entries):
            return None
        return idx

    def _on_load_history_entry(self):
        idx = self._selected_history_index()
        if idx is None:
            QMessageBox.information(self, "History", "Select a history item first.")
            return
        entry = self._history_entries[idx]
        workflow = entry.get("workflow", {})
        if not isinstance(workflow, dict):
            QMessageBox.warning(self, "History", "Selected history entry is invalid.")
            return

        self._set_combo_value(self.cmb_layout_profile_generate, entry.get("layout_profile", "balanced"))
        self._set_combo_value(self.cmb_layout_orientation_generate, entry.get("layout_orientation", "horizontal"))
        self._set_combo_value(self.cmb_layout_algorithm_generate, entry.get("layout_algorithm", "sugiyama"))
        self._on_layout_control_changed()

        self._current_model_json = workflow
        self.txt_model_json.setPlainText(json.dumps(workflow, indent=2, ensure_ascii=False))
        self.btn_rebuild.setEnabled(True)
        self.btn_auto_wire_steps.setEnabled(True)
        self.btn_relayout_json.setEnabled(True)
        self.tabs.setCurrentIndex(1)
        self._try_build_model(workflow)
        self.lbl_status.setText("Loaded model from history.")

    def _on_delete_history_entry(self):
        idx = self._selected_history_index()
        if idx is None:
            QMessageBox.information(self, "History", "Select a history item first.")
            return
        del self._history_entries[idx]
        self._save_linked_settings()
        self._refresh_history_list()

    def _on_rename_history_entry(self):
        idx = self._selected_history_index()
        if idx is None:
            QMessageBox.information(self, "History", "Select a history item first.")
            return
        current = self._history_entries[idx]
        old_title = current.get("title") or current.get("model_name", "workflow")
        new_title, ok = QInputDialog.getText(self, "Rename History Item", "New name:", text=old_title)
        if not ok:
            return
        new_title = (new_title or "").strip()
        if not new_title:
            QMessageBox.information(self, "History", "Name cannot be empty.")
            return
        current["title"] = new_title
        self._save_linked_settings()
        self._refresh_history_list()

    def _on_clear_history(self):
        if not self._history_entries:
            return
        self._history_entries = []
        self._save_linked_settings()
        self._refresh_history_list()

