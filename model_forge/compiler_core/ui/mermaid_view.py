"""MermaidGraphView — rendered mermaid flowchart inside QGIS using QWebEngineView."""

from __future__ import annotations

import os

from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtWidgets import QVBoxLayout, QWidget

_HAS_WEBENGINE = False
try:
    from qgis.PyQt.QtWebEngineWidgets import QWebEngineView

    _HAS_WEBENGINE = True
except ImportError:
    try:
        from qgis.PyQt.QtWebEngineWidgets import QWebEngineView

        _HAS_WEBENGINE = True
    except ImportError:
        _HAS_WEBENGINE = False

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  body {{ margin: 0; background: #1e1e1e; }}
  #graph {{ width: 100vw; height: 100vh; }}
</style>
</head>
<body>
<div class="mermaid" id="graph">
{mermaid_code}
</div>
<script src="{mermaid_js_path}"></script>
<script>mermaid.initialize({{ theme: "dark", startOnLoad: true }});</script>
</body>
</html>"""


class MermaidGraphView(QWidget):
    """Rendered mermaid flowchart using QWebEngineView (if available)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._view = None
        self._mermaid_js_path = self._find_mermaid_js()

        if _HAS_WEBENGINE and self._mermaid_js_path:
            layout = QVBoxLayout()
            layout.setContentsMargins(0, 0, 0, 0)
            self._view = QWebEngineView()
            self._view.setParent(self)
            layout.addWidget(self._view)
            self.setLayout(layout)

    def cleanup(self):
        if self._view is not None:
            self._view.deleteLater()
            self._view = None

    def _find_mermaid_js(self) -> str:
        """Look for mermaid.min.js bundled with the plugin."""
        plugin_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        candidates = [
            os.path.join(plugin_dir, "resources", "mermaid", "mermaid.min.js"),
            os.path.join(plugin_dir, "resources", "mermaid.js"),
        ]
        for path in candidates:
            if os.path.isfile(path):
                return path.replace("\\", "/")
        return ""

    def set_mermaid(self, mermaid_text: str) -> None:
        if self._view is None:
            return
        if not mermaid_text.strip():
            mermaid_text = "flowchart TD\n  empty[No model data]"

        if self._mermaid_js_path and not self._mermaid_js_path.startswith("http"):
            mermaid_js_url = QUrl.fromLocalFile(self._mermaid_js_path).toString()
        else:
            mermaid_js_url = (
                self._mermaid_js_path
                or "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"
            )

        html = _HTML_TEMPLATE.format(
            mermaid_code=mermaid_text,
            mermaid_js_path=mermaid_js_url,
        )
        self._view.setHtml(html, QUrl("about:blank"))

    @property
    def is_available(self) -> bool:
        return self._view is not None
