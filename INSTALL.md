# Installing Model Forge in QGIS

Model Forge ships as a standard QGIS plugin. You point QGIS at the
`model_forge/` subdirectory of this repo (which is the plugin root
because it contains `__init__.py` and `metadata.txt`).

## 1. Locate the plugin directory

The plugin root is the `model_forge/` folder in this repo. Example
path on Windows:

```
C:\Users\Wildbot\PycharmProjects\Model Forge\model_forge
```

## 2. Install in QGIS

1. Launch QGIS 3.28 or later.
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

## 3. Open the Map tab

The dock has four tabs: **Generate / Model / History / Map**. Click
the **Map** tab to use the AI map generator.

## 4. Use the Map tab — two paths

### A. With an LLM (recommended)

The Map tab calls the same LLM the **Generate** tab uses. Configure
it once in **Settings** (under the Generate tab):

1. Pick a backend (**Ollama** is the default and runs locally).
2. Enter the base URL (default `http://localhost:11434` for Ollama).
3. Enter the model name (default `gpt-oss:20b-cloud`).
4. Click **Test** to verify the connection, then **Save**.

Then in the Map tab:

1. Type a description in the box, e.g.
   *"Buffer the roads layer by 50m and produce a print map at A4."*
2. Pick a template (default / scientific / presentation / minimal).
3. Click **Generate Map**.
4. The plugin calls the LLM, generates a model, writes per-layer
   `.qml` files, builds a `.qpt` print layout, opens it in the
   QGIS layout designer, and shows the verifier report in the
   panel.

### B. Without an LLM (works out of the box)

The plugin works the same way even if you have not configured an
LLM. With at least one layer loaded in the project:

1. Type **any text** in the description box (it's used as the
   map's title; the rest is ignored).
2. Pick a template.
3. Click **Generate Map**.

The plugin will:
- Build a default model JSON (one entry per loaded layer).
- Write per-layer `.qml` files (single-symbol default symbology)
  and **apply them to the live layers in your project** so the
  map canvas updates immediately.
- Build a `.qpt` print layout pinned to the union of your layers'
  extents.
- Open the layout in the QGIS layout designer.

## 5. Export the map

In the layout designer window that opens:
- **Layout → Export as PDF…** for print.
- **Layout → Export as Image…** for PNG/SVG/JPG.

## 6. Troubleshooting

- **"Model Forge not in the list"** — make sure the directory
  contains `__init__.py` and `metadata.txt`. QGIS is picky about
  the path being a directory, not a file.
- **"Map tab is empty"** — the plugin loaded but the Map tab
  wasn't injected. Re-enable the plugin: uncheck the checkbox in
  the Plugin Manager, wait, re-check.
- **"Generate Map says Failed"** — if using an LLM, check the
  Settings tab's **Test** button. If no LLM, ensure at least one
  layer is loaded in the project.
- **"Layout opens but map is blank"** — the project's CRS doesn't
  match the layout's extent. Set the project CRS to match your
  data (Project → Properties → CRS), then click Generate Map
  again.

## 7. Where the files go

The plugin writes the generated artifacts to the project's
home directory (QGIS's "Project Home" setting; defaults to the
folder holding the `.qgz` file):

```
<project_home>/
  .model_forge/
    layouts/      ← generated .qpt files
    symbology/    ← generated .qml files
```

You can load the `.qml` files onto any layer via
**Layer Properties → Symbology → Style → Load Style…**.
