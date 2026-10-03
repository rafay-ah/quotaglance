"""The desktop widget: a small, glanceable card in three sizes.

On Wayland an app cannot position or raise its own windows, so "pinning"
(keep on top, show on every workspace, remember position) is carried out
by the QuotaGlance GNOME Shell extension, which recognises this window by
its title. Without the extension the widget is a regular small window.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

from quotaglance.gui.components import (  # noqa: E402
    Badge,
    Gauge,
    Meter,
    accent_for,
    set_severity_class,
)
from quotaglance.i18n import _  # noqa: E402
from quotaglance.models import Severity, Status  # noqa: E402
from quotaglance.presenter import ProviderView, build_views, headline  # noqa: E402
from quotaglance.timefmt import format_ago  # noqa: E402
from quotaglance.util import utcnow  # noqa: E402

WIDGET_TITLE = "QuotaGlance Widget"

SIZES = {
    "small": (170, 170),
    "medium": (364, 170),
    "large": (364, 372),
}

TINT_WHITE = "#ffffff"


def _label(text: str = "", *classes: str, xalign: float = 0.0, **props) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=xalign, **props)
    for name in classes:
        label.add_css_class(name)
    return label


class DesktopWidget(Adw.Window):
    __gtype_name__ = "QgDesktopWidget"

    def __init__(self, app) -> None:
        super().__init__(application=app, title=WIDGET_TITLE, resizable=False)
        self.app = app
        self.add_css_class("qg-widget-window")
        self._size = None
        self._tint_class: str | None = None
        self._mapped_once = False

        handle = Gtk.WindowHandle()
        self.overlay = Gtk.Overlay()
        handle.set_child(self.overlay)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.body.add_css_class("qg-widget")
        self.overlay.set_child(self.body)

        # Hover-revealed pin button, top-right.
        self.pin_button = Gtk.ToggleButton(icon_name="view-pin-symbolic",
                                           tooltip_text=_("Keep on Top"),
                                           halign=Gtk.Align.END, valign=Gtk.Align.START,
                                           margin_top=8, margin_end=8)
        self.pin_button.add_css_class("circular")
        self.pin_button.add_css_class("osd")
        self.pin_button.add_css_class("qg-widget-pin")
        self.pin_button.set_action_name("app.widget-pinned")
        self.pin_revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.CROSSFADE,
                                         halign=Gtk.Align.END, valign=Gtk.Align.START,
                                         can_target=True)
        self.pin_revealer.set_child(self.pin_button)
        self.overlay.add_overlay(self.pin_revealer)
        self.set_content(handle)

        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_a: self.pin_revealer.set_reveal_child(True))
        motion.connect("leave", lambda *_a: self._maybe_hide_pin())
        self.overlay.add_controller(motion)

        click = Gtk.GestureClick(button=3)
        click.connect("pressed", self._on_context_menu)
        self.overlay.add_controller(click)
        long_press = Gtk.GestureLongPress()
        long_press.connect("pressed", lambda _g, x, y: self._show_menu(x, y))
        self.overlay.add_controller(long_press)

        menu = Gio.Menu()
        sizes = Gio.Menu()
        for value, label in (("small", _("Small")), ("medium", _("Medium")),
                             ("large", _("Large"))):
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value("app.widget-size", GLib.Variant.new_string(value))
            sizes.append_item(item)
        menu.append_section(None, sizes)
        options = Gio.Menu()
        options.append(_("Tinted Background"), "app.widget-tinted")
        options.append(_("Keep on Top"), "app.widget-pinned")
        menu.append_section(None, options)
        actions = Gio.Menu()
        actions.append(_("Refresh Now"), "app.refresh")
        actions.append(_("Open QuotaGlance"), "app.show-window")
        actions.append(_("Hide Widget"), "app.widget")
        menu.append_section(None, actions)
        self.popover = Gtk.PopoverMenu.new_from_model(menu)
        self.popover.set_parent(self.overlay)
        self.popover.set_has_arrow(False)

        self._handlers = [
            app.connect("data-changed", lambda *_a: self.refresh_view()),
            app.connect("tick", lambda *_a: self.refresh_view(animate=False)),
        ]
        self._config_handler = app.config.connect(self._on_config)
        self.connect("close-request", self._on_close)
        self.connect("map", self._on_map)
        Adw.StyleManager.get_default().connect("notify::dark",
                                               lambda *_a: self.refresh_view(animate=False))
        self.refresh_view(animate=False)

    # -- events ---------------------------------------------------------------

    def _on_map(self, *_args) -> None:
        if not self._mapped_once:
            self._mapped_once = True
            GLib.idle_add(lambda: (self.refresh_view(animate=True), False)[1])

    def _maybe_hide_pin(self) -> None:
        if not self.pin_button.get_active():
            self.pin_revealer.set_reveal_child(False)

    def _on_context_menu(self, gesture, _n, x, y) -> None:
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._show_menu(x, y)

    def _show_menu(self, x: float, y: float) -> None:
        from gi.repository import Gdk

        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        self.popover.set_pointing_to(rect)
        self.popover.popup()

    def _on_config(self, key: str) -> None:
        if key.startswith("widget.") or key.startswith("provider"):
            self.refresh_view(animate=False)

    def _on_close(self, *_args) -> bool:
        for handler in self._handlers:
            self.app.disconnect(handler)
        self._handlers = []
        self.app.config.disconnect(self._config_handler)
        self.popover.unparent()
        if self.app.config.get("widget.visible"):
            # Closing from the window manager means "hide the widget".
            self.app.config.set("widget.visible", False)
        return False

    # -- rendering --------------------------------------------------------------

    def refresh_view(self, animate: bool = True) -> None:
        config = self.app.config
        size = config.get("widget.size", "medium")
        if size not in SIZES:
            size = "medium"
        tinted = bool(config.get("widget.tinted", False))
        pinned = bool(config.get("widget.pinned", False))
        self.pin_button.set_active(pinned)
        self.pin_revealer.set_reveal_child(pinned or self.pin_revealer.get_reveal_child())

        now = utcnow()
        views = [v for v in build_views(self.app.engine, now)
                 if v.status != Status.NOT_CONFIGURED.value]
        head = headline(views)

        width, height = SIZES[size]
        self.body.set_size_request(width, height)
        self.set_default_size(width, height)

        self._apply_tint(tinted, head.provider_id if head else (views[0].id if views else None))
        for child in list(self._children(self.body)):
            self.body.remove(child)
        animate = animate and self._mapped_once
        if not views:
            self.body.append(self._empty_state())
        elif size == "small":
            self.body.append(self._small(views, head, tinted, animate))
        elif size == "medium":
            self.body.append(self._medium(views, tinted, animate))
        else:
            self.body.append(self._large(views, tinted, animate, now))

    @staticmethod
    def _children(widget: Gtk.Widget):
        child = widget.get_first_child()
        while child is not None:
            yield child
            child = child.get_next_sibling()

    def _apply_tint(self, tinted: bool, provider_id: str | None) -> None:
        if self._tint_class:
            self.remove_css_class(self._tint_class)
            self._tint_class = None
        if tinted and provider_id:
            self.add_css_class("tinted")
            self._tint_class = f"qg-p-{provider_id}"
            self.add_css_class(self._tint_class)
        else:
            self.remove_css_class("tinted")

    def _ring_color(self, color: str, severity: Severity, tinted: bool) -> str:
        if tinted and severity is Severity.NORMAL:
            return TINT_WHITE
        return accent_for(color, severity)

    def _empty_state(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                      valign=Gtk.Align.CENTER, vexpand=True)
        box.append(_label(_("No providers"), "qg-widget-title", xalign=0.5))
        box.append(_label(_("Right-click → Open QuotaGlance"), "qg-widget-caption", "dim-label",
                          xalign=0.5, wrap=True))
        return box

    def _small(self, views: list[ProviderView], head, tinted: bool, animate: bool) -> Gtk.Widget:
        view = next((v for v in views if head and v.id == head.provider_id), views[0])
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, vexpand=True)
        title = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        title.append(Badge(self.app.engine.providers[view.id], "sm"))
        name = _label(view.name, "qg-widget-title", hexpand=True)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        title.append(name)
        box.append(title)

        window = view.peak or (view.windows[0] if view.windows else None)
        meter = Gauge(88, 9, caption=True)
        overlay, ring, pct = meter.overlay, meter.ring, meter.label
        overlay.set_vexpand(True)
        overlay.set_valign(Gtk.Align.CENTER)
        if tinted:
            ring.set_track_alpha(0.28)
        pct.add_css_class("qg-widget-small-pct")
        meter.caption.set_label(window.label if window else "")
        if window is not None:
            severity = Severity(window.severity)
            ring.set_color(self._ring_color(view.color, severity, tinted))
            ring.set_fraction(window.fraction, animate)
            pct.set_label(window.percent_text if window.percent is not None else "—")
            if not tinted:
                set_severity_class(pct, severity)
        box.append(overlay)
        reset = window.reset_text if window and window.reset_text else (
            window.amount_text if window else "")
        box.append(_label(reset or "", "qg-widget-caption", "dim-label", xalign=0.5))
        return box

    def _medium(self, views: list[ProviderView], tinted: bool, animate: bool) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4, homogeneous=True,
                      vexpand=True)
        for view in views[:3]:
            column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                             valign=Gtk.Align.CENTER)
            meter = Gauge(70, 7.5, caption=True)
            if tinted:
                meter.ring.set_track_alpha(0.28)
            meter.label.add_css_class("qg-widget-ring-pct")
            window = view.peak or (view.windows[0] if view.windows else None)
            if window is not None:
                severity = Severity(window.severity)
                meter.ring.set_color(self._ring_color(view.color, severity, tinted))
                meter.ring.set_fraction(window.fraction, animate)
                meter.label.set_label(window.percent_text if window.percent is not None
                                      else "—")
                meter.caption.set_label(window.label)
                if not tinted:
                    set_severity_class(meter.label, severity)
            column.append(meter.overlay)
            name = _label(view.name, "qg-widget-name", xalign=0.5, margin_top=4)
            name.set_ellipsize(Pango.EllipsizeMode.END)
            name.set_max_width_chars(13)
            column.append(name)
            detail = ""
            if window is not None:
                detail = (_("in {countdown}").format(countdown=window.countdown)
                          if window.countdown else (window.amount_text or ""))
            sub = _label(detail, "qg-widget-caption", "dim-label", "numeric", xalign=0.5)
            sub.set_ellipsize(Pango.EllipsizeMode.END)
            sub.set_max_width_chars(14)
            column.append(sub)
            row.append(column)
        return row

    def _large(self, views: list[ProviderView], tinted: bool, animate: bool, now) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, vexpand=True)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.append(_label(_("AI Quotas"), "qg-widget-title", hexpand=True))
        updated = self.app.engine.last_updated()
        header.append(_label(format_ago(updated, now) if updated else "", "qg-widget-caption",
                             "dim-label", xalign=1.0))
        box.append(header)
        budget = 5
        label_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        for view in views:
            if budget <= 0:
                break
            windows = view.windows[:2]
            if not windows:
                continue
            budget -= 1
            entry = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            badge = Badge(self.app.engine.providers[view.id], "sm")
            badge.set_valign(Gtk.Align.START)
            badge.set_margin_top(1)
            entry.append(badge)
            grid = Gtk.Grid(column_spacing=8, row_spacing=4, hexpand=True)
            name = _label(view.name, "qg-widget-name")
            name.set_ellipsize(Pango.EllipsizeMode.END)
            grid.attach(name, 0, 0, 3, 1)
            if view.plan:
                grid.attach(_label(view.plan, "qg-widget-caption", "dim-label", xalign=1.0),
                            3, 0, 1, 1)
            for index, window in enumerate(windows, start=1):
                row_label = _label(window.label, "qg-widget-row-label", "dim-label",
                                   max_width_chars=11, ellipsize=Pango.EllipsizeMode.END)
                label_group.add_widget(row_label)
                grid.attach(row_label, 0, index, 1, 1)
                if window.percent is not None:
                    meter = Meter(thickness=6)
                    severity = Severity(window.severity)
                    meter.set_color(self._ring_color(view.color, severity, tinted))
                    meter.set_fraction(window.fraction, animate)
                    grid.attach(meter, 1, index, 1, 1)
                    pct = _label(window.percent_text, "qg-widget-row-label", "numeric",
                                 xalign=1.0, width_chars=4)
                    if not tinted:
                        set_severity_class(pct, severity)
                    grid.attach(pct, 2, index, 1, 1)
                    grid.attach(_label(window.countdown or "", "qg-widget-caption",
                                       "dim-label", "numeric", xalign=1.0, width_chars=6),
                                3, index, 1, 1)
                else:
                    grid.attach(_label(window.amount_text or window.detail or "—",
                                       "qg-widget-row-label", "numeric", hexpand=True),
                                1, index, 3, 1)
            entry.append(grid)
            box.append(entry)
        return box
