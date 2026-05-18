# Model Forge

![Model Forge banner](model_forge/icon.png)

> **EXPERIMENTAL** — This plugin is a work in progress. APIs, features, and UI may change without notice. External links and documentation may become outdated or broken.

[![GPL v3](https://img.shields.io/badge/License-GPL%20v3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0.html)

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![QGIS](https://img.shields.io/badge/QGIS-4.0+-green)](https://www.qgis.org/)
[![Qt](https://img.shields.io/badge/Qt-6.x-green)](https://www.qt.io/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![QGIS](https://img.shields.io/badge/QGIS-4.0+-green)](https://www.qgis.org/)
[![Qt](https://img.shields.io/badge/Qt-6.x-green)](https://www.qt.io/)

---

## What is Model Forge?

Model Forge is a QGIS plugin that generates editable geoprocessing models from plain-language descriptions. It bridges AI language models with QGIS Processing Framework, transforming natural language workflow descriptions into visual models ready for editing, refinement, and execution.

### Gallery


| Generate Tab                              | Model Tab                           | Settings Tab                              | History Tab                             |
| ----------------------------------------- | ----------------------------------- | ----------------------------------------- | --------------------------------------- |
| ![Generate tab](gallery/generate-tab.png) | ![Model tab](gallery/model-tab.png) | ![Settings tab](gallery/settings-tab.png) | ![History tab](gallery/history-tab.png) |

### Architecture

```
User Description --> LLM Backend --> QGIS Processing Model
(e.g. "Buffer    (OpenAI,         (.model3 file,
 then clip...")  Ollama, any     editable in Designer)
                  OpenAI-compatible)
```

### Key Capabilities

- **Natural language to model** — Describe workflows like "Buffer input points by 500m, clip with city boundary, compute mean population"
- **Multi-LLM support** — Works with any OpenAI-compatible API (OpenAI, Ollama, Anthropic, local models, custom endpoints). Not locked to a single provider unlike [IntelliGeo](https://github.com/MahdiFarnaghi/intelli_geo)
- **Visual model generation** — Opens directly in QGIS Model Designer with pre-computed layouts
- **Iterative refinement** — Use repair prompts to fix or extend generated models
- **Layout algorithms** — Sugiyama, topological, axis pack, radial shell, ancestor weighted

---

## Known Issues

- External links may become outdated or broken
- Experimental features may change without notice
- Generated model quality depends on LLM capability
- Custom step registration does not persist across sessions

---

## User Guide

### Installation

1. Install via QGIS Plugin Manager (Plugins -> Manage and Install Plugins -> search "Model Forge")
2. Open panel: Plugins -> Model Forge -> Open Model Forge
3. Configure LLM backend in Settings tab

### Generate a Model

1. Go to **Generate** tab
2. Enter workflow description:
   > *"Buffer input points by 500 m, clip with the city boundary, then compute mean population per buffer."*
   >
3. Optional: set Name/Group, select Context layers, choose Layout profile/organisation/algorithm
4. Click **Generate Model**
5. View result in **Model** tab

### Model Tab Functions


| Action                    | Purpose                                              |
| ------------------------- | ---------------------------------------------------- |
| Model JSON (editable)     | View/edit workflow JSON                              |
| Rebuild model from JSON   | Apply manual edits to QGIS model                     |
| Save .model3              | Export to file loadable in Processing Model Designer |
| Open in Designer          | Launch Model Designer with pre-computed layout       |
| Auto-wire model steps     | Auto-connect missing parameter connections           |
| Re-layout current JSON    | Re-apply layout without regeneration                 |
| Auto-layout (Model Forge) | In-Designer re-layout                                |

### History

- **History** tab stores recent generation attempts
- Load, rename, delete, or clear past entries
- Restores saved model JSON and layout controls

### Repair Mode

- **Auto-Repair** — validates JSON structure and sends repair request to LLM if issues found
- **Send repair prompt** — describe fixes (e.g., "add dissolve after clip", "rename field to pop_2020")

### Settings

- Provider selection (OpenAI, Ollama, custom)
- API URL, key, temperature
- Algorithm catalog configuration

---

## Developer Notes

### Repository Structure

```
Model Forge/
├── model_forge/                    # Canonical stitched plugin
│   ├── model_forge.py             # Plugin entry point
│   ├── forge_dock.py              # Dock widget wrapper
│   ├── forge_widget.py            # Main UI (Generate/Model/Settings)
│   ├── forge_generate_worker.py   # Background generation thread
│   ├── legacy_base/               # Original LLM->JSON workflow
│   └── compiler_core/             # MCP compiler pipeline
├── model_forge_initial/           # Legacy variant (deprecated)
└── modelforge_arch/               # Architecture variant (deprecated)
```

### Key Modules


| Module                 | Purpose                              |
| ---------------------- | ------------------------------------ |
| `model_forge.py`       | Plugin registration, toolbar, menu   |
| `forge_dock.py`        | Embeds ForgeWidget into QGIS dock    |
| `forge_widget.py`      | UI logic, tab management, LLM wiring |
| `llm_backend.py`       | LLM abstraction layer                |
| `context_collector.py` | Gathers project/algorithm context    |
| `model_builder.py`     | JSON -> QingProcessingModelAlgorithm |
| `model_layout.py`      | DAG layout computation               |

### Concurrency

- LLM calls run in background QThread
- GenerateWorker emits finished(dict) or error(str)
- RepairWorker follows identical pattern

### Persistence

- QSettings with prefix `ModelForge/`
- Stores: backend config, URL, API key, temperature, algorithm catalog

### Model JSON Schema

```json
{
  "inputs": [{ "id": "input_points", "type": "vector", "geometry": "point" }],
  "algorithms": [
    {
      "id": "buffer_step",
      "algorithm_id": "native:buffer",
      "parameters": {
        "INPUT": { "type": "child_output", "child_id": "input_points" },
        "DISTANCE": 500
      }
    }
  ]
}
```

Child outputs use `{ "type": "child_output", "child_id": "step_id" }`. Validation performed by `_validate_model()` in forge_widget.py.

---

## Support

- Report issues: https://github.com/Wolren/ModelForge/issues
- Verify all external links before relying on them

---

*Last updated: 05.2026*
