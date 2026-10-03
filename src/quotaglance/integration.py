"""Desktop integration for the AppImage: menu entry, icons, D-Bus activation.

A .deb installs these files system-wide. An AppImage can't, so on start-up
it writes per-user copies that point at the AppImage's current location,
which also makes notifications show the right name and icon.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
from pathlib import Path

from quotaglance import APP_ID, paths

log = logging.getLogger(__name__)


def _write_if_changed(path: Path, text: str, mode: int = 0o644) -> bool:
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    paths.atomic_write_text(path, text, mode=mode)
    return True


def desktop_entry(appimage: str, template: str) -> str:
    exec_cmd = shlex.quote(appimage)
    lines = []
    for line in template.splitlines():
        if line.startswith("Exec=quotaglance"):
            line = "Exec=" + exec_cmd + line[len("Exec=quotaglance"):]
        elif line.startswith("DBusActivatable="):
            line = "DBusActivatable=false"
        lines.append(line)
    lines.append("X-AppImage-Integrate=true")
    return "\n".join(lines) + "\n"


def integrate_appimage() -> None:
    appimage = os.environ.get("APPIMAGE")
    appdir = os.environ.get("APPDIR")
    if not appimage or not appdir or not os.path.isfile(appimage):
        return
    share = Path(appdir) / "usr" / "share"
    data = paths.data_home()
    try:
        template_path = Path(appdir) / f"{APP_ID}.desktop"
        template = template_path.read_text(encoding="utf-8")
        changed = _write_if_changed(data / "applications" / f"{APP_ID}.desktop",
                                    desktop_entry(appimage, template))
        service = (f"[D-BUS Service]\nName={APP_ID}\n"
                   f"Exec={shlex.quote(appimage)} --gapplication-service\n")
        _write_if_changed(data / "dbus-1" / "services" / f"{APP_ID}.service", service)
        for sub in ("scalable/apps", "symbolic/apps"):
            source_dir = share / "icons" / "hicolor" / sub
            for icon in source_dir.glob(f"{APP_ID}*.svg"):
                target = data / "icons" / "hicolor" / sub / icon.name
                if not target.exists() or target.read_bytes() != icon.read_bytes():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(icon, target)
        if changed:
            log.info("Installed AppImage desktop integration for %s", appimage)
    except OSError as exc:
        log.warning("AppImage desktop integration failed: %s", exc)
