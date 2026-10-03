"""Main window: an overview card for every enabled provider."""

from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

from quotaglance import APP_ID, APP_NAME  # noqa: E402
from quotaglance.gui.components import (  # noqa: E402
    Badge,
    Gauge,
    Meter,
    accent_for,
    set_severity_class,
)
from quotaglance.gui.markup import markup  # noqa: E402
from quotaglance.i18n import _  # noqa: E402
from quotaglance.models import Severity, Status  # noqa: E402
from quotaglance.presenter import ProviderView, WindowView, build_views, headline  # noqa: E402
from quotaglance.timefmt import format_ago  # noqa: E402
from quotaglance.util import utcnow  # noqa: E402


def _label(text: str = "", *classes: str, xalign: float = 0.0, **props) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=xalign, **props)
    for name in classes:
        label.add_css_class(name)
    return label


class WindowRow:
    """One meter row inside a provider card (label · bar · percent · reset)."""

    def __init__(self, grid: Gtk.Grid, row: int, groups: tuple[Gtk.SizeGroup, Gtk.SizeGroup]
                 ) -> None:
        self.grid = grid
        self.row = row
        self.groups = groups
        self.label = _label("", "qg-row-label", "caption")
        self.meter = Meter(thickness=7)
        self.percent = _label("", "qg-pct", "numeric", xalign=1.0, width_chars=4)
        self.reset = _label("", "qg-reset", "dim-label", "numeric", xalign=1.0)
        self.value = _label("", "numeric", xalign=0.0)
        self.value.set_ellipsize(Pango.EllipsizeMode.END)
        groups[0].add_widget(self.label)
        groups[1].add_widget(self.reset)
        grid.attach(self.label, 0, row, 1, 1)
        grid.attach(self.meter, 1, row, 1, 1)
        grid.attach(self.percent, 2, row, 1, 1)
        grid.attach(self.reset, 3, row, 1, 1)
        grid.attach(self.value, 1, row, 2, 1)

    def update(self, view: WindowView, color: str, animate: bool) -> None:
        self.label.set_label(view.label)
        has_meter = view.has_meter
        self.meter.set_visible(has_meter)
        self.percent.set_visible(has_meter)
        self.value.set_visible(not has_meter)
        severity = Severity(view.severity)
        if has_meter:
            self.meter.set_color(accent_for(color, severity))
            self.meter.set_fraction(view.fraction, animate)
            self.percent.set_label(view.percent_text)
            set_severity_class(self.percent, severity)
            tip = view.amount_text or ""
            self.meter.set_tooltip_text(f"{view.label}: {view.percent_text}"
                                        + (f" ({tip})" if tip else ""))
        else:
            self.value.set_label(view.detail or view.amount_text or "—")
        if view.countdown:
            self.reset.set_label(view.countdown)
            self.reset.set_tooltip_text(
                _("Resets {when}").format(when=view.reset_at_text) if view.reset_at_text else None)
        elif has_meter and view.amount_text:
            self.reset.set_label(view.amount_text)
            self.reset.set_tooltip_text(None)
        else:
            self.reset.set_label("")
            self.reset.set_tooltip_text(None)

    def destroy(self) -> None:
        self.groups[0].remove_widget(self.label)
        self.groups[1].remove_widget(self.reset)
        for widget in (self.label, self.meter, self.percent, self.reset, self.value):
            self.grid.remove(widget)


