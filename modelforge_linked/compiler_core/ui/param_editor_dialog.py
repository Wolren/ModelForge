"""
ParamEditorDialog
=================
Mini dialog to define a single parameter for a custom step.
"""
from __future__ import annotations

try:
    from qgis.PyQt.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QLabel,
        QLineEdit, QComboBox, QCheckBox, QPushButton,
        QDialogButtonBox,
    )
    _HAS_QGIS = True
except ImportError:
    _HAS_QGIS = False

if _HAS_QGIS:
    from ..core.services.custom_step_author import ParamDef

    _PARAM_KINDS = [
        "vectorlayer", "rasterlayer", "number", "string",
        "boolean", "field", "expression", "crs", "extent",
        "sink", "featuresink",
    ]

    class ParamEditorDialog(QDialog):

        def __init__(self, param: ParamDef | None = None, parent=None):
            super().__init__(parent)
            self.setWindowTitle("Parameter Definition")
            self.setMinimumWidth(340)
            self._setup_ui()
            if param:
                self._populate(param)

        def _setup_ui(self):
            layout = QVBoxLayout(self)

            r1 = QHBoxLayout()
            r1.addWidget(QLabel("Name:"))
            self._name_edit = QLineEdit()
            r1.addWidget(self._name_edit, 2)
            layout.addLayout(r1)

            r2 = QHBoxLayout()
            r2.addWidget(QLabel("Kind:"))
            self._kind_combo = QComboBox()
            self._kind_combo.addItems(_PARAM_KINDS)
            r2.addWidget(self._kind_combo, 2)
            layout.addLayout(r2)

            r3 = QHBoxLayout()
            r3.addWidget(QLabel("Description:"))
            self._desc_edit = QLineEdit()
            r3.addWidget(self._desc_edit, 3)
            layout.addLayout(r3)

            self._optional_check = QCheckBox("Optional")
            layout.addWidget(self._optional_check)

            btns = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
            )
            btns.accepted.connect(self.accept)
            btns.rejected.connect(self.reject)
            layout.addWidget(btns)

        def _populate(self, param: ParamDef):
            self._name_edit.setText(param.name)
            idx = _PARAM_KINDS.index(param.kind) if param.kind in _PARAM_KINDS else 0
            self._kind_combo.setCurrentIndex(idx)
            self._desc_edit.setText(param.description)
            self._optional_check.setChecked(param.optional)

        def get_param_def(self) -> ParamDef:
            return ParamDef(
                name=self._name_edit.text().strip(),
                kind=self._kind_combo.currentText(),
                description=self._desc_edit.text().strip(),
                optional=self._optional_check.isChecked(),
            )

else:
    class ParamEditorDialog:
        pass
