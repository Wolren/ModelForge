"""
Centralized logging configuration for ModelForge.
All modules should use ``log = logging.getLogger(__name__)``
and emit messages via the standard logging interface.
"""

import logging
import sys


def configure_logger(
    name: str = "model_forge",
    level: int = logging.INFO,
    fmt: str = "[%(name)s] %(levelname)s %(message)s",
) -> None:
    root = logging.getLogger(name)
    if root.handlers:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(fmt))
    root.setLevel(level)
    root.addHandler(handler)
