"""Talk to GNOME Shell: is our extension installed/enabled, and set it up."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")

from gi.repository import Gio, GLib  # noqa: E402

from quotaglance import EXTENSION_UUID, paths  # noqa: E402
from quotaglance.i18n import _  # noqa: E402

log = logging.getLogger(__name__)

SHELL_NAME = "org.gnome.Shell"
SHELL_PATH = "/org/gnome/Shell"
EXTENSIONS_IFACE = "org.gnome.Shell.Extensions"
PANEL_BUS_NAME = "io.github.rafay_ah.QuotaGlance.Panel"

STATE_ACTIVE = 1
STATE_INACTIVE = 2
STATE_ERROR = 3
STATE_OUT_OF_DATE = 4
STATE_INITIALIZED = 6


def user_extension_dir() -> Path:
    return paths.data_home() / "gnome-shell" / "extensions" / EXTENSION_UUID


def system_extension_dirs() -> list[Path]:
    dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")
    return [Path(d) / "gnome-shell" / "extensions" / EXTENSION_UUID for d in dirs if d]


def bundled_extension_dir() -> Path | None:
    """Extension sources shipped with this copy of QuotaGlance (AppImage, checkout)."""
    candidates = []
    override = os.environ.get("QUOTAGLANCE_EXTENSION_DIR")
    if override:
        candidates.append(Path(override))
    appdir = os.environ.get("APPDIR")
    if appdir:
        candidates.append(Path(appdir) / "usr/share/quotaglance/extension" / EXTENSION_UUID)
    here = Path(__file__).resolve()
    candidates.append(here.parents[3] / "extension" / EXTENSION_UUID)  # source checkout
    candidates.append(Path("/usr/share/quotaglance/extension") / EXTENSION_UUID)
    for candidate in candidates:
        if (candidate / "metadata.json").is_file():
            return candidate
    return None


class ShellIntegration:
    def __init__(self, connection: Gio.DBusConnection | None) -> None:
        self.connection = connection

    def _call(self, method: str, args: GLib.Variant | None, reply: str):
        if self.connection is None:
            return None
        try:
            result = self.connection.call_sync(
                SHELL_NAME, SHELL_PATH, EXTENSIONS_IFACE, method, args,
                GLib.VariantType.new(reply), Gio.DBusCallFlags.NO_AUTO_START, 2000, None)
        except GLib.Error as exc:
            log.debug("Shell call %s failed: %s", method, exc.message)
            return None
        return result.unpack()

    def shell_running(self) -> bool:
        if self.connection is None:
            return False
        try:
            result = self.connection.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                "NameHasOwner", GLib.Variant("(s)", (SHELL_NAME,)),
                GLib.VariantType.new("(b)"), Gio.DBusCallFlags.NONE, 1000, None)
        except GLib.Error:
            return False
        return bool(result.unpack()[0])

    def extension_info(self) -> dict:
        result = self._call("GetExtensionInfo", GLib.Variant("(s)", (EXTENSION_UUID,)),
                            "(a{sv})")
        return dict(result[0]) if result else {}

    def is_installed_on_disk(self) -> bool:
        dirs = [user_extension_dir(), *system_extension_dirs()]
        return any((d / "metadata.json").is_file() for d in dirs)

    def state(self) -> str:
        """One of: extension, disabled, error, needs-relogin, not-installed, unavailable."""
        if not self.shell_running():
            return "unavailable"
        info = self.extension_info()
        if not info:
            return "needs-relogin" if self.is_installed_on_disk() else "not-installed"
        state = int(info.get("state", 0) or 0)
        if state == STATE_ACTIVE:
            return "extension"
        if state in (STATE_ERROR, STATE_OUT_OF_DATE):
            return "error"
        return "disabled"

    def enable(self) -> bool:
        result = self._call("EnableExtension", GLib.Variant("(s)", (EXTENSION_UUID,)), "(b)")
        return bool(result and result[0])

    @staticmethod
    def _add_to_enabled_list() -> bool:
        source = Gio.SettingsSchemaSource.get_default()
        if source is None or source.lookup("org.gnome.shell", True) is None:
            return False
        settings = Gio.Settings.new("org.gnome.shell")
        enabled = list(settings.get_strv("enabled-extensions"))
        if EXTENSION_UUID not in enabled:
            enabled.append(EXTENSION_UUID)
            settings.set_strv("enabled-extensions", enabled)
        disabled = [u for u in settings.get_strv("disabled-extensions") if u != EXTENSION_UUID]
        settings.set_strv("disabled-extensions", disabled)
        return True

    def install_user_copy(self) -> bool:
        source = bundled_extension_dir()
        if source is None:
            return False
        target = user_extension_dir()
        if target.resolve() == source.resolve():
            return True
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
        return True

    def setup(self) -> str:
        """Install and/or enable the extension; return a message for the user."""
        state = self.state()
        if state == "extension":
            return _("The top bar extension is already active")
        if state == "disabled":
            if self.enable():
                return _("Top bar extension enabled")
            self._add_to_enabled_list()
            return _("Extension enabled. Log out and back in if it doesn't appear.")
        if state == "error":
            launch_extensions_app()
            return _("Opened Extensions so you can check the error")
        if state in ("not-installed", "needs-relogin"):
            if state == "not-installed" and not self.install_user_copy():
                return _("This copy of QuotaGlance doesn't include the Shell extension")
            self._add_to_enabled_list()
            return _("Installed. Log out and back in to see QuotaGlance in the top bar.")
        return _("GNOME Shell isn't running, so the top bar extension can't be used")


def launch_extensions_app() -> None:
    from quotaglance.envutil import overridden_names

    context = Gio.AppLaunchContext()
    for name, value in overridden_names().items():
        if value is None:
            context.unsetenv(name)
        else:
            context.setenv(name, value)
    for app_id in ("org.gnome.Extensions.desktop", "com.mattjakeman.ExtensionManager.desktop"):
        try:
            info = Gio.DesktopAppInfo.new(app_id)
        except TypeError:
            info = None
        if info is not None:
            try:
                info.launch([], context)
                return
            except GLib.Error:
                continue
