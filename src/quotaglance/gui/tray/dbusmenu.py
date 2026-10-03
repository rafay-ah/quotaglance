"""Minimal ``com.canonical.dbusmenu`` server for the tray icon's menu."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import gi

gi.require_version("Gio", "2.0")

from gi.repository import Gio, GLib  # noqa: E402

log = logging.getLogger(__name__)

INTERFACE = "com.canonical.dbusmenu"

INTROSPECTION = """
<node>
  <interface name="com.canonical.dbusmenu">
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/>
      <arg type="a(ias)" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg type="u" name="revision"/>
      <arg type="i" name="parent"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg type="i" name="id"/>
      <arg type="u" name="timestamp"/>
    </signal>
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
  </interface>
</node>
"""


@dataclass
class MenuItem:
    label: str = ""
    callback: Callable[[], None] | None = None
    enabled: bool = True
    separator: bool = False
    checkmark: bool | None = None  # None: not a toggle item
    icon_name: str | None = None
    children: list[MenuItem] = field(default_factory=list)

    def signature(self) -> tuple:
        return (self.label, self.enabled, self.separator, self.checkmark, self.icon_name,
                tuple(c.signature() for c in self.children))


class DBusMenu:
    def __init__(self, connection: Gio.DBusConnection, path: str = "/MenuBar") -> None:
        self.connection = connection
        self.path = path
        self.revision = 1
        self._items: dict[int, MenuItem] = {}
        self._children: dict[int, list[int]] = {0: []}
        self._signature: tuple = ()
        node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION)
        self._registration = connection.register_object(
            path, node.interfaces[0], self._on_call, self._on_get_property, None)

    def unregister(self) -> None:
        if self._registration:
            self.connection.unregister_object(self._registration)
            self._registration = 0

    def set_items(self, items: list[MenuItem]) -> None:
        signature = tuple(i.signature() for i in items)
        if signature == self._signature:
            return
        self._signature = signature
        self._items = {}
        self._children = {0: []}
        next_id = 1

        def add(item: MenuItem, parent: int) -> None:
            nonlocal next_id
            item_id = next_id
            next_id += 1
            self._items[item_id] = item
            self._children.setdefault(parent, []).append(item_id)
            self._children[item_id] = []
            for child in item.children:
                add(child, item_id)

        for item in items:
            add(item, 0)
        self.revision += 1
        try:
            self.connection.emit_signal(None, self.path, INTERFACE, "LayoutUpdated",
                                        GLib.Variant("(ui)", (self.revision, 0)))
        except GLib.Error as exc:
            log.debug("LayoutUpdated failed: %s", exc.message)

    # -- serialisation ------------------------------------------------------------

    def _props(self, item_id: int) -> dict[str, GLib.Variant]:
        if item_id == 0:
            return {"children-display": GLib.Variant("s", "submenu")}
        item = self._items[item_id]
        if item.separator:
            return {"type": GLib.Variant("s", "separator")}
        props = {
            "label": GLib.Variant("s", item.label.replace("_", "__")),
            "enabled": GLib.Variant("b", item.enabled),
            "visible": GLib.Variant("b", True),
        }
        if item.icon_name:
            props["icon-name"] = GLib.Variant("s", item.icon_name)
        if item.checkmark is not None:
            props["toggle-type"] = GLib.Variant("s", "checkmark")
            props["toggle-state"] = GLib.Variant("i", 1 if item.checkmark else 0)
        if self._children.get(item_id):
            props["children-display"] = GLib.Variant("s", "submenu")
        return props

    def _layout(self, item_id: int, depth: int) -> GLib.Variant:
        children = []
        if depth != 0:
            for child in self._children.get(item_id, []):
                children.append(self._layout(child, depth - 1 if depth > 0 else -1))
        return GLib.Variant("(ia{sv}av)", (item_id, self._props(item_id), children))

    # -- D-Bus handlers -----------------------------------------------------------------

    def _on_get_property(self, _conn, _sender, _path, _iface, name):
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", []),
        }.get(name)

    def _on_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        try:
            if method == "GetLayout":
                parent, depth, _names = params.unpack()
                if parent != 0 and parent not in self._items:
                    parent = 0
                layout = self._layout(parent, depth)
                invocation.return_value(GLib.Variant.new_tuple(
                    GLib.Variant("u", self.revision), layout))
            elif method == "GetGroupProperties":
                ids, _names = params.unpack()
                wanted = ids or [0, *self._items]
                result = [(i, self._props(i)) for i in wanted if i == 0 or i in self._items]
                invocation.return_value(GLib.Variant("(a(ia{sv}))", (result,)))
            elif method == "GetProperty":
                item_id, name = params.unpack()
                value = self._props(item_id).get(name) if (
                    item_id == 0 or item_id in self._items) else None
                invocation.return_value(GLib.Variant("(v)", (value or GLib.Variant("s", ""),)))
            elif method == "Event":
                item_id, event, _data, _ts = params.unpack()
                if event == "clicked":
                    self._activate(item_id)
                invocation.return_value(None)
            elif method == "EventGroup":
                (events,) = params.unpack()
                errors = []
                for item_id, event, _data, _ts in events:
                    if item_id not in self._items:
                        errors.append(item_id)
                    elif event == "clicked":
                        self._activate(item_id)
                invocation.return_value(GLib.Variant("(ai)", (errors,)))
            elif method == "AboutToShow":
                invocation.return_value(GLib.Variant("(b)", (False,)))
            elif method == "AboutToShowGroup":
                invocation.return_value(GLib.Variant("(aiai)", ([], [])))
            else:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)
        except Exception as exc:
            log.exception("dbusmenu %s failed", method)
            invocation.return_dbus_error("org.freedesktop.DBus.Error.Failed", str(exc))

    def _activate(self, item_id: int) -> None:
        item = self._items.get(item_id)
        if item and item.callback and item.enabled:
            GLib.idle_add(lambda: (item.callback(), False)[1])
