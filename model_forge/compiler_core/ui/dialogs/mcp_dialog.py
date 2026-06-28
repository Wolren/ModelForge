"""
MCPDialog
=========
Main Qt6 dialog for the ModelForge MCP assistant.
Uses native QGIS/Qt widgets throughout - no extra UI framework needed.
"""

from __future__ import annotations

try:
    from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal  # noqa: F401
    from qgis.PyQt.QtGui import QFont
    from qgis.PyQt.QtWidgets import (
        QCheckBox,  # noqa: F401
        QComboBox,
        QDialog,
        QDialogButtonBox,  # noqa: F401
        QDoubleSpinBox,  # noqa: F401
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMessageBox,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QSpinBox,  # noqa: F401
        QSplitter,  # noqa: F401
        QTabWidget,
        QTextBrowser,
        QVBoxLayout,
        QWidget,
    )

    _HAS_QGIS = True
except ImportError:
    _HAS_QGIS = False

if _HAS_QGIS:
    import json

    class CompilerWorker(QThread):
        """Runs the compiler pipeline in a background thread."""

        progress = pyqtSignal(str)
        finished = pyqtSignal(dict, list)  # model_json, issues
        error = pyqtSignal(str)

        def __init__(
            self, description, model_name, model_group, llm_config, layout_mode, parent=None
        ):
            super().__init__(parent)
            self.description = description
            self.model_name = model_name
            self.model_group = model_group
            self.llm_config = llm_config
            self.layout_mode = layout_mode
            self._cancelled = False

        def run(self):
            if self._cancelled:
                return
            try:
                from ...core.compiler.algorithm_resolver import AlgorithmResolver
                from ...core.compiler.expression_validator import ExpressionValidator
                from ...core.compiler.intent_parser import IntentParser
                from ...core.compiler.ir_validator import IRValidator
                from ...core.compiler.link_repair import LinkRepairService
                from ...core.compiler.model_emitter import ModelEmitter
                from ...core.compiler.pipeline import CompilerPipeline
                from ...core.compiler.semantic_planner import SemanticPlanner
                from ...core.context_collector import ContextCollector
                from ...core.llm.factory import create_backend
                from ...core.mcp.client import DirectMCPClient
                from ...core.mcp.tool_registry import build_server
                from ...core.services.layout.graph_layout import GraphLayoutService

                self.progress.emit("Connecting to LLM backend...")
                llm = create_backend(self.llm_config)
                ctx = ContextCollector().collect()
                server = build_server(llm)
                client = DirectMCPClient(server)

                pipeline = CompilerPipeline(
                    intent_parser=IntentParser(),
                    semantic_planner=SemanticPlanner(),
                    algorithm_resolver=AlgorithmResolver(),
                    expression_validator=ExpressionValidator(),
                    ir_validator=IRValidator(),
                    model_emitter=ModelEmitter(),
                    link_repair=LinkRepairService(),
                )

                if self._cancelled:
                    return
                plan, model_json = pipeline.run(
                    raw_text=self.description,
                    model_name=self.model_name,
                    model_group=self.model_group,
                    qgis_context=ctx,
                    mcp_client=client,
                    progress_callback=lambda msg: self.progress.emit(msg),
                )
                if self._cancelled:
                    return
                layout_svc = GraphLayoutService()
                model_json = layout_svc.layout_model_json(model_json, mode=self.layout_mode)

                self.finished.emit(model_json, plan.issues)

            except Exception as e:
                from ...core.llm.base import LLMBackendError, LLMTimeoutError

                if isinstance(e, LLMTimeoutError):
                    self.error.emit(
                        "The LLM request timed out. Try a smaller context or increase backend timeout."
                    )
                elif isinstance(e, LLMBackendError):
                    self.error.emit(str(e))
                else:
                    self.error.emit(str(e) or "Compiler execution failed.")

    class MCPDialog(QDialog):
        """Main dialog for the ModelForge MCP workflow builder."""

        def __init__(self, iface, parent=None):
            super().__init__(parent)
            self.iface = iface
            self._worker = None
            self._result = None
            self._setup_ui()

        def _setup_ui(self):
            self.setWindowTitle("ModelForge — MCP Workflow Builder")
            self.setMinimumWidth(760)
            self.setMinimumHeight(600)

            root = QVBoxLayout(self)
            root.setSpacing(8)
            root.setContentsMargins(12, 12, 12, 12)

            # ── Tabs ──────────────────────────────────────────────────────
            self._tabs = QTabWidget()
            root.addWidget(self._tabs)

            # Tab 1: Build
            build_tab = QWidget()
            self._tabs.addTab(build_tab, "Build Workflow")
            self._build_build_tab(build_tab)

            # Tab 2: LLM Config
            config_tab = QWidget()
            self._tabs.addTab(config_tab, "LLM Config")
            self._build_config_tab(config_tab)

            # Tab 3: Result
            result_tab = QWidget()
            self._tabs.addTab(result_tab, "Result")
            self._build_result_tab(result_tab)

            # ── Progress bar ──────────────────────────────────────────────
            self._progress = QProgressBar()
            self._progress.setRange(0, 0)  # indeterminate
            self._progress.setVisible(False)
            root.addWidget(self._progress)

            self._status_label = QLabel("Ready.")
            root.addWidget(self._status_label)

            # ── Buttons ───────────────────────────────────────────────────
            btn_row = QHBoxLayout()
            self._build_btn = QPushButton("Build →")
            self._build_btn.setDefault(True)
            self._cancel_btn = QPushButton("Cancel")
            self._cancel_btn.setEnabled(False)
            self._import_btn = QPushButton("Import into Model Builder")
            self._import_btn.setEnabled(False)
            btn_row.addWidget(self._build_btn)
            btn_row.addWidget(self._cancel_btn)
            btn_row.addStretch()
            btn_row.addWidget(self._import_btn)
            root.addLayout(btn_row)

            # ── Connections ───────────────────────────────────────────────
            self._build_btn.clicked.connect(self._on_build)
            self._cancel_btn.clicked.connect(self._on_cancel)
            self._import_btn.clicked.connect(self._on_import)

        def _build_build_tab(self, tab):
            layout = QVBoxLayout(tab)
            layout.setSpacing(8)

            desc_group = QGroupBox("Workflow Description")
            dg = QVBoxLayout(desc_group)
            hint = QLabel(
                "Describe your geoprocessing goal in plain language. "
                "Be specific about layer names, CRS, output types."
            )
            hint.setWordWrap(True)
            dg.addWidget(hint)
            self._desc_edit = QPlainTextEdit()
            self._desc_edit.setPlaceholderText(
                "e.g. Clip all buildings within 500 m of flood zones "
                "and dissolve by building type. Use EPSG:2180."
            )
            self._desc_edit.setMinimumHeight(120)
            dg.addWidget(self._desc_edit)
            layout.addWidget(desc_group)

            meta_group = QGroupBox("Model Metadata")
            mg = QHBoxLayout(meta_group)
            mg.addWidget(QLabel("Name:"))
            self._name_edit = QLineEdit("my_workflow")
            mg.addWidget(self._name_edit, 2)
            mg.addWidget(QLabel("Group:"))
            self._group_edit = QLineEdit("ModelForge")
            mg.addWidget(self._group_edit, 2)
            mg.addWidget(QLabel("Layout:"))
            self._layout_combo = QComboBox()
            self._layout_combo.addItems(["compact", "balanced", "dense", "spacious", "debug"])
            self._layout_combo.setCurrentIndex(1)
            mg.addWidget(self._layout_combo)
            layout.addWidget(meta_group)

        def _build_config_tab(self, tab):
            layout = QVBoxLayout(tab)
            layout.setSpacing(8)

            llm_group = QGroupBox("LLM Backend")
            lg = QVBoxLayout(llm_group)

            row1 = QHBoxLayout()
            row1.addWidget(QLabel("Provider:"))
            self._provider_combo = QComboBox()
            self._provider_combo.addItems(["Ollama (local)", "OpenAI"])
            row1.addWidget(self._provider_combo, 1)
            row1.addWidget(QLabel("Model:"))
            self._llm_model_edit = QLineEdit("llama3")
            row1.addWidget(self._llm_model_edit, 2)
            lg.addLayout(row1)

            row2 = QHBoxLayout()
            row2.addWidget(QLabel("Base URL:"))
            self._base_url_edit = QLineEdit("http://localhost:11434")
            row2.addWidget(self._base_url_edit, 3)
            lg.addLayout(row2)

            row3 = QHBoxLayout()
            row3.addWidget(QLabel("API Key (OpenAI):"))
            self._api_key_edit = QLineEdit()
            self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
            row3.addWidget(self._api_key_edit, 3)
            lg.addLayout(row3)

            layout.addWidget(llm_group)
            layout.addStretch()

            self._provider_combo.currentIndexChanged.connect(self._on_provider_changed)

        def _on_provider_changed(self, idx):
            if idx == 0:  # Ollama
                self._base_url_edit.setText("http://localhost:11434")
                self._api_key_edit.setEnabled(False)
            else:
                self._base_url_edit.setText("https://api.openai.com")
                self._api_key_edit.setEnabled(True)

        def _build_result_tab(self, tab):
            layout = QVBoxLayout(tab)

            self._issues_browser = QTextBrowser()
            self._issues_browser.setMaximumHeight(120)
            self._issues_browser.setPlaceholderText("Issues will appear here after building.")
            layout.addWidget(QLabel("Issues:"))
            layout.addWidget(self._issues_browser)

            self._json_browser = QPlainTextEdit()
            font = QFont("Monospace")
            font.setPointSize(9)
            self._json_browser.setFont(font)
            self._json_browser.setReadOnly(True)
            self._json_browser.setPlaceholderText("Generated model JSON will appear here.")
            layout.addWidget(QLabel("Generated JSON:"))
            layout.addWidget(self._json_browser)

        def _collect_llm_config(self) -> dict:
            provider_map = {0: "ollama", 1: "openai"}
            idx = self._provider_combo.currentIndex()
            return {
                "provider": provider_map.get(idx, "ollama"),
                "model": self._llm_model_edit.text().strip(),
                "base_url": self._base_url_edit.text().strip(),
                "api_key": self._api_key_edit.text().strip(),
                "temperature": 0.1,
            }

        def _on_build(self):
            desc = self._desc_edit.toPlainText().strip()
            if not desc:
                QMessageBox.warning(self, "Input required", "Please enter a workflow description.")
                return

            self._build_btn.setEnabled(False)
            self._cancel_btn.setEnabled(True)
            self._import_btn.setEnabled(False)
            self._progress.setVisible(True)
            self._status_label.setText("Building workflow…")
            self._issues_browser.clear()
            self._json_browser.clear()

            self._worker = CompilerWorker(
                description=desc,
                model_name=self._name_edit.text().strip() or "my_workflow",
                model_group=self._group_edit.text().strip() or "ModelForge",
                llm_config=self._collect_llm_config(),
                layout_mode=self._layout_combo.currentText(),
                parent=self,
            )
            self._worker.progress.connect(self._on_progress)
            self._worker.finished.connect(self._on_finished)
            self._worker.error.connect(self._on_error)
            self._worker.start()

        def _on_cancel(self):
            if self._worker and self._worker.isRunning():
                self._worker._cancelled = True
                self._worker.wait()
            self._reset_ui("Cancelled.")

        def _on_progress(self, msg: str):
            self._status_label.setText(msg)

        def _on_finished(self, model_json: dict, issues: list):
            self._result = model_json
            self._reset_ui(f"Done. {len(model_json.get('algorithms', []))} step(s) generated.")
            self._import_btn.setEnabled(True)

            # Display issues
            from ...core.ir import IssueLevel

            issue_lines = []
            for issue in issues:
                icon = (
                    "❌"
                    if issue.level == IssueLevel.ERROR
                    else ("⚠️" if issue.level == IssueLevel.WARNING else "ℹ️")
                )
                issue_lines.append(f"{icon} [{issue.code}] {issue.message}")
            self._issues_browser.setPlainText("\n".join(issue_lines) or "No issues.")

            self._json_browser.setPlainText(json.dumps(model_json, indent=2, ensure_ascii=False))
            self._tabs.setCurrentIndex(2)

        def _on_error(self, msg: str):
            self._reset_ui("Error.")
            QMessageBox.critical(self, "Compiler error", msg[:2000])

        def _reset_ui(self, status: str = ""):
            self._build_btn.setEnabled(True)
            self._cancel_btn.setEnabled(False)
            self._progress.setVisible(False)
            self._status_label.setText(status)

        def _on_import(self):
            if not self._result:
                return
            try:
                from ..model_builder_bridge import ModelBuilderBridge

                bridge = ModelBuilderBridge(self.iface)
                bridge.load_model_json(self._result)
                self.accept()
            except Exception as e:
                QMessageBox.critical(self, "Import error", str(e))

else:

    class MCPDialog:
        pass
