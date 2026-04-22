import json
import traceback

from qgis.PyQt.QtCore import QThread, pyqtSignal
from qgis.PyQt.QtWidgets import QHBoxLayout, QLabel, QComboBox, QPushButton, QMessageBox

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
        layout_mode,
        selected_layer_ids,
        algo_config,
    ):
        super().__init__()
        self.description = description
        self.model_name = model_name
        self.model_group = model_group
        self.llm_config = llm_config
        self.layout_mode = layout_mode
        self.selected_layer_ids = set(selected_layer_ids or [])
        self.algo_config = algo_config or {}

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

            plan, model_json = pipeline.run(
                raw_text=self.description,
                model_name=self.model_name,
                model_group=self.model_group,
                qgis_context=ctx,
                mcp_client=client,
                progress_callback=lambda msg: self.progress.emit(msg),
            )

            model_json = GraphLayoutService().layout_model_json(model_json, mode=self.layout_mode)
            self.finished.emit(model_json, plan.issues)
        except Exception as e:
            self.error.emit(str(e) + "\n\n" + traceback.format_exc())

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


class ForgeWidget(LegacyForgeWidget):
    def __init__(self, iface, plugin=None, parent=None):
        super().__init__(iface, parent)
        self.plugin = plugin
        self.builder = ModelBuilderBridge()
        self.current_plan_issues = []
        self._inject_linked_controls()

    def _inject_linked_controls(self):
        self.chk_two_phase.setText("MCP compiler pipeline (enabled)")
        self.chk_two_phase.setChecked(True)
        self.chk_two_phase.setEnabled(False)
        self.chk_two_phase.setToolTip(
            "Uses the MCP compiler pipeline stages (parse, plan, resolve, validate, emit)."
        )

        compiler_row = QHBoxLayout()
        compiler_row.addWidget(QLabel("Layout mode:"))
        self.cmb_layout_mode = QComboBox()
        self.cmb_layout_mode.addItems(["balanced", "compact", "dense", "spacious", "debug"])
        compiler_row.addWidget(self.cmb_layout_mode)
        compiler_row.addStretch()
        self.btn_custom_step = QPushButton("Custom Step Author...")
        self.btn_custom_step.clicked.connect(self._on_open_custom_step_dialog)
        compiler_row.addWidget(self.btn_custom_step)

        generate_layout = self.tabs.widget(0).layout()
        progress_idx = generate_layout.indexOf(self.progress_bar)
        if progress_idx >= 0:
            generate_layout.insertLayout(progress_idx, compiler_row)
        else:
            generate_layout.addLayout(compiler_row)

        model_layout = self.tabs.widget(1).layout()
        self.lbl_pipeline_issues = QLabel("")
        self.lbl_pipeline_issues.setWordWrap(True)
        self.lbl_pipeline_issues.setStyleSheet("color: gray;")
        model_layout.insertWidget(1, self.lbl_pipeline_issues)

    def _compiler_llm_config(self):
        key = self.cmb_backend.itemData(self.cmb_backend.currentIndex())
        return {
            "provider": key,
            "base_url": self.txt_url.text().strip(),
            "api_key": self.txt_api_key.text().strip(),
            "model": self.txt_model.text().strip(),
            "temperature": self.sld_temperature.value() / 10.0,
        }

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
            layout_mode=self.cmb_layout_mode.currentText(),
            selected_layer_ids=selected_layer_ids,
            algo_config=algo_config,
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
        QMessageBox.critical(self, "Generation Error", "Error generating model:\n" + error_msg)

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
            model = self._ensure_model_from_editor()
            self.current_model = model
            self.builder.load_model_json(self._current_model_json, open_designer=True)
        except json.JSONDecodeError as e:
            QMessageBox.warning(self, "JSON Error", "Invalid JSON:\n" + str(e))
        except Exception as e:
            QMessageBox.critical(self, "Model Forge Linked", "Could not open Designer:\n" + str(e))

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
