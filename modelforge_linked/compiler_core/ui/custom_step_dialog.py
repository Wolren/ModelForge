"""
CustomStepDialog
================
Native Qt6 dialog for authoring, editing, and generating custom
QgsProcessingAlgorithm steps.
"""
from __future__ import annotations

try:
    from qgis.PyQt.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QGroupBox,
        QLabel, QLineEdit, QPlainTextEdit, QComboBox,
        QPushButton, QListWidget, QListWidgetItem,
        QMessageBox, QSplitter, QWidget, QCheckBox, QInputDialog,
    )
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QFont
    _HAS_QGIS = True
except ImportError:
    _HAS_QGIS = False

if _HAS_QGIS:
    from ..core.services.custom_step_author import (
        CustomStepAuthorService, CustomStepSpec, ParamDef, OutputDef,
    )

    class CustomStepDialog(QDialog):

        def __init__(self, spec: CustomStepSpec | None = None, parent=None):
            super().__init__(parent)
            self._svc  = CustomStepAuthorService()
            self._spec = spec
            self._params: list[ParamDef] = []
            self._outputs: list[OutputDef] = []
            self.generated_py_path: str | None = None
            self.generated_step_id: str | None = None
            self.setWindowTitle("Custom Step Author — ModelForge")
            self.setMinimumWidth(720)
            self.setMinimumHeight(580)
            self._setup_ui()
            if spec:
                self._populate(spec)

        def _setup_ui(self):
            root = QVBoxLayout(self)
            root.setSpacing(8)
            root.setContentsMargins(12, 12, 12, 12)

            splitter = QSplitter(Qt.Orientation.Horizontal)

            # ── Left: schema ────────────────────────────────────────────
            left = QWidget()
            lv   = QVBoxLayout(left)

            meta = QGroupBox("Algorithm metadata")
            mg   = QVBoxLayout(meta)
            r1 = QHBoxLayout()
            r1.addWidget(QLabel("Step ID:"))
            self._id_edit = QLineEdit()
            self._id_edit.setPlaceholderText("snake_case_id")
            r1.addWidget(self._id_edit, 2)
            mg.addLayout(r1)

            r2 = QHBoxLayout()
            r2.addWidget(QLabel("Display name:"))
            self._name_edit = QLineEdit()
            r2.addWidget(self._name_edit, 3)
            mg.addLayout(r2)

            r3 = QHBoxLayout()
            r3.addWidget(QLabel("Group:"))
            self._group_edit = QLineEdit("User Steps")
            r3.addWidget(self._group_edit, 2)
            mg.addLayout(r3)

            r4 = QHBoxLayout()
            r4.addWidget(QLabel("Help text:"))
            self._help_edit = QLineEdit()
            r4.addWidget(self._help_edit, 3)
            mg.addLayout(r4)

            lv.addWidget(meta)

            param_group = QGroupBox("Parameters")
            pg = QVBoxLayout(param_group)
            self._param_list = QListWidget()
            pg.addWidget(self._param_list)
            pb = QHBoxLayout()
            self._add_param_btn = QPushButton("Add param")
            self._rem_param_btn = QPushButton("Remove")
            pb.addWidget(self._add_param_btn)
            pb.addWidget(self._rem_param_btn)
            pg.addLayout(pb)
            lv.addWidget(param_group)

            out_group = QGroupBox("Outputs")
            og = QVBoxLayout(out_group)
            self._output_list = QListWidget()
            og.addWidget(self._output_list)
            ob = QHBoxLayout()
            self._add_out_btn = QPushButton("Add output")
            self._rem_out_btn = QPushButton("Remove")
            ob.addWidget(self._add_out_btn)
            ob.addWidget(self._rem_out_btn)
            og.addLayout(ob)
            lv.addWidget(out_group)

            splitter.addWidget(left)

            # ── Right: code editor ───────────────────────────────────────
            right  = QWidget()
            rv     = QVBoxLayout(right)
            rv.addWidget(QLabel("Code body (processAlgorithm inner logic):"))
            self._code_edit = QPlainTextEdit()
            mono = QFont("Monospace")
            mono.setPointSize(10)
            self._code_edit.setFont(mono)
            self._code_edit.setPlaceholderText(
                "# Param variables are pre-bound as UPPERCASE versions of parameter names\n"
                "# e.g. if param name is 'input_layer', use INPUT_LAYER\n"
                "# Raise QgsProcessingException on errors.\n"
                "# Return dict of output values.\n"
                "result = {}\n"
                "return result"
            )
            rv.addWidget(self._code_edit)

            self._issues_label = QLabel("")
            self._issues_label.setWordWrap(True)
            self._issues_label.setStyleSheet("color: red;")
            rv.addWidget(self._issues_label)

            splitter.addWidget(right)
            splitter.setSizes([320, 380])

            root.addWidget(splitter, 1)

            # ── Bottom buttons ───────────────────────────────────────────
            btn_row = QHBoxLayout()
            self._validate_btn = QPushButton("Validate code")
            self._save_btn     = QPushButton("Save & Generate")
            self._save_btn.setDefault(True)
            cancel_btn = QPushButton("Cancel")
            btn_row.addWidget(self._validate_btn)
            btn_row.addStretch()
            btn_row.addWidget(cancel_btn)
            btn_row.addWidget(self._save_btn)
            root.addLayout(btn_row)

            # ── Connections ───────────────────────────────────────────────
            self._validate_btn.clicked.connect(self._validate_code)
            self._save_btn.clicked.connect(self._save_and_generate)
            cancel_btn.clicked.connect(self.reject)
            self._add_param_btn.clicked.connect(self._add_param)
            self._rem_param_btn.clicked.connect(self._remove_param)
            self._add_out_btn.clicked.connect(self._add_output)
            self._rem_out_btn.clicked.connect(self._remove_output)

        def _populate(self, spec: CustomStepSpec):
            self._id_edit.setText(spec.step_id)
            self._name_edit.setText(spec.display_name)
            self._group_edit.setText(spec.group)
            self._help_edit.setText(spec.help_text)
            self._code_edit.setPlainText(spec.code_body)
            self._params = list(spec.parameters)
            self._outputs = list(spec.outputs)
            for p in spec.parameters:
                self._param_list.addItem(f"{p.name} ({p.kind})")
            for o in spec.outputs:
                self._output_list.addItem(f"{o.name} ({o.kind})")

        def _add_param(self):
            from .param_editor_dialog import ParamEditorDialog
            dlg = ParamEditorDialog(parent=self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                p = dlg.get_param_def()
                self._params.append(p)
                self._param_list.addItem(f"{p.name} ({p.kind})")

        def _remove_param(self):
            row = self._param_list.currentRow()
            if row >= 0:
                self._param_list.takeItem(row)
                del self._params[row]

        def _add_output(self):
            out_name, ok = QInputDialog.getText(self, "Output name", "Name:")
            if not ok or not out_name.strip():
                return
            kind, ok = QInputDialog.getItem(
                self,
                "Output kind",
                "Kind:",
                ["vector", "raster", "number", "string", "boolean"],
                0,
                False,
            )
            if not ok:
                return
            out = OutputDef(name=out_name.strip(), kind=kind, description=out_name.strip())
            self._outputs.append(out)
            self._output_list.addItem(f"{out.name} ({out.kind})")

        def _remove_output(self):
            row = self._output_list.currentRow()
            if row >= 0:
                self._output_list.takeItem(row)
                del self._outputs[row]

        def _validate_code(self):
            code   = self._code_edit.toPlainText()
            errors = self._svc.validate_code_body(code)
            if errors:
                self._issues_label.setText("Issues:\n" + "\n".join(errors))
            else:
                self._issues_label.setText("")
                QMessageBox.information(self, "Validation", "Code looks valid!")

        def _save_and_generate(self):
            step_id = self._id_edit.text().strip()
            name    = self._name_edit.text().strip()
            if not step_id or not name:
                QMessageBox.warning(self, "Required", "Step ID and display name are required.")
                return

            code   = self._code_edit.toPlainText()
            errors = self._svc.validate_code_body(code)
            if errors:
                self._issues_label.setText("Cannot save — fix issues first:\n" + "\n".join(errors))
                return

            spec = CustomStepSpec(
                step_id=step_id,
                display_name=name,
                group=self._group_edit.text().strip() or "User Steps",
                help_text=self._help_edit.text().strip(),
                parameters=list(self._params),
                outputs=list(self._outputs),
                code_body=code,
            )
            py_path = self._svc.generate_and_save(spec)
            self.generated_py_path = py_path
            self.generated_step_id = step_id
            QMessageBox.information(
                self, "Saved",
                f"Custom step saved and generated:\n{py_path}\n\n"
                f"It can now be registered in the active ModelForge provider."
            )
            self.accept()

else:
    class CustomStepDialog:
        pass
