// QuotaGlance GNOME Shell extension.
//
// A thin view over the QuotaGlance app: it reads usage over D-Bus and never
// touches the network or any credentials itself. It also "pins" the
// QuotaGlance desktop widget (keep on top, all workspaces, remembered
// position), which a Wayland client cannot do on its own.
//
// SPDX-License-Identifier: MIT

import Cairo from 'cairo';
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import * as Config from 'resource:///org/gnome/shell/misc/config.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const APP_ID = 'io.github.rafay_ah.QuotaGlance';
const OBJECT_PATH = '/io/github/rafay_ah/QuotaGlance';
const IFACE = 'io.github.rafay_ah.QuotaGlance1';
const PANEL_BUS_NAME = 'io.github.rafay_ah.QuotaGlance.Panel';
const WIDGET_TITLE = 'QuotaGlance Widget';
const SHELL_MAJOR = parseInt(Config.PACKAGE_VERSION.split('.')[0], 10);

const SEVERITY_COLORS = ['', '#f6c343', '#ff6b6b'];
const BAR_WIDTH = 132;

const QuotaProxy = Gio.DBusProxy.makeProxyWrapper(`
<node>
  <interface name="${IFACE}">
    <method name="GetSnapshot"><arg type="s" name="json" direction="out"/></method>
    <method name="Refresh"/>
    <method name="ShowWindow"/>
    <method name="ShowPreferences"/>
    <method name="SetWidgetVisible"><arg type="b" name="visible" direction="in"/></method>
    <method name="SetWidgetPinned"><arg type="b" name="pinned" direction="in"/></method>
    <method name="SetWidgetPosition">
      <arg type="i" name="x" direction="in"/>
      <arg type="i" name="y" direction="in"/>
    </method>
    <signal name="Changed"><arg type="s" name="json"/></signal>
  </interface>
</node>`);

// St.BoxLayout gained `orientation` (and deprecated `vertical`) in GNOME 48.
function vbox(params = {}) {
    if (SHELL_MAJOR >= 48)
        return new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, ...params});
    return new St.BoxLayout({vertical: true, ...params});
}

function hbox(params = {}) {
    return new St.BoxLayout(params);
}

function hexToRgb(hex) {
    const value = (hex || '#ffffff').replace('#', '');
    return [0, 2, 4].map(i => parseInt(value.substr(i, 2), 16) / 255);
}

function formatCountdown(iso) {
    if (!iso)
        return '';
    const seconds = Math.max(0, (Date.parse(iso) - Date.now()) / 1000);
    if (seconds < 60)
        return seconds <= 0 ? 'now' : '<1m';
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    if (days)
        return hours ? `${days}d ${hours}h` : `${days}d`;
    if (hours)
        return minutes ? `${hours}h ${minutes}m` : `${hours}h`;
    return `${minutes}m`;
}

function dim(actor, opacity = 170) {
    actor.opacity = opacity;
    return actor;
}

