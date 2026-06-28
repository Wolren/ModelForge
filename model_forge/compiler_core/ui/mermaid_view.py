"""MermaidGraphView — rendered mermaid flowchart inside QGIS.

Uses QWebEngineView when available; falls back to a plain-text
display so the user at least sees the diagram code.
"""

from __future__ import annotations

import logging
import os

from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtWidgets import QPlainTextEdit, QVBoxLayout, QWidget

_HAS_WEBENGINE = False
QWebEngineView = None
for mod in (
    "qgis.PyQt.QtWebEngineWidgets.QWebEngineView",
    "PyQt5.QtWebEngineWidgets.QWebEngineView",
    "PyQt6.QtWebEngineWidgets.QWebEngineView",
):
    try:
        parts = mod.split(".")
        exec(
            f"from {'.'.join(parts[:-1])} import {parts[-1]}",
            globals(),
        )
        _HAS_WEBENGINE = True
        break
    except ImportError:
        continue

log = logging.getLogger(__name__)

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8">
<style>
  body { margin: 0; background: #1e1e1e; }
  #graph { width: 100vw; height: 100vh; }
</style></head>
<body>
<div class="mermaid" id="graph">
{mermaid_code}
</div>
<script src="{mermaid_js_path}"></script>
<script>mermaid.initialize({{ theme: "dark", startOnLoad: true }});</script>
</body>
</html>"""


class MermaidGraphView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._view = None
        self._mermaid_js_path = self._find_mermaid_js()

        if _HAS_WEBENGINE and self._view is None:
            try:
                layout = QVBoxLayout()
                layout.setContentsMargins(0, 0, 0, 0)
                self._view = QWebEngineView()
                self._view.setParent(self)
                layout.addWidget(self._view)
                self.setLayout(layout)
            except Exception:  # noqa: BLE001
                self._view = None

        if self._view is None:
            # Fallback: show mermaid text in a read‑only box.
            layout = QVBoxLayout()
            layout.setContentsMargins(0, 0, 0, 0)
            self._text_fallback = QPlainTextEdit()
            self._text_fallback.setReadOnly(True)
            self._text_fallback.setPlainText("Mermaid diagram (QWebEngine not available):")
            layout.addWidget(self._text_fallback)
            self.setLayout(layout)

    def cleanup(self):
        if self._view is not None:
            self._view.deleteLater()
            self._view = None

    def _find_mermaid_js(self) -> str:
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
        if not mermaid_text.strip():
            mermaid_text = "flowchart TD\n  empty[No model data]"

        if self._view is not None:
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
        else:
            # Fallback: show raw mermaid text.
            self._text_fallback.setPlainText(
                "Mermaid diagram (QWebEngine not available, showing code):\n\n" + mermaid_text
            )

    @property
    def is_available(self) -> bool:
        return self._view is not None
