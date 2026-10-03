"""Claude Code status-line bridge (official, zero network).

Claude Code (2.1.80+) passes the current 5-hour and weekly usage to the
command configured as its status line, on every assistant message. When the
user opts in from Preferences, QuotaGlance wraps their status line: the tap
(``quotaglance --claude-statusline``) saves ``rate_limits`` to a small cache
file and then runs the user's original command, so their status line keeps
working exactly as before.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from quotaglance import paths

TAP_FLAG = "--claude-statusline"


def snapshot_path() -> Path:
    return paths.cache_dir() / "claude-statusline.json"


def chain_path() -> Path:
    return paths.config_dir() / "claude-statusline.json"


def claude_config_dir(env: dict[str, str] | None = None) -> Path:
    env = env if env is not None else dict(os.environ)
    custom = env.get("CLAUDE_CONFIG_DIR")
    return Path(custom).expanduser() if custom else Path.home() / ".claude"


def settings_path(env: dict[str, str] | None = None) -> Path:
    return claude_config_dir(env) / "settings.json"


# -- the tap -------------------------------------------------------------------------


def _default_line(data: dict[str, Any]) -> str:
    model = ((data.get("model") or {}).get("display_name") or "Claude").strip()
    limits = data.get("rate_limits") or {}
    parts = [model]
    for key, label in (("five_hour", "5h"), ("seven_day", "week")):
        pct = (limits.get(key) or {}).get("used_percentage")
        if isinstance(pct, (int, float)):
            parts.append(f"{label} {round(pct)}%")
    return " · ".join(parts)


def run_tap(raw: bytes) -> str:
    """Record ``rate_limits`` and return what the status line should print."""
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        data = {}
    if isinstance(data, dict) and isinstance(data.get("rate_limits"), dict):
        record = {
            "observed_at": time.time(),
            "rate_limits": data["rate_limits"],
            "model": (data.get("model") or {}).get("id"),
            "version": data.get("version"),
            "session_id": data.get("session_id"),
        }
        try:
            paths.atomic_write_text(snapshot_path(), json.dumps(record), mode=0o600)
        except OSError:
            pass  # never break the user's status line
    previous = None
    try:
        previous = json.loads(chain_path().read_text(encoding="utf-8")).get("previous")
    except (OSError, ValueError, AttributeError):
        previous = None
    command = previous.get("command") if isinstance(previous, dict) else None
    if command:
        try:
            result = subprocess.run(command, shell=True, input=raw, capture_output=True,
                                    timeout=2.5, check=False)
            return result.stdout.decode("utf-8", errors="replace").rstrip("\n")
        except (OSError, subprocess.TimeoutExpired):
            return ""
    return _default_line(data) if isinstance(data, dict) else ""


def tap_main() -> int:
    output = run_tap(sys.stdin.buffer.read())
    if output:
        sys.stdout.write(output + "\n")
    return 0


# -- install / uninstall (explicit user action from Preferences) ---------------------


def _read_settings(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("settings.json is not a JSON object")
    return data


def is_installed(path: Path | None = None) -> bool:
    path = path or settings_path()
    try:
        command = (_read_settings(path).get("statusLine") or {}).get("command") or ""
    except (OSError, ValueError, AttributeError):
        return False
    return TAP_FLAG in command


def install(tap_command: list[str], path: Path | None = None) -> None:
    """Point Claude Code's status line at the tap, keeping the old one chained."""
    path = path or settings_path()
    settings = _read_settings(path)
    current = settings.get("statusLine")
    if not (isinstance(current, dict) and TAP_FLAG in str(current.get("command", ""))):
        paths.atomic_write_text(chain_path(), json.dumps({"previous": current}), mode=0o600)
        if path.exists():
            backup = path.with_name(path.name + ".quotaglance-backup")
            if not backup.exists():
                backup.write_bytes(path.read_bytes())
    command = " ".join(shlex.quote(part) for part in [*tap_command, TAP_FLAG])
    settings["statusLine"] = {"type": "command", "command": command, "padding": 0}
    paths.atomic_write_text(path, json.dumps(settings, indent=2) + "\n", mode=0o644)


def uninstall(path: Path | None = None) -> None:
    """Restore the status line that was there before."""
    path = path or settings_path()
    settings = _read_settings(path)
    try:
        previous = json.loads(chain_path().read_text(encoding="utf-8")).get("previous")
    except (OSError, ValueError, AttributeError):
        previous = None
    if previous:
        settings["statusLine"] = previous
    else:
        settings.pop("statusLine", None)
    paths.atomic_write_text(path, json.dumps(settings, indent=2) + "\n", mode=0o644)
    try:
        chain_path().unlink()
    except FileNotFoundError:
        pass