const QuotaIndicator = GObject.registerClass(
class QuotaIndicator extends PanelMenu.Button {
    _init(controller) {
        super._init(0.5, 'QuotaGlance', false);
        this._controller = controller;
        this._fraction = null;
        this._severity = 0;

        const box = hbox({style_class: 'panel-status-menu-box qg-panel-box'});
        this._ring = new St.DrawingArea({
            style_class: 'qg-panel-ring',
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._ring.connect('repaint', area => this._paintRing(area));
        this._label = new St.Label({
            text: '',
            style_class: 'qg-panel-label',
            y_align: Clutter.ActorAlign.CENTER,
        });
        box.add_child(this._ring);
        box.add_child(this._label);
        this.add_child(box);

        this.menu.box.add_style_class_name('qg-menu');
        this._header = new PopupMenu.PopupBaseMenuItem({
            activate: false,
            hover: false,
            can_focus: false,
        });
        const headerBox = hbox({x_expand: true, style_class: 'qg-header'});
        const titles = vbox({x_expand: true});
        titles.add_child(new St.Label({text: 'QuotaGlance', style_class: 'qg-header-title'}));
        this._updated = dim(new St.Label({text: '', style_class: 'qg-header-sub'}));
        titles.add_child(this._updated);
        headerBox.add_child(titles);
        this._refreshButton = new St.Button({
            style_class: 'qg-icon-button',
            can_focus: true,
            y_align: Clutter.ActorAlign.CENTER,
            child: new St.Icon({icon_name: 'view-refresh-symbolic', icon_size: 16}),
            accessible_name: 'Refresh now',
        });
        this._refreshButton.connect('clicked', () => this._controller.call('Refresh'));
        headerBox.add_child(this._refreshButton);
        this._header.add_child(headerBox);
        this.menu.addMenuItem(this._header);

        // Providers live in a scroll view so long lists never overflow the screen.
        this._providerSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._providerSection);
        this._scroll = new St.ScrollView({
            style_class: 'qg-scroll',
            hscrollbar_policy: St.PolicyType.NEVER,
            vscrollbar_policy: St.PolicyType.AUTOMATIC,
            overlay_scrollbars: true,
        });
        const sectionActor = this._providerSection.actor;
        const index = this.menu.box.get_children().indexOf(sectionActor);
        this.menu.box.remove_child(sectionActor);
        if (this._scroll.set_child)
            this._scroll.set_child(sectionActor);
        else
            this._scroll.add_actor(sectionActor);
        this.menu.box.insert_child_at_index(this._scroll, index);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._widgetSwitch = new PopupMenu.PopupSwitchMenuItem('Desktop widget', false);
        this._widgetSwitch.connect('toggled', (_item, state) =>
            this._controller.call('SetWidgetVisible', state));
        this.menu.addMenuItem(this._widgetSwitch);
        this._pinSwitch = new PopupMenu.PopupSwitchMenuItem('Keep widget on top', false);
        this._pinSwitch.connect('toggled', (_item, state) =>
            this._controller.call('SetWidgetPinned', state));
        this.menu.addMenuItem(this._pinSwitch);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._openItem = this.menu.addAction('Open QuotaGlance',
            () => this._controller.openApp());
        this.menu.addAction('Preferences', () => this._controller.call('ShowPreferences'));

        this.menu.connect('open-state-changed', (_menu, open) => {
            if (!open)
                return;
            const monitor = Main.layoutManager.primaryMonitor;
            const scale = St.ThemeContext.get_for_stage(global.stage).scale_factor;
            const limit = Math.round((monitor?.height ?? 900) * 0.62 / scale);
            this._scroll.style = `max-height: ${limit}px;`;
            this._controller.render();
        });
    }

    setData(data, running) {
        const panel = data?.panel ?? {};
        this._fraction = running && typeof panel.percent === 'number'
            ? Math.min(1, Math.max(0, panel.percent / 100)) : null;
        this._severity = panel.severity ?? 0;
        this._ring.queue_repaint();
        this._label.text = running && panel.show_percent && panel.percent_text
            ? panel.percent_text : '';
        this._label.visible = this._label.text !== '';
        for (const name of ['qg-warning', 'qg-critical'])
            this._label.remove_style_class_name(name);
        if (this._severity === 1)
            this._label.add_style_class_name('qg-warning');
        else if (this._severity === 2)
            this._label.add_style_class_name('qg-critical');
        this.accessible_name = panel.tooltip ?? 'QuotaGlance';

        this._updated.text = running ? (data?.updated_text ?? '') : 'QuotaGlance is not running';
        this._refreshButton.visible = running;
        this._widgetSwitch.visible = running;
        this._pinSwitch.visible = running;
        this._widgetSwitch.setToggleState(Boolean(data?.widget?.visible));
        this._pinSwitch.setToggleState(Boolean(data?.widget?.pinned));
        this._openItem.label.text = running ? 'Open QuotaGlance' : 'Start QuotaGlance';

        this._providerSection.removeAll();
        if (!running)
            return;
        const providers = data?.providers ?? [];
        if (providers.length === 0) {
            const empty = new PopupMenu.PopupMenuItem('No providers enabled yet', {
                reactive: true,
            });
            empty.connect('activate', () => this._controller.call('ShowPreferences'));
            this._providerSection.addMenuItem(empty);
            return;
        }
        for (const provider of providers)
            this._providerSection.addMenuItem(this._providerItem(provider));
    }

    _providerItem(provider) {
        const item = new PopupMenu.PopupBaseMenuItem({style_class: 'qg-provider-item'});
        const row = hbox({x_expand: true, style_class: 'qg-provider'});
        const badge = new St.Bin({
            style_class: 'qg-badge',
            style: `background-color: ${provider.color};`,
            y_align: Clutter.ActorAlign.START,
            child: new St.Label({
                text: provider.short,
                style_class: 'qg-badge-text',
                x_align: Clutter.ActorAlign.CENTER,
                y_align: Clutter.ActorAlign.CENTER,
            }),
        });
        row.add_child(badge);

        const column = vbox({x_expand: true, style_class: 'qg-provider-col'});
        const title = hbox({x_expand: true});
        title.add_child(new St.Label({
            text: provider.name,
            style_class: 'qg-provider-name',
            x_expand: true,
        }));
        if (provider.plan)
            title.add_child(dim(new St.Label({text: provider.plan, style_class: 'qg-plan'})));
        column.add_child(title);

        const failed = ['error', 'not_configured', 'stale'].includes(provider.status);
        if (failed && provider.message) {
            const note = new St.Label({
                text: provider.message.replaceAll('`', ''),
                style_class: 'qg-note',
            });
            note.clutter_text.line_wrap = true;
            column.add_child(dim(note, 200));
        }
        for (const window of (provider.windows ?? []).slice(0, 4))
            column.add_child(this._windowRow(provider, window, provider.status === 'stale'));
        if (!provider.windows?.length && !failed)
            column.add_child(dim(new St.Label({text: provider.meta_text ?? '',
                style_class: 'qg-note'})));
        row.add_child(column);
        item.add_child(row);
        item.connect('activate', () => this._controller.openApp());
        return item;
    }

    _windowRow(provider, window, stale) {
        const row = hbox({style_class: 'qg-window-row', x_expand: true});
        row.add_child(dim(new St.Label({
            text: window.label,
            style_class: 'qg-window-label',
            y_align: Clutter.ActorAlign.CENTER,
        }), 200));
        if (window.percent !== null && window.percent !== undefined) {
            const track = new St.Widget({
                style_class: 'qg-bar-track',
                width: BAR_WIDTH,
                y_align: Clutter.ActorAlign.CENTER,
            });
            const color = window.severity ? SEVERITY_COLORS[window.severity] : provider.color;
            const fill = new St.Widget({
                style_class: 'qg-bar-fill',
                style: `background-color: ${color};`,
                width: Math.max(window.fraction > 0 ? 6 : 0, Math.round(BAR_WIDTH * window.fraction)),
            });
            track.add_child(fill);
            row.add_child(track);
            const pct = new St.Label({
                text: window.percent_text,
                style_class: 'qg-window-pct',
                y_align: Clutter.ActorAlign.CENTER,
            });
            if (window.severity === 1)
                pct.add_style_class_name('qg-warning');
            else if (window.severity === 2)
                pct.add_style_class_name('qg-critical');
            row.add_child(pct);
        } else {
            row.add_child(new St.Label({
                text: window.amount_text ?? window.detail ?? '—',
                style_class: 'qg-window-value',
                y_align: Clutter.ActorAlign.CENTER,
            }));
        }
        row.add_child(dim(new St.Label({
            text: formatCountdown(window.resets_at),
            style_class: 'qg-window-reset',
            x_expand: true,
            x_align: Clutter.ActorAlign.END,
            y_align: Clutter.ActorAlign.CENTER,
        })));
        if (stale)
            row.opacity = 150;
        return row;
    }

    _paintRing(area) {
        const cr = area.get_context();
        const [width, height] = area.get_surface_size();
        const size = Math.min(width, height);
        const thickness = Math.max(2, size * 0.16);
        const radius = (size - thickness) / 2 - size * 0.04;
        const cx = width / 2;
        const cy = height / 2;
        const fg = area.get_theme_node().get_foreground_color();
        cr.setLineWidth(thickness);
        cr.setLineCap(Cairo.LineCap.ROUND);
        cr.setSourceRGBA(fg.red / 255, fg.green / 255, fg.blue / 255, 0.35);
        cr.arc(cx, cy, radius, 0, 2 * Math.PI);
        cr.stroke();
        if (this._fraction === null) {
            cr.setSourceRGBA(fg.red / 255, fg.green / 255, fg.blue / 255, 0.6);
            cr.arc(cx, cy, Math.max(1, size * 0.09), 0, 2 * Math.PI);
            cr.fill();
        } else if (this._fraction > 0.001) {
            const [r, g, b] = this._severity
                ? hexToRgb(SEVERITY_COLORS[this._severity])
                : [fg.red / 255, fg.green / 255, fg.blue / 255];
            cr.setSourceRGBA(r, g, b, 1);
            const start = -Math.PI / 2;
            cr.arc(cx, cy, radius, start, start + 2 * Math.PI * this._fraction);
            cr.stroke();
        }
        cr.$dispose();
    }
});

class Controller {
    constructor(extension) {
        this._extension = extension;
        this._data = null;
        this._proxy = null;
        this._indicator = new QuotaIndicator(this);
        this._widgetWindow = null;
        this._positionTimeout = 0;
        this._tickId = 0;

        Main.panel.addToStatusArea(extension.uuid, this._indicator);
        this._indicator.setData(null, false);

        this._nameId = Gio.bus_own_name(Gio.BusType.SESSION, PANEL_BUS_NAME,
            Gio.BusNameOwnerFlags.NONE, null, null, null);

        new QuotaProxy(Gio.DBus.session, APP_ID, OBJECT_PATH, (proxy, error) => {
            if (error) {
                console.warn(`QuotaGlance: D-Bus proxy failed: ${error.message}`);
                return;
            }
            if (!this._indicator)
                return;  // disabled meanwhile
            this._proxy = proxy;
            this._changedId = proxy.connectSignal('Changed', (_p, _sender, [json]) =>
                this._apply(json));
            this._ownerId = proxy.connect('notify::g-name-owner', () => this._reload());
            this._reload();
        }, null, Gio.DBusProxyFlags.DO_NOT_AUTO_START);

        global.display.connectObject('window-created',
            (_display, win) => this._watchWindow(win), this);
        for (const actor of global.get_window_actors())
            this._watchWindow(actor.meta_window);

        // Countdowns tick even when nothing else changes.
        this._tickId = GLib.timeout_add_seconds(GLib.PRIORITY_LOW, 30, () => {
            if (this._indicator?.menu.isOpen)
                this.render();
            return GLib.SOURCE_CONTINUE;
        });
    }

    get running() {
        return Boolean(this._proxy?.g_name_owner);
    }

    _reload() {
        if (!this.running) {
            this._data = null;
            this.render();
            return;
        }
        this._proxy.GetSnapshotRemote((result, error) => {
            if (error) {
                console.warn(`QuotaGlance: GetSnapshot failed: ${error.message}`);
                return;
            }
            this._apply(result[0]);
        });
    }

    _apply(json) {
        try {
            this._data = JSON.parse(json);
        } catch (e) {
            console.warn(`QuotaGlance: bad snapshot: ${e.message}`);
            return;
        }
        this.render();
        this._applyWidgetState();
    }

    render() {
        this._indicator?.setData(this._data, this.running);
    }

    call(method, ...args) {
        if (!this.running) {
            this.openApp();
            return;
        }
        this._proxy[`${method}Remote`](...args, (_result, error) => {
            if (error)
                console.warn(`QuotaGlance: ${method} failed: ${error.message}`);
        });
    }

    openApp() {
        if (this.running) {
            this.call('ShowWindow');
            return;
        }
        const app = Shell.AppSystem.get_default().lookup_app(`${APP_ID}.desktop`);
        if (app)
            app.activate();
        else
            Main.notify('QuotaGlance', 'The QuotaGlance app is not installed.');
    }

    // -- desktop widget pinning ---------------------------------------------

    _isWidget(win) {
        if (!win)
            return false;
        const appId = win.get_gtk_application_id?.() ?? win.get_wm_class?.();
        return (appId === APP_ID || win.get_wm_class?.() === APP_ID) &&
            win.get_title() === WIDGET_TITLE;
    }

    _watchWindow(win) {
        if (!win)
            return;
        if (this._isWidget(win)) {
            this._manageWidget(win);
            return;
        }
        // The title can arrive just after creation: look again once it is shown.
        win.connectObject('shown', () => {
            win.disconnectObject(this);
            if (this._isWidget(win) && this._widgetWindow !== win)
                this._manageWidget(win);
        }, this);
    }

    _manageWidget(win) {
        this._widgetWindow = win;
        win.connectObject(
            'position-changed', () => this._savePositionSoon(),
            'unmanaged', () => {
                if (this._widgetWindow === win)
                    this._widgetWindow = null;
            }, this);
        const position = this._data?.widget?.position;
        if (Array.isArray(position) && position.length === 2) {
            const [x, y] = position;
            const rect = win.get_frame_rect();
            if (x >= 0 && y >= 0 && x + rect.width / 2 < global.stage.width &&
                y + rect.height / 2 < global.stage.height)
                win.move_frame(true, x, y);
        }
        this._applyWidgetState();
    }

    _applyWidgetState() {
        const win = this._widgetWindow;
        if (!win)
            return;
        try {
            if (this._data?.widget?.pinned) {
                if (!win.is_above())
                    win.make_above();
                if (!win.is_on_all_workspaces())
                    win.stick();
            } else {
                if (win.is_above())
                    win.unmake_above();
                if (win.is_on_all_workspaces())
                    win.unstick();
            }
        } catch (e) {
            console.warn(`QuotaGlance: could not update widget window: ${e.message}`);
        }
    }

    _savePositionSoon() {
        if (this._positionTimeout)
            GLib.source_remove(this._positionTimeout);
        this._positionTimeout = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 600, () => {
            this._positionTimeout = 0;
            const win = this._widgetWindow;
            if (win && this.running) {
                const rect = win.get_frame_rect();
                this.call('SetWidgetPosition', rect.x, rect.y);
            }
            return GLib.SOURCE_REMOVE;
        });
    }

    destroy() {
        if (this._tickId)
            GLib.source_remove(this._tickId);
        if (this._positionTimeout)
            GLib.source_remove(this._positionTimeout);
        this._tickId = this._positionTimeout = 0;
        global.display.disconnectObject(this);
        for (const actor of global.get_window_actors())
            actor.meta_window?.disconnectObject(this);
        if (this._proxy) {
            if (this._changedId)
                this._proxy.disconnectSignal(this._changedId);
            if (this._ownerId)
                this._proxy.disconnect(this._ownerId);
            this._proxy = null;
        }
        if (this._nameId) {
            Gio.bus_unown_name(this._nameId);
            this._nameId = 0;
        }
        this._indicator?.destroy();
        this._indicator = null;
        this._widgetWindow = null;
    }
}

export default class QuotaGlanceExtension extends Extension {
    enable() {
        this._controller = new Controller(this);
    }

    disable() {
        this._controller?.destroy();
        this._controller = null;
    }
}
