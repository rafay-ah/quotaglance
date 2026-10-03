"""StatusNotifierItem (a.k.a. AppIndicator) implemented directly over D-Bus.

GTK 4 has no tray API, and libayatana-appindicator is GTK 3 only, so the
protocol is implemented here with Gio. Ubuntu ships the AppIndicator Shell
extension enabled by default, so this gives a working top-bar meter out of
the box; the richer QuotaGlance Shell extension takes over when enabled.
"""

from __future__ import annotations

import logging

import gi

gi.require_version("Gio", "2.0")

from gi.repository import Gio, GLib  # noqa: E402

from quotaglance import APP_ID, APP_NAME  # noqa: E402
from quotaglance.gui.tray.dbusmenu import DBusMenu, MenuItem  # noqa: E402
from quotaglance.gui.tray.icon import ring_pixmaps  # noqa: E402
from quotaglance.models import Severity  # noqa: E402

log = logging.getLogger(__name__)

ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
INTERFACE = "org.kde.StatusNotifierItem"

INTROSPECTION = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionMovieName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <property name="XAyatanaLabel" type="s" access="read"/>
    <property name="XAyatanaLabelGuide" type="s" access="read"/>
    <property name="XAyatanaOrderingIndex" type="u" access="read"/>
    <method name="ContextMenu">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Activate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Scroll">
      <arg name="delta" type="i" direction="in"/>
      <arg name="orientation" type="s" direction="in"/>
    </method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus">
      <arg name="status" type="s"/>
    </signal>
    <signal name="NewMenu"/>
    <signal name="XAyatanaNewLabel">
      <arg name="label" type="s"/>
      <arg name="guide" type="s"/>
    </signal>
  </interface>
</node>
"""


class StatusNotifierItem:
    def __init__(self, connection: Gio.DBusConnection, on_activate) -> None:
        self.connection = connection
        self.on_activate = on_activate
        self.status = "Active"
        self.label = ""
        self.tooltip = (APP_NAME, "")
        self._pixmaps = ring_pixmaps(None, Severity.NORMAL)
        self._icon_key: tuple | None = None
        self.menu = DBusMenu(connection, MENU_PATH)
        node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION)
        self._registration = connection.register_object(
            ITEM_PATH, node.interfaces[0], self._on_call, self._on_get_property, None)
        self._watch = Gio.bus_watch_name_on_connection(
            connection, WATCHER_NAME, Gio.BusNameWatcherFlags.NONE,
            self._on_watcher_appeared, None)
        self.registered = False

    # -- lifecycle -------------------------------------------------------------------

    def _on_watcher_appeared(self, connection, _name, _owner) -> None:
        connection.call(
            WATCHER_NAME, WATCHER_PATH, WATCHER_NAME, "RegisterStatusNotifierItem",
            GLib.Variant("(s)", (connection.get_unique_name(),)), None,
            Gio.DBusCallFlags.NONE, 3000, None, self._on_registered)

    def _on_registered(self, connection, result) -> None:
        try:
            connection.call_finish(result)
            self.registered = True
        except GLib.Error as exc:
            log.info("Tray host refused registration: %s", exc.message)

    def destroy(self) -> None:
        Gio.bus_unwatch_name(self._watch)
        if self._registration:
            self.connection.unregister_object(self._registration)
            self._registration = 0
        self.menu.unregister()

    # -- updates --------------------------------------------------------------------

    def _emit(self, signal: str, params: GLib.Variant | None = None) -> None:
        try:
            self.connection.emit_signal(None, ITEM_PATH, INTERFACE, signal, params)
        except GLib.Error as exc:
            log.debug("SNI %s failed: %s", signal, exc.message)

    def set_visible(self, visible: bool) -> None:
        status = "Active" if visible else "Passive"
        if status != self.status:
            self.status = status
            self._emit("NewStatus", GLib.Variant("(s)", (status,)))

    def update(self, fraction: float | None, severity: Severity, label: str,
               tooltip_title: str, tooltip_body: str, items: list[MenuItem]) -> None:
        key = (None if fraction is None else round(fraction, 2), int(severity))
        if key != self._icon_key:
            self._icon_key = key
            self._pixmaps = ring_pixmaps(fraction, severity)
            self._emit("NewIcon")
        if label != self.label:
            self.label = label
            self._emit("XAyatanaNewLabel", GLib.Variant("(ss)", (label, "100%")))
        if (tooltip_title, tooltip_body) != self.tooltip:
            self.tooltip = (tooltip_title, tooltip_body)
            self._emit("NewToolTip")
        self.menu.set_items(items)

    # -- D-Bus handlers ---------------------------------------------------------------------

    def _pixmap_variant(self) -> GLib.Variant:
        return GLib.Variant("a(iiay)", [(w, h, data) for w, h, data in self._pixmaps])

    def _on_get_property(self, _conn, _sender, _path, _iface, name):
        empty_pixmap = GLib.Variant("a(iiay)", [])
        values = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", APP_ID),
            "Title": GLib.Variant("s", APP_NAME),
            "Status": GLib.Variant("s", self.status),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", ""),
            "IconPixmap": self._pixmap_variant(),
            "OverlayIconName": GLib.Variant("s", ""),
            "OverlayIconPixmap": empty_pixmap,
            "AttentionIconName": GLib.Variant("s", ""),
            "AttentionIconPixmap": empty_pixmap,
            "AttentionMovieName": GLib.Variant("s", ""),
            "IconThemePath": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", ("", [], self.tooltip[0], self.tooltip[1])),
            "ItemIsMenu": GLib.Variant("b", True),
            "Menu": GLib.Variant("o", MENU_PATH),
            "XAyatanaLabel": GLib.Variant("s", self.label),
            "XAyatanaLabelGuide": GLib.Variant("s", "100%"),
            "XAyatanaOrderingIndex": GLib.Variant("u", 0),
        }
        return values.get(name)

    def _on_call(self, _conn, _sender, _path, _iface, method, _params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(lambda: (self.on_activate(), False)[1])
        invocation.return_value(None)
