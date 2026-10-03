"""XDG base directories for QuotaGlance's own files."""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR_NAME = "quotaglance"


def _xdg(env_name: str, fallback: str) -> Path:
    value = os.environ.get(env_name)
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / fallback


def config_home() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config")


def cache_home() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache")


def state_home() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state")


def data_home() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share")


def config_dir() -> Path:
    return config_home() / APP_DIR_NAME


def cache_dir() -> Path:
    return cache_home() / APP_DIR_NAME


def state_dir() -> Path:
    return state_home() / APP_DIR_NAME


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Write a file atomically so a crash never leaves it half-written."""
    ensure_dir(path.parent)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)
