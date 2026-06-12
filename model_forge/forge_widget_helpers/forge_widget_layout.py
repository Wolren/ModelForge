import json

from qgis.PyQt.QtCore import QSettings
from qgis.PyQt.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QLabel, QMessageBox, QPushButton

from model_forge.compiler_core.core.services.layout.graph_layout import GraphLayoutService


class ForgeWidgetLayoutMixin:
    """Mixin for layout-related functionality in ForgeWidget."""

    def _inject_compiler_controls(self):
        self.chk_two_phase.setText("MCP compiler pipeline (enabled)")
        self.chk_two_phase.setChecked(True)
        self.chk_two_phase.setToolTip(
            "Uses the MCP compiler pipeline stages (parse, plan, resolve, validate, emit)."
        )

        layout_algorithms = [
            "sugiyama",
            "topological",
            "axis_pack",
            "radial_shell",
            "ancestor_weighted",
        ]
        compiler_controls = QHBoxLayout()
        compiler_controls.setSpacing(4)
        compiler_controls.addWidget(QLabel("Profile:"))
        self.cmb_layout_profile_generate = QComboBox()
        self.cmb_layout_profile_generate.addItems(
            ["balanced", "compact", "dense", "spacious", "debug"]
        )
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
        self.cmb_layout_profile_model.addItems(
            ["balanced", "compact", "dense", "spacious", "debug"]
        )
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
        self.btn_relayout_json.setToolTip(
            "Apply the selected layout mode without regenerating the model."
        )
        self.btn_relayout_json.clicked.connect(self._on_relayout_json)
        self.btn_relayout_json.setEnabled(False)
        relayout_actions.addWidget(self.btn_relayout_json)
        relayout_actions.addStretch()
        model_layout.insertLayout(2, relayout_controls)
        model_layout.insertLayout(3, relayout_actions)

        self._bind_layout_control_sync()
        self._apply_saved_layout_settings()

    def _load_layout_settings(self):
        s = QSettings()
        self._saved_layout_profile = s.value(
            "ModelForge/layout_profile",
            s.value("ModelForge/Linked/layout_profile", "balanced"),
        )
        self._saved_layout_orientation = s.value(
            "ModelForge/layout_orientation",
            s.value("ModelForge/Linked/layout_orientation", "horizontal"),
        )
        self._saved_layout_algorithm = s.value(
            "ModelForge/layout_algorithm",
            s.value("ModelForge/Linked/layout_algorithm", "sugiyama"),
        )

    def _save_layout_settings(self):
        s = QSettings()
        s.setValue("ModelForge/layout_profile", self.cmb_layout_profile_generate.currentText())
        s.setValue(
            "ModelForge/layout_orientation", self.cmb_layout_orientation_generate.currentText()
        )
        s.setValue("ModelForge/layout_algorithm", self.cmb_layout_algorithm_generate.currentText())

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
        self.cmb_layout_profile_generate.currentIndexChanged.connect(
            self._on_layout_control_changed
        )
        self.cmb_layout_orientation_generate.currentIndexChanged.connect(
            self._on_layout_control_changed
        )
        self.cmb_layout_algorithm_generate.currentIndexChanged.connect(
            self._on_layout_control_changed
        )
        self.cmb_layout_profile_model.currentIndexChanged.connect(self._on_layout_control_changed)
        self.cmb_layout_orientation_model.currentIndexChanged.connect(
            self._on_layout_control_changed
        )
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
        self._save_layout_settings()

    def _layout_control_value(self, kind, sender):
        model_controls = (
            self.cmb_layout_profile_model,
            self.cmb_layout_orientation_model,
            self.cmb_layout_algorithm_model,
        )
        if kind == "profile":
            return (
                self.cmb_layout_profile_model.currentText()
                if sender in model_controls
                else self.cmb_layout_profile_generate.currentText()
            )
        if kind == "orientation":
            return (
                self.cmb_layout_orientation_model.currentText()
                if sender in model_controls
                else self.cmb_layout_orientation_generate.currentText()
            )
        return (
            self.cmb_layout_algorithm_model.currentText()
            if sender in model_controls
            else self.cmb_layout_algorithm_generate.currentText()
        )

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

    def _apply_saved_layout_settings(self):
        self._layout_syncing = True
        try:
            self._set_combo_value(self.cmb_layout_profile_generate, self._saved_layout_profile)
            self._set_combo_value(self.cmb_layout_profile_model, self._saved_layout_profile)
            self._set_combo_value(
                self.cmb_layout_orientation_generate, self._saved_layout_orientation
            )
            self._set_combo_value(self.cmb_layout_orientation_model, self._saved_layout_orientation)
            self._set_combo_value(self.cmb_layout_algorithm_generate, self._saved_layout_algorithm)
            self._set_combo_value(self.cmb_layout_algorithm_model, self._saved_layout_algorithm)
        finally:
            self._layout_syncing = False

    def _on_relayout_json(self):
        text = self.txt_model_json.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Model Forge", "No model JSON available to re-layout.")
            return
        try:
            workflow = json.loads(text)
        except json.JSONDecodeError as e:
            QMessageBox.warning(self, "JSON Error", "Invalid JSON:\n" + str(e))
            return

        if "inputs" not in workflow or "algorithms" not in workflow:
            QMessageBox.warning(
                self,
                "Model Forge",
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
            QMessageBox.warning(self, "Model Forge", "No model JSON available to auto-wire.")
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
