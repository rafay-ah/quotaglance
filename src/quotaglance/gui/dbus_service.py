"""Session-bus API used by the GNOME Shell extension.

Bus name:  io.github.rafay_ah.QuotaGlance  (owned by the GApplication)
Object:    /io/github/rafay_ah/QuotaGlance
Interface: io.github.rafay_ah.QuotaGlance1
"""

from __future__ import annotations

import logging

import gi

gi.require_version("Gio", "2.0")

from gi.repository import Gio, GLib  # noqa: E402

from quotaglance import __version__  # noqa: E402

log = logging.getLogger(__name__)

INTERFACE = "io.github.rafay_ah.QuotaGlance1"

INTROSPECTION = f"""
<node>
  <interface name="{INTERFACE}">
    <method name="GetSnapshot">
      <arg type="s" name="json" direction="out"/>
    </method>
    <method name="Refresh"/>
    <method name="ShowWindow"/>
    <method name="ShowPreferences"/>
    <method name="SetWidgetVisible">
      <arg type="b" name="visible" direction="in"/>
    </method>
    <method name="SetWidgetPinned">
      <arg type="b" name="pinned" direction="in"/>
    </method>
    <method name="SetWidgetPosition">
      <arg type="i" name="x" direction="in"/>
      <arg type="i" name="y" direction="in"/>
    </method>
    <method name="Quit"/>
    <signal name="Changed">
      <arg type="s" name="json"/>
    </signal>
    <property name="Version" type="s" access="read"/>
  </interface>
</node>
"""


class DBusService:
    def __init__(self, app) -> None:
        self.app = app
        self.connection: Gio.DBusConnection | None = None
        self.object_path: str | None = None
        self._registration = 0
        self._pending = 0

    def register(self, connection: Gio.DBusConnection, object_path: str) -> None:
        node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION)
        self._registration = connection.register_object(
            object_path, node.interfaces[0], self._on_method_call, self._on_get_property, None)
        self.connection = connection
        self.object_path = object_path

    def unregister(self) -> None:
        if self.connection and self._registration:
            self.connection.unregister_object(self._registration)
        self._registration = 0
        self.connection = None

    def changed(self) -> None:
        """Coalesce bursts of updates into one signal every 250 ms."""
        if self._pending or not self.connection:
            return
        self._pending = GLib.timeout_add(250, self._emit)

    def _emit(self) -> bool:
        self._pending = 0
        if self.connection and self.object_path:
            try:
                self.connection.emit_signal(None, self.object_path, INTERFACE, "Changed",
                                            GLib.Variant("(s)", (self.app.dbus_payload(),)))
            except GLib.Error as exc:
                log.debug("Could not emit Changed: %s", exc.message)
        return False

    def _on_get_property(self, _conn, _sender, _path, _iface, name):
        if name == "Version":
            return GLib.Variant("s", __version__)
        return None

    def _on_method_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        app = self.app
        try:
            if method == "GetSnapshot":
                invocation.return_value(GLib.Variant("(s)", (app.dbus_payload(),)))
                return
            if method == "Refresh":
                app.activate_action("refresh", None)
            elif method == "ShowWindow":
                app.activate_action("show-window", None)
            elif method == "ShowPreferences":
                app.activate_action("preferences", None)
            elif method == "SetWidgetVisible":
                app.config.set("widget.visible", bool(params.unpack()[0]))
            elif method == "SetWidgetPinned":
                app.config.set("widget.pinned", bool(params.unpack()[0]))
            elif method == "SetWidgetPosition":
                x, y = params.unpack()
                app.config.set("widget.position", [int(x), int(y)])
            elif method == "Quit":
                GLib.idle_add(lambda: (app.quit(), False)[1])
            invocation.return_value(None)
        except Exception as exc:
            log.exception("D-Bus call %s failed", method)
            invocation.return_dbus_error(f"{INTERFACE}.Error", str(exc))
