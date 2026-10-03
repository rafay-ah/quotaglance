"""Reusable widgets: meter bar, ring gauge, provider badge, colour helpers."""

from __future__ import annotations

import colorsys
import math

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")

from gi.repository import Adw, Gdk, Graphene, Gsk, Gtk, Pango  # noqa: E402

from quotaglance.models import Severity  # noqa: E402

WARNING_HEX = "#e5a50a"
CRITICAL_HEX = "#e01b24"
CRITICAL_DARK_HEX = "#ff5a5f"


# --------------------------------------------------------------------------
# Colours


def hex_to_rgb(value: str) -> tuple[float, float, float]:
    value = value.lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    return tuple(int(value[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02x}" for c in rgb)


def adjust_lightness(value: str, delta: float) -> str:
    r, g, b = hex_to_rgb(value)
    hue, lightness, saturation = colorsys.rgb_to_hls(r, g, b)
    lightness = max(0.0, min(1.0, lightness + delta))
    return rgb_to_hex(colorsys.hls_to_rgb(hue, lightness, saturation))


def luminance(value: str) -> float:
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in hex_to_rgb(value))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def readable_on_dark(value: str) -> str:
    """Lift very dark brand colours so rings and bars stay visible in dark mode."""
    color = value
    for _ in range(6):
        if luminance(color) >= 0.16:
            break
        color = adjust_lightness(color, 0.08)
    return color


def rgba(value: str, alpha: float = 1.0) -> Gdk.RGBA:
    color = Gdk.RGBA()
    color.parse(value)
    color.alpha = alpha
    return color


def is_dark() -> bool:
    return Adw.StyleManager.get_default().get_dark()


def accent_for(provider_color: str, severity: Severity) -> str:
    dark = is_dark()
    if severity is Severity.CRITICAL:
        return CRITICAL_DARK_HEX if dark else CRITICAL_HEX
    if severity is Severity.WARNING:
        return WARNING_HEX
    return readable_on_dark(provider_color) if dark else provider_color


def provider_css(providers) -> str:
    """Badge and tinted-widget colours for every provider."""
    rules = []
    for provider in providers:
        base = provider.color
        light = adjust_lightness(base, 0.10)
        deep = adjust_lightness(base, -0.08)
        rules.append(
            f".qg-badge.qg-p-{provider.id} {{ background-color: {base};"
            f" background-image: linear-gradient(160deg, {light}, {deep}); }}")
        top = adjust_lightness(base, 0.02)
        bottom = adjust_lightness(base, -0.16)
        rules.append(
            f"window.qg-widget-window.tinted.qg-p-{provider.id} {{ background-color: {bottom};"
            f" background-image: linear-gradient(165deg, {top}, {bottom}); }}")
    return "\n".join(rules)


# --------------------------------------------------------------------------
# Meter bar


def _rounded_fill(snapshot: Gtk.Snapshot, x: float, y: float, w: float, h: float,
                  color: Gdk.RGBA) -> None:
    rect = Graphene.Rect().init(x, y, w, h)
    rounded = Gsk.RoundedRect()
    rounded.init_from_rect(rect, h / 2)
    snapshot.push_rounded_clip(rounded)
    snapshot.append_color(color, rect)
    snapshot.pop()


class _Animated:
    """Mixin: animate a 0..1 value with libadwaita's timed animations."""

    _value = 0.0
    _target = 0.0
    _animation = None

    def _animate_to(self, fraction: float, animate: bool) -> None:
        fraction = max(0.0, min(1.0, fraction))
        if (abs(fraction - self._target) < 1e-4 and self._animation is None
                and abs(self._value - fraction) < 1e-4):
            return
        self._target = fraction
        if self._animation is not None:
            self._animation.pause()
            self._animation = None
        if animate and self.get_mapped() and Adw.StyleManager.get_default() is not None:
            target = Adw.CallbackAnimationTarget.new(self._on_animation_value)
            animation = Adw.TimedAnimation.new(self, self._value, fraction, 650, target)
            animation.set_easing(Adw.Easing.EASE_OUT_CUBIC)
            animation.connect("done", self._on_animation_done)
            self._animation = animation
            animation.play()
        else:
            self._value = fraction
            self.queue_draw()

    def _on_animation_value(self, value: float) -> None:
        self._value = value
        self.queue_draw()

    def _on_animation_done(self, _animation) -> None:
        self._animation = None


class Meter(_Animated, Gtk.Widget):
    """A slim, rounded progress bar with an animated fill."""

    __gtype_name__ = "QgMeter"

    def __init__(self, thickness: int = 8) -> None:
        super().__init__(accessible_role=Gtk.AccessibleRole.PROGRESS_BAR)
        self._thickness = thickness
        self._color = rgba("#3584e4")
        self.set_valign(Gtk.Align.CENTER)
        self.set_hexpand(True)

    def set_color(self, value: str) -> None:
        self._color = rgba(value)
        self.queue_draw()

    def set_fraction(self, fraction: float, animate: bool = True) -> None:
        self._animate_to(fraction, animate)
        percent = round(max(0.0, min(1.0, fraction)) * 100)
        self.update_property(
            [Gtk.AccessibleProperty.VALUE_MIN, Gtk.AccessibleProperty.VALUE_MAX,
             Gtk.AccessibleProperty.VALUE_NOW, Gtk.AccessibleProperty.VALUE_TEXT],
            [0.0, 100.0, float(percent), f"{percent}%"])

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            return 32, 140, -1, -1
        return self._thickness, self._thickness, -1, -1

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width = self.get_width()
        height = self.get_height()
        thickness = min(self._thickness, height)
        y = (height - thickness) / 2
        fg = self.get_color()
        track = Gdk.RGBA()
        track.red, track.green, track.blue = fg.red, fg.green, fg.blue
        track.alpha = 0.14
        _rounded_fill(snapshot, 0, y, width, thickness, track)
        if self._value > 0.0005:
            fill = max(thickness, width * self._value)
            _rounded_fill(snapshot, 0, y, fill, thickness, self._color)


class Ring(_Animated, Gtk.Widget):
    """Circular gauge, drawn with cairo, starting at 12 o'clock."""

    __gtype_name__ = "QgRing"

    def __init__(self, size: int = 64, thickness: float = 7.0) -> None:
        super().__init__(accessible_role=Gtk.AccessibleRole.PROGRESS_BAR)
        self._size = size
        self._thickness = thickness
        self._color = (0.21, 0.52, 0.89)
        self._track_alpha = 0.14
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)

    def set_color(self, value: str) -> None:
        self._color = hex_to_rgb(value)
        self.queue_draw()

    def set_track_alpha(self, alpha: float) -> None:
        self._track_alpha = alpha
        self.queue_draw()

    def set_fraction(self, fraction: float, animate: bool = True) -> None:
        self._animate_to(fraction, animate)
        percent = round(max(0.0, min(1.0, fraction)) * 100)
        self.update_property(
            [Gtk.AccessibleProperty.VALUE_MIN, Gtk.AccessibleProperty.VALUE_MAX,
             Gtk.AccessibleProperty.VALUE_NOW, Gtk.AccessibleProperty.VALUE_TEXT],
            [0.0, 100.0, float(percent), f"{percent}%"])

    def do_measure(self, orientation, for_size):
        return self._size, self._size, -1, -1

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width = self.get_width()
        height = self.get_height()
        size = min(width, height)
        if size <= 0:
            return
        cr = snapshot.append_cairo(Graphene.Rect().init(0, 0, width, height))
        cx, cy = width / 2, height / 2
        radius = (size - self._thickness) / 2
        cr.set_line_width(self._thickness)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        fg = self.get_color()
        cr.set_source_rgba(fg.red, fg.green, fg.blue, self._track_alpha)
        cr.arc(cx, cy, radius, 0, 2 * math.pi)
        cr.stroke()
        if self._value > 0.0005:
            start = -math.pi / 2
            cr.set_source_rgb(*self._color)
            cr.arc(cx, cy, radius, start, start + 2 * math.pi * self._value)
            cr.stroke()


