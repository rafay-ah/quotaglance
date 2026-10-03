// Development-only helper: scripted screenshots inside a headless GNOME Shell.
// Steps come from $QG_CAPTURE_STEPS (comma separated), files go to $QG_CAPTURE_DIR.
//   wait:<ms>  open-menu  close-menu  shot:<name>  log:<text>  quit
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import Shell from 'gi://Shell';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const INDICATOR = 'quotaglance@rafay-ah.github.io';

export default class CaptureExtension extends Extension {
    enable() {
        this._dir = GLib.getenv('QG_CAPTURE_DIR') || '/tmp/qg-capture';
        GLib.mkdir_with_parents(this._dir, 0o755);
        this._steps = (GLib.getenv('QG_CAPTURE_STEPS') || 'wait:8000,shot:desktop').split(',');
        this._next(1000);
    }

    disable() {
        if (this._timeout)
            GLib.source_remove(this._timeout);
        this._timeout = 0;
    }

    _next(delay) {
        this._timeout = GLib.timeout_add(GLib.PRIORITY_DEFAULT, delay, () => {
            this._timeout = 0;
            this._run();
            return GLib.SOURCE_REMOVE;
        });
    }

    _log(text) {
        const file = Gio.File.new_for_path(`${this._dir}/capture.log`);
        const stream = file.append_to(Gio.FileCreateFlags.NONE, null);
        stream.write_all(`${text}\n`, null);
        stream.close(null);
    }

    _run() {
        const step = this._steps.shift();
        if (!step)
            return;
        const [cmd, arg] = step.split(':');
        let delay = 300;
        try {
            if (cmd === 'wait') {
                delay = parseInt(arg, 10);
            } else if (cmd === 'open-menu') {
                // open-menu[:<status-area key or prefix>]
                const wanted = step.slice('open-menu:'.length) || INDICATOR;
                const key = Object.keys(Main.panel.statusArea).find(k => k.startsWith(wanted));
                const indicator = key ? Main.panel.statusArea[key] : null;
                this._log(`open-menu ${wanted}: ${indicator ? key : 'missing'}`);
                indicator?.menu.open(false);
                delay = 1200;
            } else if (cmd === 'close-menu') {
                Main.panel.statusArea[arg || INDICATOR]?.menu.close(false);
                delay = 600;
            } else if (cmd === 'shot') {
                this._shot(`${this._dir}/${arg}.png`);
                delay = 1500;
            } else if (cmd === 'clear-notifications') {
                for (const source of Main.messageTray.getSources())
                    source.destroy();
                delay = 600;
            } else if (cmd === 'overview-hide') {
                Main.overview.hide();
                delay = 800;
            } else if (cmd === 'move') {
                // move:<title>:<x>:<y>
                const [, title, x, y] = step.split(':');
                for (const actor of global.get_window_actors()) {
                    const win = actor.meta_window;
                    if (win.get_title() === title)
                        win.move_frame(true, parseInt(x, 10), parseInt(y, 10));
                }
            } else if (cmd === 'focus') {
                const [, title] = step.split(':');
                for (const actor of global.get_window_actors()) {
                    const win = actor.meta_window;
                    if (win.get_title() === title)
                        win.activate(global.get_current_time());
                }
            } else if (cmd === 'log') {
                const ids = Object.entries(Main.panel.statusArea)
                    .map(([key, item]) => `${key}${item.container?.visible === false ? '(hidden)' : ''}`)
                    .join(' ');
                this._log(`status area: ${ids}`);
            } else if (cmd === 'quit') {
                this._log('done');
                return;
            }
        } catch (e) {
            this._log(`step ${step} failed: ${e.message}`);
        }
        this._next(delay);
    }

    _shot(path) {
        const file = Gio.File.new_for_path(path);
        const stream = file.replace(null, false, Gio.FileCreateFlags.NONE, null);
        const shooter = new Shell.Screenshot();
        shooter.screenshot(false, stream, (obj, res) => {
            try {
                obj.screenshot_finish(res);
                this._log(`wrote ${path}`);
            } catch (e) {
                this._log(`screenshot failed: ${e.message}`);
            }
            stream.close(null);
        });
    }
}