class ProviderCard(Gtk.Box):
    __gtype_name__ = "QgProviderCard"

    def __init__(self, provider, on_setup, groups: tuple[Gtk.SizeGroup, Gtk.SizeGroup]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.provider = provider
        self.groups = groups
        self.add_css_class("card")
        self.add_css_class("qg-card")
        self._rows: dict[str, WindowRow] = {}
        self._row_order: list[str] = []

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header.append(Badge(provider, "md"))
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                         valign=Gtk.Align.CENTER)
        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.title = _label(provider.name, "qg-card-title")
        self.plan = _label("", "qg-plan", "caption")
        title_row.append(self.title)
        title_row.append(self.plan)
        titles.append(title_row)
        self.meta = _label("", "caption", "dim-label")
        self.meta.set_ellipsize(Pango.EllipsizeMode.END)
        titles.append(self.meta)
        header.append(titles)

        self.side = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                              valign=Gtk.Align.CENTER, hhomogeneous=False)
        self.spinner = Gtk.Spinner()
        self.side.add_named(self.spinner, "spinner")
        self.peak = _label("", "qg-peak", "numeric", xalign=1.0)
        self.side.add_named(self.peak, "peak")
        self.alert_icon = Gtk.Image(icon_name="dialog-warning-symbolic")
        self.alert_icon.add_css_class("qg-severity-warning")
        self.side.add_named(self.alert_icon, "alert")
        self.side.add_named(Gtk.Box(), "none")
        header.append(self.side)
        self.append(header)

        self.grid = Gtk.Grid(column_spacing=12, row_spacing=9)
        self.grid.add_css_class("qg-grid")
        self.append(self.grid)

        self.note_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.note_icon = Gtk.Image(icon_name="dialog-information-symbolic",
                                   valign=Gtk.Align.START)
        self.note_icon.add_css_class("dim-label")
        self.note = _label("", "qg-note", wrap=True, hexpand=True)
        self.note.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.note.set_selectable(True)
        self.note_box.append(self.note_icon)
        self.note_box.append(self.note)
        self.append(self.note_box)

        self.setup_button = Gtk.Button(label=_("Set Up…"), halign=Gtk.Align.START)
        self.setup_button.add_css_class("pill")
        self.setup_button.connect("clicked", lambda *_a: on_setup(provider.id))
        self.append(self.setup_button)

    def update(self, view: ProviderView, animate: bool = True) -> None:
        self.plan.set_label(view.plan or "")
        self.plan.set_visible(bool(view.plan))
        self.meta.set_label(view.meta_text)
        self.meta.set_visible(bool(view.meta_text))
        if view.account:
            self.title.set_tooltip_text(view.account)

        # Rows: reuse widgets by window id so meters animate between values.
        wanted = [w.id for w in view.windows]
        if wanted != self._row_order:
            for row in self._rows.values():
                row.destroy()
            self._rows = {wid: WindowRow(self.grid, i, self.groups)
                          for i, wid in enumerate(wanted)}
            self._row_order = wanted
            animate = False
        for window in view.windows:
            self._rows[window.id].update(window, view.color, animate)
        self.grid.set_visible(bool(view.windows))

        if view.loading and not view.windows:
            self.side.set_visible_child_name("spinner")
            self.spinner.start()
        else:
            self.spinner.stop()
            if view.status == Status.ERROR.value:
                self.side.set_visible_child_name("alert")
            elif view.peak is not None:
                self.peak.set_label(view.peak.percent_text)
                set_severity_class(self.peak, Severity(view.peak.severity))
                self.side.set_visible_child_name("peak")
            else:
                self.side.set_visible_child_name("none")

        stale = view.is_stale
        if stale:
            self.add_css_class("qg-stale")
        else:
            self.remove_css_class("qg-stale")

        note = None
        icon = "dialog-information-symbolic"
        if view.status == Status.NOT_CONFIGURED.value:
            note = view.message or _("Not set up")
            if view.hint:
                note += "\n" + view.hint
        elif view.status in (Status.ERROR.value, Status.STALE.value) and view.message:
            note = view.message + (f"\n{view.hint}" if view.hint else "")
            icon = "dialog-warning-symbolic"
        self.note.set_markup(markup(note))
        self.note_icon.set_from_icon_name(icon)
        self.note_box.set_visible(bool(note))
        self.setup_button.set_visible(view.status == Status.NOT_CONFIGURED.value)


class HeadlineCard(Gtk.Box):
    """Big gauge for the single most constrained quota."""

    __gtype_name__ = "QgHeadlineCard"

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=18)
        self.add_css_class("card")
        self.add_css_class("qg-headline")
        meter = Gauge(84, 9)
        self.ring, self.pct = meter.ring, meter.label
        self.pct.add_css_class("title-3")
        self.append(meter.overlay)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3,
                        valign=Gtk.Align.CENTER, hexpand=True)
        self.caption = _label(_("Closest to its limit"), "caption", "dim-label")
        self.title = _label("", "qg-headline-title", "title-4")
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        self.subtitle = _label("", "dim-label")
        self.subtitle.set_ellipsize(Pango.EllipsizeMode.END)
        texts.append(self.caption)
        texts.append(self.title)
        texts.append(self.subtitle)
        self.append(texts)

    def update(self, head, animate: bool) -> None:
        window = head.window
        severity = Severity(window.severity)
        self.ring.set_color(accent_for(head.color, severity))
        self.ring.set_fraction(window.fraction, animate)
        self.pct.set_label(window.percent_text)
        self.title.set_label(f"{head.provider_name} · {window.label}")
        parts = [p for p in (window.reset_text, window.amount_text) if p]
        self.subtitle.set_label(" · ".join(parts) if parts else _("No reset scheduled"))


