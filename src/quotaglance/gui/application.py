"""The QuotaGlance application: a background service with optional windows.

The app keeps running when its windows are closed so the top-bar
indicator, desktop widget and notifications stay live. It stops only when
the user picks Quit (window menu, tray menu or ``quotaglance --quit``).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from quotaglance import APP_ID, APP_NAME, HOMEPAGE, __version__, autostart  # noqa: E402
from quotaglance.config import Config  # noqa: E402
from quotaglance.engine import Engine  # noqa: E402
from quotaglance.gui.components import provider_css  # noqa: E402
from quotaglance.gui.dbus_service import DBusService  # noqa: E402
from quotaglance.gui.notifier import Notifier  # noqa: E402
from quotaglance.gui.shell import PANEL_BUS_NAME, ShellIntegration  # noqa: E402
from quotaglance.i18n import _  # noqa: E402
from quotaglance.models import Severity  # noqa: E402
from quotaglance.presenter import build_views, dbus_payload, headline  # noqa: E402
from quotaglance.secretstore import KeyringSecretStore  # noqa: E402
from quotaglance.timefmt import format_ago  # noqa: E402
from quotaglance.util import utcnow  # noqa: E402

log = logging.getLogger(__name__)

TICK_SECONDS = 15


class QuotaGlanceApp(Adw.Application):
    __gtype_name__ = "QgApplication"
    __gsignals__ = {
        # provider id that changed, or "" for "everything / scheduling state".
        "data-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "tick": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.add_main_option("background", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             _("Start without opening a window"), None)
        self.add_main_option("demo", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             _("Use realistic sample data"), None)
        self.add_main_option("widget", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             _("Toggle the desktop widget"), None)
        self.add_main_option("preferences", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             _("Open Preferences"), None)
        self.add_main_option("quit", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             _("Quit the running instance"), None)
        self.add_main_option("debug", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             _("Verbose logging"), None)
        self.config: Config | None = None
        self.engine: Engine | None = None
        self.dbus = DBusService(self)
        self.window = None
        self.widget = None
        self.tray = None
        self.notifier = None
        self.shell: ShellIntegration | None = None
        self._panel_attached = False
        self._panel_watch = 0
        self._held = False
        self._started_demo = False

    # -- GApplication vfuncs ---------------------------------------------------------

    def do_dbus_register(self, connection, object_path) -> bool:
        Adw.Application.do_dbus_register(self, connection, object_path)
        try:
            self.dbus.register(connection, object_path)
        except GLib.Error as exc:
            log.warning("Could not export D-Bus API: %s", exc.message)
        return True

    def do_dbus_unregister(self, connection, object_path) -> None:
        self.dbus.unregister()
        Adw.Application.do_dbus_unregister(self, connection, object_path)

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        GLib.set_application_name(APP_NAME)
        self.config = Config()
        self.secrets = KeyringSecretStore()
        demo = self._started_demo or os.environ.get("QUOTAGLANCE_DEMO", "").lower() in (
            "1", "true", "yes")
        self.engine = Engine(self.config, self.secrets, dispatch=self._dispatch, demo=demo)
        self.engine.connect(self._on_engine_changed)
        self.notifier = Notifier(self)
        self.config.connect(self._on_config_changed)
        self._load_css()
        self._setup_actions()
        connection = self.get_dbus_connection()
        self.shell = ShellIntegration(connection)
        if connection is not None:
            self._panel_watch = Gio.bus_watch_name_on_connection(
                connection, PANEL_BUS_NAME, Gio.BusNameWatcherFlags.NONE,
                lambda *_a: self.set_panel_attached(True),
                lambda *_a: self.set_panel_attached(False))
        # A background service: windows may come and go.
        self.hold()
        self._held = True
        autostart.sync(bool(self.config.get("autostart", True)))
        self.engine.start()
        GLib.timeout_add_seconds(TICK_SECONDS, self._on_tick)
        # Give the extension a moment to claim its bus name before showing a tray icon.
        GLib.timeout_add(1500, self._ensure_tray)
        if self.config.get("widget.visible"):
            GLib.idle_add(lambda: (self.show_widget(), False)[1])

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        options = command_line.get_options_dict().end().unpack()
        if options.get("debug"):
            logging.getLogger().setLevel(logging.DEBUG)
        if options.get("quit"):
            self.quit()
            return 0
        if options.get("demo") and not self.engine.demo:
            self.set_demo(True)
        if options.get("widget"):
            self.config.set("widget.visible", not self.config.get("widget.visible"))
            return 0
        if options.get("preferences"):
            self.show_preferences()
            return 0
        if options.get("background"):
            return 0
        self.activate()
        return 0

    def do_activate(self) -> None:
        self.show_window()

    def do_shutdown(self) -> None:
        if self.engine:
            self.engine.shutdown()
        if self.tray:
            self.tray.destroy()
            self.tray = None
        if self._panel_watch:
            Gio.bus_unwatch_name(self._panel_watch)
        Adw.Application.do_shutdown(self)

    # -- plumbing -----------------------------------------------------------------------------

    def _dispatch(self, fn) -> None:
        def run() -> bool:
            try:
                fn()
            except Exception:
                log.exception("Dispatched callback failed")
            return False

        GLib.idle_add(run)

    def _load_css(self) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        provider = Gtk.CssProvider()
        css_path = Path(__file__).with_name("style.css")
        provider.load_from_path(str(css_path))
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        colors = Gtk.CssProvider()
        css = provider_css(self.engine.providers.values())
        try:
            colors.load_from_string(css)  # GTK >= 4.12
        except AttributeError:
            colors.load_from_data(css, -1)
        Gtk.StyleContext.add_provider_for_display(
            display, colors, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _setup_actions(self) -> None:
        def simple(name: str, callback, accels: list[str] | None = None) -> None:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a: callback())
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)

        simple("refresh", lambda: self.engine.refresh(), ["<Control>r", "F5"])
        simple("show-window", self.show_window)
        simple("preferences", self.show_preferences, ["<Control>comma"])
        simple("providers", lambda: self.show_preferences(page="providers"))
        simple("about", self.show_about)
        simple("quit", self.quit_app, ["<Control>q"])
        simple("demo", lambda: self.set_demo(True))
        self.set_accels_for_action("window.close", ["<Control>w"])

        def stateful(name: str, key: str) -> None:
            action = Gio.SimpleAction.new_stateful(
                name, None, GLib.Variant.new_boolean(bool(self.config.get(key))))
            action.connect("activate", lambda a, _p: self.config.set(
                key, not a.get_state().get_boolean()))
            self.add_action(action)

        stateful("widget", "widget.visible")
        stateful("widget-tinted", "widget.tinted")
        stateful("widget-pinned", "widget.pinned")
        size = Gio.SimpleAction.new_stateful(
            "widget-size", GLib.VariantType.new("s"),
            GLib.Variant.new_string(self.config.get("widget.size", "medium")))
        size.connect("activate", lambda _a, p: self.config.set("widget.size", p.get_string()))
        self.add_action(size)

    def _sync_action_states(self) -> None:
        for name, key in (("widget", "widget.visible"), ("widget-tinted", "widget.tinted"),
                          ("widget-pinned", "widget.pinned")):
            action = self.lookup_action(name)
            if action is not None:
                action.set_state(GLib.Variant.new_boolean(bool(self.config.get(key))))
        size = self.lookup_action("widget-size")
        if size is not None:
            size.set_state(GLib.Variant.new_string(self.config.get("widget.size", "medium")))

    # -- reactions ------------------------------------------------------------------------------

    def _on_engine_changed(self, provider_id: str | None) -> None:
        if provider_id and self.notifier:
            self.notifier.evaluate(provider_id)
        self.emit("data-changed", provider_id or "")
        self._update_tray()
        self.dbus.changed()

    def _on_config_changed(self, key: str) -> None:
        self._sync_action_states()
        if key == "widget.visible":
            if self.config.get("widget.visible"):
                self.show_widget()
            elif self.widget is not None:
                widget, self.widget = self.widget, None
                widget.close()
        elif key == "autostart":
            autostart.sync(bool(self.config.get("autostart")))
        elif key == "refresh_minutes":
            self.engine.refresh(force=False)
        if key.startswith(("panel.", "widget.", "providers.")):
            self._update_tray()
            self.dbus.changed()

    def _on_tick(self) -> bool:
        self.engine.tick()
        self.emit("tick")
        self._update_tray()
        self.dbus.changed()
        return True

    # -- public API used by windows, tray and D-Bus ------------------------------------------------

    def dbus_payload(self) -> str:
        return dbus_payload(self.engine, self.config, utcnow())

    def show_window(self) -> None:
        if self.window is None:
            from quotaglance.gui.window import MainWindow

            self.window = MainWindow(self)
            self.window.connect("close-request", self._on_window_closed)
        self.window.present()

    def _on_window_closed(self, *_args) -> bool:
        self.window = None
        return False

    def show_widget(self) -> None:
        if self.widget is None:
            from quotaglance.gui.widget import DesktopWidget

            self.widget = DesktopWidget(self)
            self.widget.connect("close-request", self._on_widget_closed)
        self.widget.present()

    def _on_widget_closed(self, *_args) -> bool:
        self.widget = None
        return False

    def show_preferences(self, page: str | None = None, provider_id: str | None = None) -> None:
        from quotaglance.gui.preferences import PreferencesDialog

        self.show_window()
        dialog = PreferencesDialog(self, page=page, provider_id=provider_id)
        dialog.present(self.window)

    def open_provider_settings(self, provider_id: str) -> None:
        self.show_preferences(provider_id=provider_id)

    def show_about(self) -> None:
        self.show_window()
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            developer_name="rafay-ah",
            version=__version__,
            website=HOMEPAGE,
            issue_url=f"{HOMEPAGE}/issues",
            license_type=Gtk.License.MIT_X11,
            comments=_("Your AI coding quotas at a glance."),
        )
        about.add_acknowledgement_section(
            _("Inspired by"), ["CodexBar for macOS https://github.com/steipete/CodexBar"])
        about.present(self.window)

    def quit_app(self) -> None:
        if self._held:
            self._held = False
            self.release()
        for window in list(self.get_windows()):
            window.destroy()
        self.quit()

    def set_autostart(self, enabled: bool) -> None:
        self.config.set("autostart", bool(enabled))

    def set_provider_enabled(self, provider_id: str, enabled: bool) -> None:
        if self.config.provider_enabled(provider_id) == enabled:
            return
        self.config.set_provider(provider_id, "enabled", enabled)
        if enabled:
            self.engine.refresh([provider_id])
        else:
            self.engine.snapshots.pop(provider_id, None)
        self.emit("data-changed", "")
        self._update_tray()
        self.dbus.changed()

    def set_demo(self, enabled: bool) -> None:
        if self.engine is None:
            self._started_demo = enabled
            return
        if self.engine.demo == enabled:
            return
        self.engine.shutdown()
        self.engine = Engine(self.config, self.secrets, dispatch=self._dispatch, demo=enabled)
        self.engine.connect(self._on_engine_changed)
        self.notifier.reset_for_demo(enabled)
        self.engine.start()
        self.emit("data-changed", "")
        self._update_tray()
        self.dbus.changed()

    # -- top bar --------------------------------------------------------------------------

    def set_panel_attached(self, attached: bool) -> None:
        self._panel_attached = attached
        if self.tray is not None:
            self.tray.set_visible(not attached)
        elif not attached:
            self._ensure_tray()

    def indicator_state(self) -> str:
        if self._panel_attached:
            return "extension"
        state = self.shell.state() if self.shell else "unavailable"
        if state in ("not-installed", "unavailable") and self.tray is not None and \
                self.tray.registered:
            return "tray"
        return state

    def setup_shell_extension(self) -> str:
        return self.shell.setup() if self.shell else _("GNOME Shell isn't available")

    def _ensure_tray(self) -> bool:
        if self.tray is None and not self._panel_attached:
            connection = self.get_dbus_connection()
            if connection is not None:
                try:
                    from quotaglance.gui.tray.sni import StatusNotifierItem

                    self.tray = StatusNotifierItem(connection, self.show_window)
                except Exception:
                    log.exception("Could not create the tray icon")
                    self.tray = None
        self._update_tray()
        return False

    def _update_tray(self) -> None:
        if self.tray is None:
            return
        from quotaglance.gui.tray.dbusmenu import MenuItem

        now = utcnow()
        views = build_views(self.engine, now)
        mode = self.config.get("panel.mode", "highest")
        head = headline(views, None if mode == "highest" else mode)
        fraction = head.window.fraction if head else None
        severity = head.severity if head else Severity.NORMAL
        label = head.window.percent_text if head and self.config.get("panel.show_percent",
                                                                       True) else ""
        updated = self.engine.last_updated()
        items: list[MenuItem] = [
            MenuItem(_("Updated {ago}").format(ago=format_ago(updated, now)) if updated
                     else _("Waiting for data…"), enabled=False),
            MenuItem(separator=True),
        ]
        for view in views:
            title = view.name + (f"  ·  {view.plan}" if view.plan else "")
            items.append(MenuItem(title, self.show_window))
            if view.message and view.status != "ok":
                items.append(MenuItem("    " + view.message, self.show_window))
            for window in view.windows[:3]:
                if window.percent is not None:
                    filled = round(window.fraction * 10)
                    bar = "▰" * filled + "▱" * (10 - filled)
                    text = f"    {window.label}  {bar}  {window.percent_text}"
                else:
                    text = f"    {window.label}  {window.amount_text or window.detail or ''}"
                if window.countdown:
                    text += f"  ·  {window.countdown}"
                items.append(MenuItem(text, self.show_window))
        if views:
            items.append(MenuItem(separator=True))
        items += [
            MenuItem(_("Refresh Now"), lambda: self.engine.refresh()),
            MenuItem(_("Desktop Widget"), lambda: self.config.set(
                "widget.visible", not self.config.get("widget.visible")),
                checkmark=bool(self.config.get("widget.visible"))),
            MenuItem(_("Open QuotaGlance"), self.show_window),
            MenuItem(_("Preferences…"), self.show_preferences),
            MenuItem(separator=True),
            MenuItem(_("Quit"), self.quit_app),
        ]
        tooltip = head and f"{head.provider_name} · {head.window.label} {head.window.percent_text}"
        self.tray.update(fraction, severity, label, APP_NAME, tooltip or "", items)


def run(argv: list[str]) -> int:
    if "--demo" in argv:
        os.environ["QUOTAGLANCE_DEMO"] = os.environ.get("QUOTAGLANCE_DEMO") or "1"
    app = QuotaGlanceApp()
    return app.run(argv if argv else sys.argv)
