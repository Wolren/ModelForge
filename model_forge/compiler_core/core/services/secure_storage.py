"""Secure API key storage — keyring with QSettings fallback."""

from __future__ import annotations

_SERVICE_NAME = "model_forge"

_HAS_KEYRING = False
try:
    import keyring
    import keyring.errors

    _HAS_KEYRING = True
except ImportError:
    pass


def get_api_key(username: str = "llm_api_key") -> str | None:
    """Retrieve API key from keyring, falling back to QSettings."""
    if _HAS_KEYRING:
        try:
            val = keyring.get_password(_SERVICE_NAME, username)
            if val:
                return val
        except keyring.errors.KeyringError:
            pass

    try:
        from qgis.PyQt.QtCore import QSettings

        s = QSettings()
        return s.value("ModelForge/api_key", "")
    except ImportError:
        return ""


def set_api_key(api_key: str, username: str = "llm_api_key") -> None:
    """Store API key in keyring, fall back to QSettings."""
    if _HAS_KEYRING:
        try:
            keyring.set_password(_SERVICE_NAME, username, api_key)
            return
        except keyring.errors.KeyringError:
            pass

    try:
        from qgis.PyQt.QtCore import QSettings

        s = QSettings()
        s.setValue("ModelForge/api_key", api_key)
    except ImportError:
        pass


def delete_api_key(username: str = "llm_api_key") -> None:
    """Remove API key from keyring."""
    if _HAS_KEYRING:
        try:
            keyring.delete_password(_SERVICE_NAME, username)
            return
        except (keyring.errors.PasswordDeleteError, keyring.errors.KeyringError):
            pass

    try:
        from qgis.PyQt.QtCore import QSettings

        s = QSettings()
        s.remove("ModelForge/api_key")
    except ImportError:
        pass
