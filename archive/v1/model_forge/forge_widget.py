import os
import json

from qgis.PyQt.QtCore import Qt, QSettings
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QPushButton, QTextEdit, QLineEdit, QComboBox, QCheckBox,
    QMessageBox, QProgressBar, QFileDialog, QTabWidget,
    QListWidget, QListWidgetItem, QAbstractItemView, QPlainTextEdit
)
from qgis.core import QgsApplication, QgsProject

from .llm_backend import LLMBackend
from .model_builder import ModelBuilder


class ForgeWidget(QWidget):
    """Main widget for Model Forge"""

    def __init__(self, iface):
        super().__init__()
        self.iface = iface
        self.llm = LLMBackend()
        self.builder = ModelBuilder()

        self.init_ui()
        self.load_settings()
        self.load_layers()
        self.connect_project_signals()

    def connect_project_signals(self):
        project = QgsProject.instance()
        project.layersAdded.connect(self.on_layers_changed)
        project.layersRemoved.connect(self.on_layers_changed)

    def on_layers_changed(self, *args):
        self.load_layers()

    def disconnect_signals(self):
        try:
            project = QgsProject.instance()
            project.layersAdded.disconnect(self.on_layers_changed)
            project.layersRemoved.disconnect(self.on_layers_changed)
        except:
            pass

    def init_ui(self):
        main_layout = QVBoxLayout()
        main_layout.setSpacing(8)

        self.tabs = QTabWidget()

        self.generate_tab = self.create_generate_tab()
        self.tabs.addTab(self.generate_tab, "Generate")

        self.settings_tab = self.create_settings_tab()
        self.tabs.addTab(self.settings_tab, "Settings")

        self.history_tab = self.create_history_tab()
        self.tabs.addTab(self.history_tab, "History")

        main_layout.addWidget(self.tabs)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        main_layout.addWidget(self.progress_bar)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("font-size: 9pt;")
        main_layout.addWidget(self.status_label)

        self.setLayout(main_layout)

    def create_generate_tab(self):
        tab = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(6)

        desc_group = QGroupBox("Workflow description")
        desc_layout = QVBoxLayout()
        desc_layout.setSpacing(4)

        hint_label = QLabel(
            "Describe the geoprocessing workflow you need. "
            "Mention input types, operations and desired outputs."
        )
        hint_label.setWordWrap(True)
        hint_label.setStyleSheet("color: gray; font-size: 8pt;")
        desc_layout.addWidget(hint_label)

        self.description_edit = QTextEdit()
        self.description_edit.setPlaceholderText(
            "Example: Take multiple vector layers and a clip polygon. "
            "Clip all vectors to the polygon, then dissolve each by a field, "
            "then compute area statistics for each dissolved layer."
        )
        self.description_edit.setMinimumHeight(120)
        desc_layout.addWidget(self.description_edit)

        examples_layout = QHBoxLayout()
        examples_layout.addWidget(QLabel("Quick patterns:"))
        self.pattern_combo = QComboBox()
        self.pattern_combo.addItems([
            "(select a pattern)",
            "Multi-layer clip + dissolve + stats",
            "Buffer + intersection + join",
            "Raster reclassify + zonal statistics",
            "Reproject + clip + field calculator",
            "Points to polygon + area calculation",
            "DEM → slope + hillshade + viewshed",
            "Vector difference + merge + dissolve",
        ])
        self.pattern_combo.currentIndexChanged.connect(self.on_pattern_selected)
        examples_layout.addWidget(self.pattern_combo)
        desc_layout.addLayout(examples_layout)

        desc_group.setLayout(desc_layout)
        layout.addWidget(desc_group)

        context_group = QGroupBox("Context layers (optional)")
        context_layout = QVBoxLayout()
        context_layout.setSpacing(4)

        context_hint = QLabel("Select layers to give the LLM context about available data:")
        context_hint.setStyleSheet("color: gray; font-size: 8pt;")
        context_layout.addWidget(context_hint)

        self.layer_list = QListWidget()
        self.layer_list.setSelectionMode(QAbstractItemView.MultiSelection)
        self.layer_list.setMaximumHeight(100)
        context_layout.addWidget(self.layer_list)

        context_group.setLayout(context_layout)
        layout.addWidget(context_group)

        output_group = QGroupBox("Model output")
        output_layout = QVBoxLayout()
        output_layout.setSpacing(4)

        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("Model name:"))
        self.model_name_edit = QLineEdit()
        self.model_name_edit.setPlaceholderText("my_workflow")
        name_layout.addWidget(self.model_name_edit)
        output_layout.addLayout(name_layout)

        group_layout = QHBoxLayout()
        group_layout.addWidget(QLabel("Model group:"))
        self.model_group_edit = QLineEdit()
        self.model_group_edit.setText("Model Forge")
        group_layout.addWidget(self.model_group_edit)
        output_layout.addLayout(group_layout)

        self.open_designer_cb = QCheckBox("Open in Model Designer after generation")
        self.open_designer_cb.setChecked(True)
        output_layout.addWidget(self.open_designer_cb)

        output_group.setLayout(output_layout)
        layout.addWidget(output_group)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.generate_btn = QPushButton("⚡ Generate Model")
        self.generate_btn.setStyleSheet("font-weight: bold; padding: 6px 20px;")
        self.generate_btn.clicked.connect(self.generate_model)
        btn_layout.addWidget(self.generate_btn)
        layout.addLayout(btn_layout)

        self.result_group = QGroupBox("Generated model definition")
        result_layout = QVBoxLayout()
        self.result_edit = QPlainTextEdit()
        self.result_edit.setReadOnly(True)
        self.result_edit.setMaximumHeight(160)
        self.result_edit.setStyleSheet("font-family: monospace; font-size: 8pt;")
        result_layout.addWidget(self.result_edit)

        result_btn_layout = QHBoxLayout()
        self.save_btn = QPushButton("Save .model3")
        self.save_btn.clicked.connect(self.save_model)
        self.save_btn.setEnabled(False)
        result_btn_layout.addWidget(self.save_btn)

        self.open_btn = QPushButton("Open in Designer")
        self.open_btn.clicked.connect(self.open_in_designer)
        self.open_btn.setEnabled(False)
        result_btn_layout.addWidget(self.open_btn)
        result_btn_layout.addStretch()
        result_layout.addLayout(result_btn_layout)

        self.result_group.setLayout(result_layout)
        self.result_group.setVisible(False)
        layout.addWidget(self.result_group)

        layout.addStretch()
        tab.setLayout(layout)
        return tab

    def create_settings_tab(self):
        tab = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(6)

        provider_group = QGroupBox("LLM Provider")
        provider_layout = QVBoxLayout()
        provider_layout.setSpacing(4)

        type_layout = QHBoxLayout()
        type_layout.addWidget(QLabel("Backend:"))
        self.backend_combo = QComboBox()
        self.backend_combo.addItems(["Ollama (local)", "OpenAI-compatible API"])
        self.backend_combo.currentIndexChanged.connect(self.on_backend_changed)
        type_layout.addWidget(self.backend_combo)
        provider_layout.addLayout(type_layout)

        url_layout = QHBoxLayout()
        url_layout.addWidget(QLabel("API URL:"))
        self.api_url_edit = QLineEdit()
        self.api_url_edit.setPlaceholderText("http://localhost:11434")
        url_layout.addWidget(self.api_url_edit)
        provider_layout.addLayout(url_layout)

        key_layout = QHBoxLayout()
        key_layout.addWidget(QLabel("API Key:"))
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setPlaceholderText("(not needed for Ollama)")
        key_layout.addWidget(self.api_key_edit)
        provider_layout.addLayout(key_layout)

        model_layout = QHBoxLayout()
        model_layout.addWidget(QLabel("Model:"))
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.addItems([
            "qwen2.5-coder:7b",
            "llama3.1:8b",
            "mistral:7b",
            "codellama:13b",
            "gpt-4o",
            "gpt-4o-mini",
        ])
        model_layout.addWidget(self.model_combo)
        provider_layout.addLayout(model_layout)

        self.test_btn = QPushButton("Test Connection")
        self.test_btn.clicked.connect(self.test_connection)
        provider_layout.addWidget(self.test_btn)

        provider_group.setLayout(provider_layout)
        layout.addWidget(provider_group)

        advanced_group = QGroupBox("Advanced")
        advanced_layout = QVBoxLayout()
        advanced_layout.setSpacing(4)

        temp_layout = QHBoxLayout()
        temp_layout.addWidget(QLabel("Temperature:"))
        self.temp_edit = QLineEdit("0.2")
        self.temp_edit.setMaximumWidth(60)
        temp_layout.addWidget(self.temp_edit)
        temp_layout.addStretch()
        advanced_layout.addLayout(temp_layout)

        self.include_layer_context_cb = QCheckBox("Include layer metadata in prompt")
        self.include_layer_context_cb.setChecked(True)
        advanced_layout.addWidget(self.include_layer_context_cb)

        save_settings_btn = QPushButton("Save Settings")
        save_settings_btn.clicked.connect(self.save_settings)
        advanced_layout.addWidget(save_settings_btn)

        advanced_group.setLayout(advanced_layout)
        layout.addWidget(advanced_group)

        layout.addStretch()
        tab.setLayout(layout)
        return tab

    def create_history_tab(self):
        tab = QWidget()
        layout = QVBoxLayout()
        layout.setSpacing(6)

        self.history_list = QListWidget()
        layout.addWidget(self.history_list)

        clear_btn = QPushButton("Clear History")
        clear_btn.clicked.connect(self.history_list.clear)
        layout.addWidget(clear_btn)

        layout.addStretch()
        tab.setLayout(layout)
        return tab

    def on_backend_changed(self, index):
        if index == 0:
            self.api_url_edit.setPlaceholderText("http://localhost:11434")
            self.api_key_edit.setPlaceholderText("(not needed for Ollama)")
        else:
            self.api_url_edit.setPlaceholderText("https://api.openai.com/v1")
            self.api_key_edit.setPlaceholderText("sk-...")

    PATTERN_DESCRIPTIONS = {
        1: (
            "Take multiple vector layers and a clip polygon as inputs. "
            "Clip each vector layer to the clip polygon, then dissolve "
            "each clipped layer by a specified field, then calculate "
            "basic statistics (count, sum of area) for each dissolved layer."
        ),
        2: (
            "Take a point or polygon layer and a distance value as inputs. "
            "Create a buffer around the input layer features, then intersect "
            "the buffer with a target layer, then perform a spatial join to "
            "attach attributes from the target to the buffered features."
        ),
        3: (
            "Take a raster layer and a vector zone layer as inputs. "
            "Reclassify the raster into categories using a reclassification table, "
            "then compute zonal statistics of the reclassified raster for each "
            "polygon in the zone layer."
        ),
        4: (
            "Take a vector layer, a target CRS, and a clip polygon as inputs. "
            "Reproject the vector layer to the target CRS, then clip it to the "
            "polygon boundary, then add a new field computed with a field "
            "calculator expression."
        ),
        5: (
            "Take a point layer and a grouping field as inputs. "
            "Convert points to polygons (convex hull or minimum bounding geometry) "
            "grouped by the field, then calculate the area of each resulting polygon "
            "and add it as a new attribute."
        ),
        6: (
            "Take a DEM raster layer and an observer point layer as inputs. "
            "Compute slope from the DEM, compute hillshade from the DEM, "
            "then run a viewshed analysis from the observer points on the DEM."
        ),
        7: (
            "Take two vector layers as inputs. Compute the geometric difference "
            "between them, then merge the difference result with a third layer, "
            "then dissolve the merged result by a specified field."
        ),
    }

    def on_pattern_selected(self, index):
        if index in self.PATTERN_DESCRIPTIONS:
            self.description_edit.setPlainText(self.PATTERN_DESCRIPTIONS[index])

    def load_layers(self):
        self.layer_list.clear()
        for layer in QgsProject.instance().mapLayers().values():
            if layer.type() == 0:
                item = QListWidgetItem(f"\U0001f4d0 {layer.name()}")
            elif layer.type() == 1:
                item = QListWidgetItem(f"\U0001f5fa {layer.name()}")
            else:
                continue
            item.setData(Qt.UserRole, layer)
            self.layer_list.addItem(item)

    def gather_layer_context(self):
        context_lines = []
        for i in range(self.layer_list.count()):
            item = self.layer_list.item(i)
            if not item.isSelected():
                continue
            layer = item.data(Qt.UserRole)
            if layer is None:
                continue
            info = {"name": layer.name(), "crs": layer.crs().authid()}
            if layer.type() == 0:
                info["type"] = "vector"
                info["geometry"] = layer.geometryType()
                info["feature_count"] = layer.featureCount()
                info["fields"] = [f.name() for f in layer.fields()]
            elif layer.type() == 1:
                info["type"] = "raster"
                info["bands"] = layer.bandCount()
                info["width"] = layer.width()
                info["height"] = layer.height()
            context_lines.append(json.dumps(info))
        return "\n".join(context_lines)

    def generate_model(self):
        description = self.description_edit.toPlainText().strip()
        if not description:
            QMessageBox.warning(self, "No Description", "Please describe the workflow you need.")
            return

        model_name = self.model_name_edit.text().strip() or "generated_model"
        model_group = self.model_group_edit.text().strip() or "Model Forge"

        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.status_label.setText("Generating model definition...")
        self.generate_btn.setEnabled(False)

        try:
            backend_type = "ollama" if self.backend_combo.currentIndex() == 0 else "openai"
            self.llm.configure(
                backend=backend_type,
                url=self.api_url_edit.text().strip(),
                api_key=self.api_key_edit.text().strip(),
                model=self.model_combo.currentText().strip(),
                temperature=float(self.temp_edit.text() or "0.2"),
            )

            layer_context = ""
            if self.include_layer_context_cb.isChecked():
                layer_context = self.gather_layer_context()

            workflow_json = self.llm.generate_workflow(
                description=description,
                model_name=model_name,
                model_group=model_group,
                layer_context=layer_context,
            )

            self.result_edit.setPlainText(json.dumps(workflow_json, indent=2))
            self.result_group.setVisible(True)
            self.save_btn.setEnabled(True)
            self.open_btn.setEnabled(True)

            self._last_workflow = workflow_json
            self._last_model_name = model_name
            self._last_model_group = model_group

            self.history_list.addItem(f"{model_name}: {description[:80]}...")
            self.status_label.setText(
                f"Model \"{model_name}\" generated with "
                f"{len(workflow_json.get('algorithms', []))} steps."
            )

        except Exception as e:
            QMessageBox.critical(
                self, "Generation Error",
                f"Error generating model:\n{str(e)}"
            )
            self.status_label.setText("")

        finally:
            self.progress_bar.setVisible(False)
            self.generate_btn.setEnabled(True)

    def save_model(self):
        if not hasattr(self, '_last_workflow'):
            return

        models_dir = os.path.join(
            QgsApplication.qgisSettingsDirPath(), "processing", "models"
        )
        os.makedirs(models_dir, exist_ok=True)
        default_path = os.path.join(models_dir, f"{self._last_model_name}.model3")

        path, _ = QFileDialog.getSaveFileName(
            self, "Save Model", default_path, "Processing Models (*.model3)"
        )
        if not path:
            return

        try:
            model = self.builder.build_model(
                self._last_workflow,
                self._last_model_name,
                self._last_model_group,
            )
            if model.toFile(path):
                self.status_label.setText(f"Model saved to {path}")
                if self.open_designer_cb.isChecked():
                    self._open_model_in_designer(path)
            else:
                QMessageBox.warning(self, "Save Error", "Could not write model file.")
        except Exception as e:
            QMessageBox.critical(
                self, "Build Error",
                f"Error building model:\n{str(e)}"
            )

    def open_in_designer(self):
        if not hasattr(self, '_last_workflow'):
            return

        import tempfile
        try:
            model = self.builder.build_model(
                self._last_workflow,
                self._last_model_name,
                self._last_model_group,
            )
            tmp = tempfile.NamedTemporaryFile(suffix=".model3", delete=False)
            tmp.close()
            if model.toFile(tmp.name):
                self._open_model_in_designer(tmp.name)
            else:
                QMessageBox.warning(self, "Error", "Could not create temporary model file.")
        except Exception as e:
            QMessageBox.critical(
                self, "Build Error",
                f"Error building model:\n{str(e)}"
            )

    def _open_model_in_designer(self, path):
        try:
            from processing.modeler.ModelerDialog import ModelerDialog
            from qgis.core import QgsProcessingModelAlgorithm

            model = QgsProcessingModelAlgorithm()
            model.fromFile(path)
            dlg = ModelerDialog(model)
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()
            self._designer_dlg = dlg
        except Exception as e:
            self.status_label.setText(f"Could not open Designer: {e}")

    def test_connection(self):
        try:
            backend_type = "ollama" if self.backend_combo.currentIndex() == 0 else "openai"
            self.llm.configure(
                backend=backend_type,
                url=self.api_url_edit.text().strip(),
                api_key=self.api_key_edit.text().strip(),
                model=self.model_combo.currentText().strip(),
                temperature=0.2,
            )
            result = self.llm.test_connection()
            if result:
                QMessageBox.information(self, "Connection OK", f"Connected successfully.\nModel: {self.model_combo.currentText()}")
            else:
                QMessageBox.warning(self, "Connection Failed", "Could not reach the LLM backend.")
        except Exception as e:
            QMessageBox.critical(self, "Connection Error", str(e))

    def save_settings(self):
        s = QSettings()
        s.setValue('ModelForge/backend', self.backend_combo.currentIndex())
        s.setValue('ModelForge/api_url', self.api_url_edit.text())
        s.setValue('ModelForge/api_key', self.api_key_edit.text())
        s.setValue('ModelForge/model', self.model_combo.currentText())
        s.setValue('ModelForge/temperature', self.temp_edit.text())
        s.setValue('ModelForge/include_layer_context', self.include_layer_context_cb.isChecked())
        self.status_label.setText("Settings saved.")

    def load_settings(self):
        s = QSettings()
        self.backend_combo.setCurrentIndex(int(s.value('ModelForge/backend', 0)))
        self.api_url_edit.setText(s.value('ModelForge/api_url', ''))
        self.api_key_edit.setText(s.value('ModelForge/api_key', ''))
        model = s.value('ModelForge/model', '')
        if model:
            self.model_combo.setCurrentText(model)
        self.temp_edit.setText(s.value('ModelForge/temperature', '0.2'))
        self.include_layer_context_cb.setChecked(
            s.value('ModelForge/include_layer_context', True, type=bool)
        )
