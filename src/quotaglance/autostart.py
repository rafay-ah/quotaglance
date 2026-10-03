"""Start-on-login via an XDG autostart entry (works on GNOME, KDE, XFCE...)."""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from pathlib import Path

from quotaglance import APP_ID, APP_NAME, paths

AUTOSTART_NAME = f"{APP_ID}.desktop"


def autostart_path() -> Path:
    return paths.config_home() / "autostart" / AUTOSTART_NAME


def launch_command() -> list[str]:
    """The command that starts this installation of QuotaGlance."""
    appimage = os.environ.get("APPIMAGE")
    if appimage and os.path.isfile(appimage):
        return [appimage]
    installed = shutil.which("quotaglance")
    if installed:
        return [installed]
    # Running from a source checkout: python -m quotaglance with the right path.
    package_root = Path(__file__).resolve().parent.parent
    return ["env", f"PYTHONPATH={package_root}", sys.executable, "-m", "quotaglance"]


def desktop_entry(command: list[str]) -> str:
    exec_line = " ".join(shlex.quote(part) for part in [*command, "--background"])
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        f"Name={APP_NAME}",
        "Comment=Keep an eye on your AI coding quotas",
        f"Exec={exec_line}",
        f"Icon={APP_ID}",
        "Terminal=false",
        "NoDisplay=false",
        "X-GNOME-Autostart-enabled=true",
        # Give the Shell (and its tray host) a moment to come up first.
        "X-GNOME-Autostart-Delay=3",
        "",
    ])


def is_enabled() -> bool:
    path = autostart_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    lowered = text.lower()
    return "hidden=true" not in lowered and "x-gnome-autostart-enabled=false" not in lowered


def set_enabled(enabled: bool, command: list[str] | None = None) -> None:
    path = autostart_path()
    if enabled:
        paths.atomic_write_text(path, desktop_entry(command or launch_command()), mode=0o644)
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def sync(enabled: bool) -> None:
    """Make the autostart file match the setting (also refreshes a moved AppImage path)."""
    if enabled:
        command = launch_command()
        wanted = desktop_entry(command)
        try:
            current = autostart_path().read_text(encoding="utf-8")
        except OSError:
            current = ""
        if current != wanted:
            set_enabled(True, command)
    elif autostart_path().exists():
        set_enabled(False)
