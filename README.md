# Model Forge ![Model Forge icon](model_forge/icon.png)

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

Model Forge is a QGIS plugin that helps you turn plain‑language descriptions of GIS workflows into editable Models. It is designed to make it easier to prototype, inspect and refine multi‑step geoprocessing without building every model node by hand. It is designed to arbitralily accept more LLM providers than [IntelliGeo](https://github.com/MahdiFarnaghi/intelli_geo)

---

## 1. User guide

### Installation

1. Install the plugin from the QGIS Plugin Manager (Plugins → Manage and Install Plugins).
2. After installation, open the panel from **Plugins → Model Forge → Open Model Forge**.
3. Configure your LLM backend in the **Settings** tab (URL, API key if needed, and model name).

### Generate a model from text

1. Go to the **Generate** tab.
2. In **Describe your workflow**, write what you want to do, for example:  
   “Buffer input points by 500 m, clip with the city boundary, then compute mean population per buffer.”
3. Optionally:
   - Set a **Name** and **Group** for the model.
   - Select one or more **Context layers** that describe your data.
   - Use **Layout and naming options**:
     - **Layout profile** (`compact`, `balanced`, `dense`, `spacious`, `debug`)
     - **Organisation** (`horizontal`, `vertical`, `axis`)
     - **Layout algorithm** (`sugiyama`, `topological`, `axis_pack`, `radial_shell`, `ancestor_weighted`)
   - Enable **Optimize generation (auto-fix errors)** to auto-retry with tighter prompts/context and suppress transient timeout/error noise.
4. Click **Generate Model**.
5. When generation finishes, the plugin switches to the **Model** tab and shows the resulting model JSON.

### Inspect and refine the model

In the **Model** tab:

- The **Model JSON (editable)** panel shows the generated workflow as JSON, with syntax highlighting.
- Use **Rebuild model from JSON above** after manual edits to rebuild the internal QGIS model object.
- Use **Save .model3** to write the current model to a `.model3` file that you can load from the standard Processing Model Designer.
- Use **Open in Designer** to open the model directly in the QGIS Model Designer with a pre‑computed layout.
- Use **Auto-wire model steps** to let the model builder automatically wire missing parameter connections.
- Use **Re-layout current JSON** to re-apply a selected profile/organisation/algorithm without regenerating the workflow.
- In **Model Designer**, use **Auto-layout (Model Forge)** for direct in-designer re-layout with current controls.

### History

- The **History** tab (between Model and Settings) stores recent generation attempts.
- You can **load**, **rename**, **delete**, or **clear** past attempts.
- Loaded entries restore the saved model JSON and layout controls.

### Debug and improve

- **Auto‑Repair (validation)** validates the current JSON structure and sends a repair request to the LLM if issues are found.
- **Send repair prompt** lets you describe what to fix or improve (for example “add dissolve after clip” or “change the field name to `pop_2020`”).  
  The plugin sends the current JSON plus your feedback to the LLM and updates the model JSON with the result.

### Backend settings

In the **Settings** tab:

- Choose an LLM provider and model.
- Configure URL, API key, and “thinking level” (temperature).
- Configure how many Processing algorithms and providers are exposed as context for the LLM.
- Click **Apply Settings** to save the configuration to your QGIS profile. Settings are persisted across sessions.

---

## 2. Developer notes

### Repository layout

Plugin variants in this monorepo:

- `model_forge/` — legacy standalone plugin (classic dock workflow + direct LLM JSON generation).
- `modelforge_arch/model_forge/` — architecture-first standalone plugin (MCP compiler + provider algorithms + custom step tooling).
- `modelforge_linked/` and `modelforge-linked/` — stitched standalone plugin variants that bundle both legacy UI flow and architecture-first compiler stack in one package (no sibling-folder linking required).

For the stitched standalone variant, `modelforge_linked/` is the canonical package (underscore). It includes:
- runtime registration of generated custom steps under the plugin provider (`model_forge:<step_id>`),
- contract-test fixture generation/execution for happy, boundary, and adversarial geometry/CRS cases,
- extended auto-layout presets: `compact`, `balanced`, `dense`, `spacious`, `debug`.

Key files and modules:

- `model_forge.py`  
  QGIS plugin entry point. Registers the plugin, adds the toolbar/menu actions, and creates the main dock widget.
- `forge_dock.py`  
  Dock widget wrapper that embeds the main `ForgeWidget` into a QGIS dock.
- `forge_widget.py`  
  Main UI logic. Implements the Generate / Model / Settings tabs, wiring between buttons, LLM backend, context collector and model builder.
- `llm_backend.py`  
  Thin abstraction over one or more LLM backends. Handles configuration (provider, URL, API key, model) and exposes methods:  
  - `generate_single_pass(description, name, group, context_text)`  
  - `generate_plan(description, context_text)`  
  - `generate_model_from_plan(plan, context_text)`  
  - `repair_model(workflow_json, errors, context_text)`
- `context_collector.py`  
  Collects information about the current project and Processing algorithms to send as textual context to the LLM. Supports limiting the number of algorithms and selecting providers.
- `model_builder.py`  
  Converts the workflow JSON into a `QgsProcessingModelAlgorithm`, creates inputs, algorithm components and connections.
- `model_layout.py`  
  Computes positions for inputs and algorithm components (simple DAG layout) so the model opens in the Model Designer with a readable arrangement.
- `resources.qrc` / generated `resources_rc.py`  
  Icon and other static assets.

### Threads and background work

- LLM calls and model repair are run in background threads to keep the QGIS UI responsive.
- `GenerateWorker(QThread)` runs generation (single‑pass or two‑phase) and emits:
  - `finished(dict)` with the workflow JSON,
  - `error(str)` with an error message and traceback.
- `RepairWorker(QThread)` runs model repair requests in the same pattern.
- The main widget connects to these signals and updates the UI (buttons, progress bar, labels) on the main thread.

### Settings and persistence

- The plugin uses `QSettings` with the prefix `ModelForge/` to persist:
  - backend key, URL, API key, model name,
  - temperature,
  - algorithm catalog settings (max algorithms, provider flags).
- Settings are loaded once in `ForgeWidget.__init__` and applied to the widgets in the Settings tab.

### Model JSON schema

The plugin expects and produces a simple JSON structure:

- Top‑level keys:
  - `"inputs"`: list of input definitions,
  - `"algorithms"`: list of algorithm steps.
- Each algorithm has:
  - `"id"`: unique identifier for the step,
  - `"algorithm_id"`: Processing provider id (e.g. `native:buffer`),
  - `"parameters"`: mapping of parameter name to values or references.
- Child outputs are expressed as:

  ```json
  { "type": "child_output", "child_id": "some_step_id" }
  ```

  and are used to build connections between model components.

- For output destination parameters (sink/destination) the linked builder now defaults missing values to temporary outputs, so finished layers are added to project layers by default unless explicitly overridden in JSON.

`_validate_model` in `forge_widget.py` performs basic structural checks (missing keys, duplicate ids, invalid child references) before attempting repair.

### Extending the plugin

Feel free to suggest prompt, UI or other improvements