class Gauge:
    """A ring with a centred percentage label and an optional caption below it."""

    def __init__(self, size: int = 64, thickness: float = 7.0, caption: bool = False) -> None:
        self.overlay = Gtk.Overlay(halign=Gtk.Align.CENTER)
        self.ring = Ring(size, thickness)
        self.overlay.set_child(self.ring)
        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.CENTER,
                         valign=Gtk.Align.CENTER)
        self.label = Gtk.Label()
        self.label.add_css_class("numeric")
        center.append(self.label)
        self.caption: Gtk.Label | None = None
        if caption:
            self.caption = Gtk.Label()
            self.caption.add_css_class("qg-widget-caption")
            self.caption.add_css_class("dim-label")
            self.caption.set_max_width_chars(9)
            self.caption.set_ellipsize(Pango.EllipsizeMode.END)
            center.append(self.caption)
        self.overlay.add_overlay(center)


class Badge(Gtk.Label):
    """Rounded monogram tile in the provider's colour."""

    __gtype_name__ = "QgBadge"

    SIZES = {"sm": 18, "md": 32, "lg": 40}

    def __init__(self, provider, size: str = "md") -> None:
        super().__init__(label=provider.short)
        pixels = self.SIZES.get(size, 32)
        self.set_size_request(pixels, pixels)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self.set_xalign(0.5)
        self.set_yalign(0.5)
        self.add_css_class("qg-badge")
        self.add_css_class(f"size-{size}")
        self.add_css_class(f"qg-p-{provider.id}")
        self.update_property([Gtk.AccessibleProperty.LABEL], [provider.name])


def severity_class(severity: Severity) -> str | None:
    if severity is Severity.CRITICAL:
        return "qg-severity-critical"
    if severity is Severity.WARNING:
        return "qg-severity-warning"
    return None


def set_severity_class(widget: Gtk.Widget, severity: Severity) -> None:
    for name in ("qg-severity-warning", "qg-severity-critical"):
        widget.remove_css_class(name)
    name = severity_class(severity)
    if name:
        widget.add_css_class(name)
