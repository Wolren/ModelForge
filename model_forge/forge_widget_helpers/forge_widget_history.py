import json
from datetime import datetime

from qgis.PyQt.QtWidgets import QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QMessageBox, QInputDialog
from qgis.PyQt.QtWidgets import QListWidget, QListWidgetItem, QWidget
from qgis.PyQt.QtCore import Qt, QSize, QSettings


class ForgeWidgetHistoryMixin:
    """Mixin for history-related functionality in ForgeWidget."""

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

    def _load_history_settings(self):
        s = QSettings()
        history_raw = s.value(
            "ModelForge/generation_history",
            s.value("ModelForge/Linked/generation_history", "[]"),
        )
        try:
            self._history_entries = json.loads(history_raw) if history_raw else []
            if not isinstance(self._history_entries, list):
                self._history_entries = []
        except Exception:
            self._history_entries = []

    def _save_history_settings(self):
        s = QSettings()
        s.setValue("ModelForge/generation_history", json.dumps(self._history_entries, ensure_ascii=False))

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
        self._save_history_settings()
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
        self.btn_improve.setEnabled(True)
        self.btn_auto_wire_steps.setEnabled(True)
        self.btn_relayout_json.setEnabled(True)
        self.tabs.setCurrentIndex(1)
        self._try_build_model(workflow)
        self._refresh_context_for_improve()
        self.lbl_status.setText("Loaded model from history.")

    def _on_delete_history_entry(self):
        idx = self._selected_history_index()
        if idx is None:
            QMessageBox.information(self, "History", "Select a history item first.")
            return
        del self._history_entries[idx]
        self._save_history_settings()
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
        self._save_history_settings()
        self._refresh_history_list()

    def _on_clear_history(self):
        if not self._history_entries:
            return
        self._history_entries = []
        self._save_history_settings()
        self._refresh_history_list()
