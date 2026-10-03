"""Preferences: general behaviour, top bar, widget, notifications, providers."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from quotaglance.config import REFRESH_CHOICES, WIDGET_SIZES  # noqa: E402
from quotaglance.gui.components import Badge  # noqa: E402
from quotaglance.gui.markup import markup  # noqa: E402
from quotaglance.i18n import _, ngettext  # noqa: E402
from quotaglance.models import Status  # noqa: E402

CATEGORIES = (
    ("agents", _("Coding Agents")),
    ("editors", _("Editors & IDEs")),
    ("api", _("API Platforms")),
    ("media", _("Voice & Media")),
)


def esc(text: str | None) -> str:
    """libadwaita renders titles and subtitles as Pango markup."""
    return markup(text)


def _switch_row(title: str, subtitle: str, active: bool, on_change) -> Adw.SwitchRow:
    row = Adw.SwitchRow(title=esc(title), subtitle=esc(subtitle))
    row.set_active(active)
    row.connect("notify::active", lambda r, _p: on_change(r.get_active()))
    return row


def _combo_row(title: str, subtitle: str, labels: list[str], selected: int,
               on_change) -> Adw.ComboRow:
    row = Adw.ComboRow(title=esc(title), subtitle=esc(subtitle),
                       model=Gtk.StringList.new(labels))
    row.set_selected(max(0, selected))
    row.connect("notify::selected", lambda r, _p: on_change(r.get_selected()))
    return row


class PreferencesDialog(Adw.PreferencesDialog):
    __gtype_name__ = "QgPreferencesDialog"

    def __init__(self, app, page: str | None = None, provider_id: str | None = None) -> None:
        super().__init__()
        self.app = app
        self.config = app.config
        self.set_search_enabled(True)
        self._status_rows: dict[str, Adw.ActionRow] = {}
        self._expanders: dict[str, Adw.ExpanderRow] = {}
        self.add(self._general_page())
        self.add(self._providers_page())
        if page == "providers" or provider_id:
            self.set_visible_page_name("providers")
        if provider_id and provider_id in self._expanders:
            self._expanders[provider_id].set_expanded(True)
        self._handler = app.connect("data-changed", self._on_data_changed)
        self.connect("closed", lambda *_a: app.disconnect(self._handler))

    # -- General --------------------------------------------------------------

    def _general_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=_("General"), icon_name="preferences-system-symbolic",
                                   name="general")
        config = self.config

        startup = Adw.PreferencesGroup(title=esc(_("Startup & Updates")))
        startup.add(_switch_row(
            _("Start on Login"), _("Run quietly in the background after you log in"),
            bool(config.get("autostart", True)), self.app.set_autostart))
        minutes = config.get("refresh_minutes", 5)
        labels = [ngettext("{n} minute", "{n} minutes", n).format(n=n) for n in REFRESH_CHOICES]
        index = REFRESH_CHOICES.index(minutes) if minutes in REFRESH_CHOICES else 2
        startup.add(_combo_row(
            _("Check Online Sources Every"), _("Local files are read every minute"),
            labels, index, lambda i: config.set("refresh_minutes", REFRESH_CHOICES[i])))
        page.add(startup)

        panel = Adw.PreferencesGroup(title=esc(_("Top Bar")))
        self.indicator_row = Adw.ActionRow(title=esc(_("Top Bar Indicator")))
        self.indicator_button = Gtk.Button(valign=Gtk.Align.CENTER)
        self.indicator_button.connect("clicked", self._on_indicator_action)
        self.indicator_row.add_suffix(self.indicator_button)
        panel.add(self.indicator_row)
        self._update_indicator_row()

        providers = self.app.engine.enabled_providers()
        choices = ["highest", *[p.id for p in providers]]
        labels = [_("Busiest quota"), *[p.name for p in providers]]
        mode = config.get("panel.mode", "highest")
        panel.add(_combo_row(
            _("Show in Top Bar"), _("Which quota drives the meter"), labels,
            choices.index(mode) if mode in choices else 0,
            lambda i: config.set("panel.mode", choices[i])))
        panel.add(_switch_row(
            _("Show Percentage"), _("Display the number next to the meter"),
            bool(config.get("panel.show_percent", True)),
            lambda v: config.set("panel.show_percent", v)))
        page.add(panel)

        widget = Adw.PreferencesGroup(title=esc(_("Desktop Widget")))
        widget.add(_switch_row(
            _("Show Desktop Widget"), _("A small card you can keep on your desktop"),
            bool(config.get("widget.visible", False)),
            lambda v: config.set("widget.visible", v)))
        size_labels = [_("Small"), _("Medium"), _("Large")]
        size = config.get("widget.size", "medium")
        widget.add(_combo_row(
            _("Size"), "", size_labels,
            WIDGET_SIZES.index(size) if size in WIDGET_SIZES else 1,
            lambda i: config.set("widget.size", WIDGET_SIZES[i])))
        widget.add(_switch_row(
            _("Tinted Background"), _("Colour the widget after the busiest provider"),
            bool(config.get("widget.tinted", False)),
            lambda v: config.set("widget.tinted", v)))
        widget.add(_switch_row(
            _("Keep on Top"),
            _("Pin above other windows on every workspace (needs the Shell extension)"),
            bool(config.get("widget.pinned", False)),
            lambda v: config.set("widget.pinned", v)))
        page.add(widget)

        alerts = Adw.PreferencesGroup(
            title=esc(_("Notifications")),
            description=esc(_("Get a heads-up before you hit a limit")))
        children: list[Adw.SwitchRow] = []

        def master_changed(value: bool) -> None:
            config.set("notifications.enabled", value)
            for child in children:
                child.set_sensitive(value)

        alerts.add(_switch_row(
            _("Usage Alerts"), _("Notify when a quota crosses a threshold"),
            bool(config.get("notifications.enabled", True)), master_changed))
        for key, title, subtitle in (
            ("warn_80", _("At 80%"), _("A gentle warning")),
            ("warn_95", _("At 95%"), _("Last call before the limit")),
            ("on_reset", _("When a Limit Resets"), _("After you were warned about it")),
        ):
            default = key != "on_reset"
            row = _switch_row(title, subtitle, bool(config.get(f"notifications.{key}", default)),
                              lambda v, k=key: config.set(f"notifications.{k}", v))
            row.set_sensitive(bool(config.get("notifications.enabled", True)))
            children.append(row)
            alerts.add(row)
        page.add(alerts)

        demo = Adw.PreferencesGroup(title=esc(_("Demo")))
        demo.add(_switch_row(
            _("Demo Mode"), _("Show realistic sample data instead of your accounts"),
            self.app.engine.demo, self.app.set_demo))
        page.add(demo)
        return page

    def _update_indicator_row(self) -> None:
        state = self.app.indicator_state()
        subtitles = {
            "extension": _("GNOME Shell extension is active"),
            "tray": _("Shown as a tray icon. Install the Shell extension for the full popup."),
            "needs-relogin": _("Extension installed. Log out and back in to finish."),
            "disabled": _("Extension is installed but turned off"),
            "error": _("The extension reported an error. Check the Extensions app."),
            "unavailable": _("Not available on this desktop"),
            "not-installed": _("Install the extension to show usage in the top bar"),
        }
        actions = {
            "tray": _("Install"),
            "not-installed": _("Install"),
            "disabled": _("Enable"),
            "error": _("Details"),
        }
        self.indicator_row.set_subtitle(esc(subtitles.get(state, "")))
        label = actions.get(state)
        self.indicator_button.set_visible(label is not None)
        if label:
            self.indicator_button.set_label(label)
            if state in ("tray", "not-installed", "disabled"):
                self.indicator_button.add_css_class("suggested-action")
        self._indicator_state = state

    def _on_indicator_action(self, _button) -> None:
        message = self.app.setup_shell_extension()
        if message:
            self.add_toast(Adw.Toast(title=message, timeout=6))
        self._update_indicator_row()

    # -- Providers ------------------------------------------------------------------

    def _providers_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title=_("Providers"), icon_name="view-grid-symbolic",
                                   name="providers")
        intro = Adw.PreferencesGroup(
            description=esc(_("QuotaGlance reads the sign-ins your tools already keep on this "
                              "computer. It never asks for passwords; API keys you add are "
                              "stored in GNOME Keyring.")))
        page.add(intro)
        engine = self.app.engine
        by_category: dict[str, list] = {}
        for provider in engine.ordered_providers():
            by_category.setdefault(provider.category, []).append(provider)
        for category, title in CATEGORIES:
            providers = by_category.get(category)
            if not providers:
                continue
            group = Adw.PreferencesGroup(title=esc(title))
            for provider in providers:
                group.add(self._provider_row(provider))
            page.add(group)
        return page

    def _provider_row(self, provider) -> Adw.ExpanderRow:
        engine = self.app.engine
        row = Adw.ExpanderRow(title=esc(provider.name), subtitle=esc(provider.source_summary),
                              show_enable_switch=True)
        row.add_prefix(Badge(provider, "md"))
        row.set_enable_expansion(engine.is_enabled(provider.id))
        row.connect("notify::enable-expansion",
                    lambda r, _p: self.app.set_provider_enabled(provider.id,
                                                                r.get_enable_expansion()))
        self._expanders[provider.id] = row

        status = Adw.ActionRow(title=esc(_("Status")))
        status.set_subtitle_selectable(True)
        check = Gtk.Button(label=_("Check Now"), valign=Gtk.Align.CENTER)
        check.connect("clicked", lambda *_a: engine.refresh([provider.id]))
        status.add_suffix(check)
        row.add_row(status)
        self._status_rows[provider.id] = status
        self._update_status(provider.id)

        for spec in provider.settings:
            widget = self._setting_row(provider, spec)
            if widget is not None:
                row.add_row(widget)

        if provider.setup_hint:
            help_row = Adw.ActionRow(title=esc(_("How to Set Up")),
                                     subtitle=esc(provider.setup_hint))
            help_row.set_subtitle_selectable(True)
            if provider.homepage:
                link = Gtk.LinkButton(uri=provider.homepage, label=_("Website"),
                                      valign=Gtk.Align.CENTER)
                help_row.add_suffix(link)
            row.add_row(help_row)
        return row

    def _setting_row(self, provider, spec):
        settings = self.config.provider_settings(provider.id)
        ctx = self.app.engine.context_for(provider)
        if spec.kind == "secret":
            return self._secret_row(provider, spec)
        if spec.kind == "choice":
            values = [value for value, _label in spec.choices]
            labels = [label for _value, label in spec.choices]
            current = provider.setting_value(ctx, spec)
            return _combo_row(spec.title, spec.subtitle, labels,
                              values.index(current) if current in values else 0,
                              lambda i: self._set_provider(provider.id, spec.key, values[i]))
        if spec.kind == "switch":
            row = None

            def changed(value: bool) -> None:
                if not self._set_provider(provider.id, spec.key, value) and row is not None:
                    row.set_active(not value)

            row = _switch_row(spec.title, spec.subtitle, bool(provider.setting_value(ctx, spec)),
                              changed)
            return row
        if spec.kind == "text":
            entry = Adw.EntryRow(title=esc(spec.title), show_apply_button=True)
            entry.set_text(str(settings.get(spec.key, spec.default) or ""))
            entry.connect("apply", lambda e: self._set_provider(provider.id, spec.key,
                                                                e.get_text().strip()))
            return entry
        return None

    def _secret_row(self, provider, spec) -> Gtk.Widget:
        secrets = self.app.secrets
        saved = bool(secrets.lookup(provider.id, spec.key)) if secrets.available else False
        entry = Adw.PasswordEntryRow(title=esc(spec.title), show_apply_button=True)
        entry.set_tooltip_text(spec.subtitle)
        clear = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                           tooltip_text=_("Remove from Keyring"))
        clear.add_css_class("flat")
        clear.set_visible(saved)
        entry.add_suffix(clear)
        if saved:
            entry.set_title(esc(_("{title} (saved in Keyring)").format(title=spec.title)))
        if not secrets.available:
            entry.set_sensitive(False)
            entry.set_title(esc(_("{title}: GNOME Keyring unavailable").format(title=spec.title)))

        def apply(row: Adw.PasswordEntryRow) -> None:
            value = row.get_text().strip()
            if not value:
                return
            label = _("QuotaGlance: {provider} {what}").format(provider=provider.name,
                                                              what=spec.title)
            if secrets.store(provider.id, spec.key, value, label):
                row.set_text("")
                row.set_title(esc(_("{title} (saved in Keyring)").format(title=spec.title)))
                clear.set_visible(True)
                self.add_toast(Adw.Toast(title=_("Saved to GNOME Keyring"), timeout=3))
                if not self.config.provider_enabled(provider.id):
                    self._expanders[provider.id].set_enable_expansion(True)
                self.app.engine.refresh([provider.id])
            else:
                self.add_toast(Adw.Toast(title=_("Couldn't save to GNOME Keyring")))

        def remove(_button) -> None:
            secrets.clear(provider.id, spec.key)
            entry.set_title(esc(spec.title))
            clear.set_visible(False)
            self.app.engine.refresh([provider.id])

        entry.connect("apply", apply)
        clear.connect("clicked", remove)
        return entry

    def _set_provider(self, provider_id: str, key: str, value) -> bool:
        engine = self.app.engine
        provider = engine.providers[provider_id]
        try:
            message = provider.apply_setting(engine.context_for(provider), key, value)
        except Exception as exc:  # e.g. a settings file we could not update
            self.add_toast(Adw.Toast(title=esc(str(getattr(exc, "message", exc))), timeout=5))
            return False
        self.config.set_provider(provider_id, key, value)
        if message:
            self.add_toast(Adw.Toast(title=esc(message), timeout=4))
        engine.refresh([provider_id])
        return True

    # -- live status ------------------------------------------------------------------

    def _on_data_changed(self, _app, provider_id: str) -> None:
        if provider_id and provider_id in self._status_rows:
            self._update_status(provider_id)
        elif not provider_id:
            for pid in self._status_rows:
                self._update_status(pid)

    def _update_status(self, provider_id: str) -> None:
        row = self._status_rows.get(provider_id)
        if row is None:
            return
        engine = self.app.engine
        snapshot = engine.snapshots.get(provider_id)
        if provider_id in engine.in_flight:
            text = _("Checking…")
        elif snapshot is None:
            text = _("Not checked yet") if engine.is_enabled(provider_id) else _("Turned off")
        elif snapshot.status is Status.OK:
            parts = [_("Working")]
            if snapshot.plan:
                parts.append(snapshot.plan)
            if snapshot.source:
                parts.append(snapshot.source)
            text = " · ".join(parts)
        else:
            text = snapshot.message or _("Not available")
            if snapshot.hint:
                text += f" — {snapshot.hint}"
        row.set_subtitle(esc(text))
