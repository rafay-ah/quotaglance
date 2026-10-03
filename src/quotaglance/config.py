"""User settings, stored as JSON in ``~/.config/quotaglance/config.json``.

Secrets never live here: API keys go to GNOME Keyring (see ``secrets.py``).
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from quotaglance import paths

log = logging.getLogger(__name__)

CONFIG_VERSION = 1

REFRESH_CHOICES = (1, 2, 5, 10, 15, 30)
WIDGET_SIZES = ("small", "medium", "large")

DEFAULTS: dict[str, Any] = {
    "version": CONFIG_VERSION,
    "refresh_minutes": 5,
    "autostart": True,
    "notifications": {
        "enabled": True,
        "warn_80": True,
        "warn_95": True,
        "on_reset": False,
    },
    "panel": {
        # "highest": most constrained window across enabled providers,
        # otherwise a provider id to always show.
        "mode": "highest",
        "show_percent": True,
    },
    "widget": {
        "visible": False,
        "size": "medium",
        "pinned": False,
        "tinted": False,
        "position": None,  # [x, y], reported by the Shell extension
    },
    # provider id -> {"enabled": bool, ...provider specific options}
    "providers": {},
    "provider_order": [],
    "first_run_complete": False,
}

Listener = Callable[[str], None]


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class Config:
    def __init__(self, path: Path | None = None, autosave: bool = True) -> None:
        self.path = path or paths.config_dir() / "config.json"
        self.autosave = autosave
        self._listeners: dict[int, Listener] = {}
        self._next_id = 1
        self.data = self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("config root is not an object")
        except FileNotFoundError:
            return copy.deepcopy(DEFAULTS)
        except (OSError, ValueError) as exc:
            log.warning("Ignoring unreadable config %s: %s", self.path, exc)
            return copy.deepcopy(DEFAULTS)
        return _deep_merge(DEFAULTS, raw)

    def save(self) -> None:
        try:
            paths.atomic_write_text(self.path, json.dumps(self.data, indent=2, sort_keys=True))
        except OSError as exc:
            log.error("Could not save config %s: %s", self.path, exc)

    # -- change notification ----------------------------------------------

    def connect(self, listener: Listener) -> int:
        handler = self._next_id
        self._next_id += 1
        self._listeners[handler] = listener
        return handler

    def disconnect(self, handler: int) -> None:
        self._listeners.pop(handler, None)

    def _changed(self, key: str) -> None:
        if self.autosave:
            self.save()
        for listener in list(self._listeners.values()):
            try:
                listener(key)
            except Exception:  # a bad listener must not break settings
                log.exception("Config listener failed for %s", key)

    # -- generic access ---------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return copy.deepcopy(node)

    def set(self, key: str, value: Any) -> None:
        parts = key.split(".")
        node = self.data
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        if node.get(parts[-1]) == value:
            return
        node[parts[-1]] = copy.deepcopy(value)
        self._changed(key)

    # -- providers -----------------------------------------------------------

    def provider_settings(self, provider_id: str) -> dict[str, Any]:
        settings = self.data.get("providers", {}).get(provider_id, {})
        return copy.deepcopy(settings) if isinstance(settings, dict) else {}

    def provider_enabled(self, provider_id: str) -> bool | None:
        """True/False once decided; None means "not decided yet" (first run)."""
        value = self.provider_settings(provider_id).get("enabled")
        return value if isinstance(value, bool) else None

    def set_provider(self, provider_id: str, key: str, value: Any) -> None:
        self.set(f"providers.{provider_id}.{key}", value)

    @property
    def refresh_seconds(self) -> int:
        minutes = self.get("refresh_minutes", 5)
        if not isinstance(minutes, (int, float)) or minutes <= 0:
            minutes = 5
        return int(max(1, minutes) * 60)