class MainWindow(Adw.ApplicationWindow):
    __gtype_name__ = "QgMainWindow"

    def __init__(self, app) -> None:
        super().__init__(application=app, title=APP_NAME)
        self.app = app
        self.set_default_size(460, 720)
        self.set_size_request(360, 420)
        self._cards: dict[str, ProviderCard] = {}
        self._mapped_once = False
        self._groups = (Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL),
                        Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL))

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title_widget = Adw.WindowTitle(title=APP_NAME, subtitle="")
        header.set_title_widget(self.title_widget)

        self.refresh_button = Gtk.Button(icon_name="view-refresh-symbolic",
                                         tooltip_text=_("Refresh Now"),
                                         action_name="app.refresh")
        self.refresh_spinner = Gtk.Spinner()
        self.refresh_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.refresh_stack.add_named(self.refresh_button, "button")
        self.refresh_stack.add_named(self.refresh_spinner, "spinner")
        header.pack_start(self.refresh_stack)

        menu = Gio.Menu()
        section = Gio.Menu()
        section.append(_("Desktop Widget"), "app.widget")
        section.append(_("Preferences"), "app.preferences")
        menu.append_section(None, section)
        about = Gio.Menu()
        about.append(_("About {name}").format(name=APP_NAME), "app.about")
        about.append(_("Quit"), "app.quit")
        menu.append_section(None, about)
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                     tooltip_text=_("Main Menu"), primary=True)
        header.pack_end(menu_button)
        widget_button = Gtk.ToggleButton(icon_name="view-pin-symbolic",
                                         tooltip_text=_("Desktop Widget"),
                                         action_name="app.widget")
        header.pack_end(widget_button)
        toolbar.add_top_bar(header)

        self.banner = Adw.Banner(title=_("Demo mode: showing sample data"),
                                 button_label=_("Exit Demo"))
        self.banner.connect("button-clicked", lambda *_a: app.set_demo(False))
        toolbar.add_top_bar(self.banner)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        empty = Adw.StatusPage(
            icon_name=f"{APP_ID}-symbolic",
            title=_("No Providers Yet"),
            description=_("QuotaGlance didn't find any signed-in AI tools on this computer. "
                          "Choose the providers you use to start tracking their limits."),
        )
        empty_actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                                halign=Gtk.Align.CENTER)
        choose = Gtk.Button(label=_("Choose Providers…"), action_name="app.providers")
        choose.add_css_class("pill")
        choose.add_css_class("suggested-action")
        demo = Gtk.Button(label=_("Try Demo Mode"), action_name="app.demo")
        demo.add_css_class("pill")
        empty_actions.append(choose)
        empty_actions.append(demo)
        empty.set_child(empty_actions)
        self.stack.add_named(empty, "empty")

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        clamp = Adw.Clamp(maximum_size=620, tightening_threshold=440)
        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                                margin_start=12, margin_end=12, margin_top=6)
        self.list_box.add_css_class("qg-list")
        self.headline = HeadlineCard()
        self.list_box.append(self.headline)
        clamp.set_child(self.list_box)
        scroller.set_child(clamp)
        self.stack.add_named(scroller, "list")

        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(self.stack)
        toolbar.set_content(self.toasts)
        self.set_content(toolbar)

        self._handlers = [
            app.connect("data-changed", lambda *_a: self.refresh_view()),
            app.connect("tick", lambda *_a: self.refresh_view(animate=False)),
        ]
        self.connect("close-request", self._on_close)
        self.connect("map", self._on_map)
        self.refresh_view(animate=False)

    def _on_map(self, *_args) -> None:
        if not self._mapped_once:
            self._mapped_once = True
            GLib.idle_add(lambda: (self.refresh_view(animate=True), False)[1])

    def _on_close(self, *_args) -> bool:
        for handler in self._handlers:
            self.app.disconnect(handler)
        self._handlers = []
        return False

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))

    def refresh_view(self, animate: bool = True) -> None:
        engine = self.app.engine
        now = utcnow()
        views = build_views(engine, now)
        refreshing = bool(engine.in_flight)
        self.refresh_stack.set_visible_child_name("spinner" if refreshing else "button")
        if refreshing:
            self.refresh_spinner.start()
        else:
            self.refresh_spinner.stop()
        updated = engine.last_updated()
        subtitle = _("Updated {ago}").format(ago=format_ago(updated, now)) if updated else ""
        if refreshing and not updated:
            subtitle = _("Updating…")
        self.title_widget.set_subtitle(subtitle)
        # Screenshot tooling hides the demo banner (QUOTAGLANCE_HIDE_DEMO_BANNER=1).
        self.banner.set_revealed(engine.demo and not os.environ.get("QUOTAGLANCE_HIDE_DEMO_BANNER"))

        if not views:
            self.stack.set_visible_child_name("empty")
            return
        self.stack.set_visible_child_name("list")

        head = headline(views)
        self.headline.set_visible(head is not None)
        if head is not None:
            self.headline.update(head, animate and self._mapped_once)

        wanted = [v.id for v in views]
        for pid in list(self._cards):
            if pid not in wanted:
                self.list_box.remove(self._cards.pop(pid))
        previous = self.headline
        for view in views:
            card = self._cards.get(view.id)
            if card is None:
                provider = engine.providers[view.id]
                card = ProviderCard(provider, self.app.open_provider_settings, self._groups)
                self._cards[view.id] = card
                self.list_box.insert_child_after(card, previous)
            elif card.get_prev_sibling() is not previous:
                self.list_box.reorder_child_after(card, previous)
            card.update(view, animate and self._mapped_once)
            previous = card
