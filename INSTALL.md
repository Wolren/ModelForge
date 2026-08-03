# Installing Model Forge in QGIS

Model Forge ships as a standard QGIS plugin. You point QGIS at the
`model_forge/` subdirectory of this repo (which is the plugin root
because it contains `__init__.py` and `metadata.txt`).

## 1. Locate the plugin directory

The plugin root is the `model_forge/` folder in this repo. Example
path on Windows:

```
D:\Projects\python\Model Forge\model_forge
```

## 2. Install in QGIS

1. Launch QGIS 3.22 or later.
2. **Plugins → Manage and Install Plugins…**
3. On the left, click **Settings** (the gear icon).
4. Under **Plugin Directories**, click **Add…** and browse to the
   `model_forge` folder. If the folder shows a red icon (QGIS
   couldn't parse the metadata), double-check that you're pointing
   at the **folder** (not a file inside it) and that the folder
   contains `__init__.py` and `metadata.txt`.
5. QGIS scans the new directory; close the Settings tab and switch
   to the **Installed** tab.
6. Find **Model Forge** in the list, tick the checkbox to enable.
7. The **Model Forge** toolbar button (panel icon) appears. Click
   it to open the **Model Forge** dock on the right side of QGIS.

## 3. The dock

The dock has four tabs: **Generate / Model / History / Settings**.

- **Generate** - enter a workflow description, configure options,
  and generate a model.
- **Model** - view and edit the model JSON, rebuild, save `.model3`,
  open in the Model Designer, re-layout, or auto-wire steps.
- **History** - load, rename, delete, or clear past generations.
- **Settings** - pick the LLM backend, API URL, key, temperature,
  and algorithm catalog.

The **Generate** tab calls the LLM backend configured in
**Settings**. Pick a backend (**Ollama** is the default and runs
locally), enter the base URL (default `http://localhost:11434` for
Ollama), enter the model name, click **Test** to verify the
connection, then **Save**.

## 4. Troubleshooting

- **"Model Forge not in the list"** - make sure the directory
  contains `__init__.py` and `metadata.txt`. QGIS is picky about
  the path being a directory, not a file.
- **"Generation says Failed"** - check the Settings tab's **Test**
  button. The backend must be reachable and the model name must
  exist on it.
- **Model Designer doesn't open** - the model is still saved as
  `.model3`; use **Save .model3** in the Model tab and open it
  manually via Processing → Models → Open Existing Model.

## 5. MCP server

The Settings tab also has a **Start MCP Server** button that runs
the MCP server in a background thread (it also copies a ready-made
client config to the clipboard). See [docs/MCP.md](docs/MCP.md) for
the full MCP server reference, including the standalone `python -m
model_forge.mcp_server` entry point.
