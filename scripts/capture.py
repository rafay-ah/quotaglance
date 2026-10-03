#!/usr/bin/env python3
"""Render QuotaGlance windows to PNG (used for README screenshots and visual QA).

Runs the real app in demo mode with throwaway config directories, then
snapshots windows through GTK's own renderer, so the output is pixel-exact
(including libadwaita's rounded corners and shadows).

    python3 scripts/capture.py --out docs/screenshots [--dark] [--only main,widget-medium]

Needs a display; in CI/headless use `weston --backend=headless` or
`xvfb-run`.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

TMP = Path(tempfile.mkdtemp(prefix="qg-capture-"))
for name in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
    os.environ[name] = str(TMP / name.lower())
os.environ["QUOTAGLANCE_DEMO"] = "1"
os.environ.setdefault("GTK_A11Y", "none")

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")

from gi.repository import Adw, GLib, Graphene, Gtk  # noqa: E402

from quotaglance.gui import application  # noqa: E402
from quotaglance.gui.application import QuotaGlanceApp  # noqa: E402
from quotaglance.secretstore import MemorySecretStore  # noqa: E402

# Screenshots never touch the real keyring (it may be locked or absent).
application.KeyringSecretStore = MemorySecretStore
WATCHDOG_SECONDS = 240


def render(widget: Gtk.Widget, path: Path, pad: int = 0) -> None:
    paintable = Gtk.WidgetPaintable.new(widget)
    width, height = widget.get_width(), widget.get_height()
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, width, height)
    node = snapshot.to_node()
    if node is None:
        print(f"nothing to render for {path.name}")
        return
    bounds = node.get_bounds()
    if pad:
        bounds = Graphene.Rect().init(bounds.get_x() - pad, bounds.get_y() - pad,
                                      bounds.get_width() + 2 * pad,
                                      bounds.get_height() + 2 * pad)
    renderer = widget.get_native().get_renderer()
    texture = renderer.render_texture(node, bounds)
    texture.save_to_png(str(path))
    print(f"wrote {path} ({int(bounds.get_width())}x{int(bounds.get_height())})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "docs" / "screenshots"))
    parser.add_argument("--dark", action="store_true")
    parser.add_argument("--only", default="")
    parser.add_argument("--suffix", default="")
    parser.add_argument("--font", default="")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    only = {s for s in args.only.split(",") if s}
    suffix = args.suffix or ("-dark" if args.dark else "-light")

    app = QuotaGlanceApp()
    def want(name: str) -> bool:
        return not only or name in only

    def setup(_app) -> None:
        manager = Adw.StyleManager.get_default()
        manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK if args.dark
                                 else Adw.ColorScheme.FORCE_LIGHT)
        if args.font:
            Gtk.Settings.get_default().set_property("gtk-font-name", args.font)
        app.config.set("autostart", False)
        app.config.set("widget.visible", False)
        GLib.timeout_add(2500, step)
        GLib.timeout_add_seconds(WATCHDOG_SECONDS, give_up)

    queue: list = []
    failures: list[str] = []

    def give_up() -> bool:
        print(f"capture: gave up after {WATCHDOG_SECONDS} s", file=sys.stderr)
        failures.append("timeout")
        app.quit_app()
        return False

    def step() -> bool:
        if not queue:
            build_queue()
        if not queue:
            app.quit_app()
            return False
        action = queue.pop(0)
        try:
            delay = action()
        except Exception:  # report it and keep going: one broken shot must not hang CI
            traceback.print_exc()
            failures.append(getattr(action, "__name__", "step"))
            delay = None
        GLib.timeout_add(delay or 900, step)
        return False

    def build_queue() -> None:
        if getattr(build_queue, "done", False):
            return
        build_queue.done = True
        if want("main"):
            queue.append(lambda: (app.show_window(), 1600)[1])
            queue.append(lambda: render(app.window, out / f"main{suffix}.png", pad=0))
        for size in ("small", "medium", "large"):
            for tinted in (False, True):
                name = f"widget-{size}" + ("-tinted" if tinted else "")
                if not want(name):
                    continue

                def show(size=size, tinted=tinted):
                    app.config.set("widget.size", size)
                    app.config.set("widget.tinted", tinted)
                    app.config.set("widget.visible", True)
                    return 1400

                def shoot(name=name):
                    render(app.widget, out / f"{name}{suffix}.png")

                queue.append(show)
                queue.append(shoot)
        if want("preferences"):
            def prefs():
                app.show_preferences()
                return 1500

            def shoot_prefs():
                for window in app.get_windows():
                    if window is app.window:
                        render(window, out / f"preferences{suffix}.png")
                        break

            queue.append(prefs)
            queue.append(shoot_prefs)
        if want("providers"):
            def prefs_providers():
                for window in app.get_windows():
                    if window is not app.window and window is not app.widget:
                        window.close()
                app.show_preferences(page="providers")
                return 1500

            def shoot_providers():
                render(app.window, out / f"providers{suffix}.png")

            queue.append(prefs_providers)
            queue.append(shoot_providers)

    app.connect("startup", setup)
    status = app.run([sys.argv[0]])
    if failures:
        print(f"capture: failed steps: {', '.join(failures)}", file=sys.stderr)
        return 1
    return status


if __name__ == "__main__":
    sys.exit(main())
