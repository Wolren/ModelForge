"""
Model Forge - Main Widget

⚠️  EXPERIMENTAL PROJECT
This plugin is experimental. Features may change, break, or be removed without notice.
Links and documentation may become outdated or broken.
Use at your own risk.
"""

import json

from qgis.PyQt.QtWidgets import QMessageBox

from model_forge.forge_widget_helpers.forge_widget_base import (
    ForgeWidget as LegacyForgeWidget,
)
from model_forge.forge_widget_helpers.forge_widget_history import (
    ForgeWidgetHistoryMixin,
)
from model_forge.forge_widget_helpers.forge_widget_layout import ForgeWidgetLayoutMixin

from .compiler_core.core.ir import IssueLevel
from .compiler_core.ui.custom_step_dialog import CustomStepDialog
from .compiler_core.ui.model_builder_bridge import ModelBuilderBridge
from .forge_generate_worker import ForgeGenerateWorker


class ForgeWidget(ForgeWidgetLayoutMixin, ForgeWidgetHistoryMixin, LegacyForgeWidget):
    def __init__(self, iface, plugin=None, parent=None):
        super().__init__(iface, parent)
        self.plugin = plugin
        self.builder = ModelBuilderBridge()
        self.current_plan_issues = []
        self._history_entries = []
        self._layout_syncing = False
        self._load_compiler_settings()
        self._inject_compiler_controls()
        self._inject_history_tab()
        self._refresh_history_list()
        self.btn_cancel.clicked.connect(self._on_cancel_generate)

    def _load_compiler_settings(self):
        self._load_layout_settings()
        self._load_history_settings()

    def _save_compiler_settings(self):
        self._save_layout_settings()
        self._save_history_settings()

    def _on_generate(self):
        self._stop_worker("worker")
        description = self.txt_description.toPlainText().strip()
        if not description:
            QMessageBox.warning(self, "Model Forge", "Please enter a workflow description.")
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

        self.worker = ForgeGenerateWorker(
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
        self.worker.cancelled.connect(self._on_generate_cancelled)
        # Swap buttons
        self.btn_generate.setVisible(False)
        self.btn_cancel.setEnabled(True)
        self.btn_cancel.setVisible(True)
        self.worker.start()

    def _on_cancel_generate(self):
        if self.worker and self.worker.isRunning():
            self.worker.request_cancel()
            self.lbl_status.setText("Cancelling...")
            self.btn_cancel.setEnabled(False)

    def _on_generate_cancelled(self):
        self.progress_bar.hide()
        self.btn_generate.setVisible(True)
        self.btn_generate.setEnabled(True)
        self.btn_cancel.setVisible(False)
        self.lbl_status.setText("Generation cancelled.")
        self._current_model_json = None

    def _on_generate_finished(self, workflow, issues):
        self.progress_bar.hide()
        self.btn_generate.setVisible(True)
        self.btn_generate.setEnabled(True)
        self.btn_cancel.setVisible(False)
        self.current_plan_issues = issues or []
        self._update_issue_summary()

        if not workflow or "inputs" not in workflow or "algorithms" not in workflow:
            self.lbl_status.setText("LLM response missing required keys.")
            raw_text = json.dumps(workflow, indent=2, ensure_ascii=False)
            self.txt_model_json.setPlainText(raw_text)
            self.lbl_validity.setText(
                "\u26a0 Missing 'inputs' or 'algorithms'. Keys: "
                + str(list(workflow.keys() if workflow else []))
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
        self._refresh_mermaid()

    def _on_generate_error(self, error_msg):
        self.progress_bar.hide()
        self.btn_generate.setVisible(True)
        self.btn_generate.setEnabled(True)
        self.btn_cancel.setVisible(False)
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
            icon = (
                "❌"
                if issue.level == IssueLevel.ERROR
                else ("⚠️" if issue.level == IssueLevel.WARNING else "ℹ️")
            )
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
            QMessageBox.warning(self, "Model Forge", str(e))
            return

        path, _ = QFileDialog.getSaveFileName(self, "Save Model", "", "QGIS Models (*.model3)")
        if not path:
            return
        if not path.endswith(".model3"):
            path += ".model3"

        model.setSourceFilePath(path)
        if model.toFile(path):
            QMessageBox.information(self, "Model Forge", "Model saved to:\n" + path)
        else:
            QMessageBox.warning(self, "Model Forge", "Failed to save model.")

    def _on_open_designer(self):
        try:
            workflow = self._workflow_from_editor_for_designer()
            self._open_designer_dialog(workflow)
        except json.JSONDecodeError as e:
            QMessageBox.warning(self, "JSON Error", "Invalid JSON:\n" + str(e))
        except Exception as e:
            QMessageBox.critical(self, "Model Forge", "Could not open Designer:\n" + str(e))

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
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        self._designer_dlg = dlg

    def _on_open_custom_step_dialog(self):
        try:
            dlg = CustomStepDialog(parent=self)
            if dlg.exec():
                py_path = getattr(dlg, "generated_py_path", None)
                if py_path and self.plugin is not None:
                    alg_id = self.plugin.register_generated_step(py_path)
                    self.lbl_status.setText(f"Registered custom step: {alg_id}")
        except Exception as e:
            QMessageBox.critical(self, "Model Forge", f"Could not open Custom Step Author:\n{e}")
