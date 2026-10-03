"""Render the tray meter icon as SNI pixmaps (ARGB32, network byte order)."""

from __future__ import annotations

import math
import sys

import cairo

from quotaglance.models import Severity

SIZES = (16, 22, 24, 32, 48)

COLORS = {
    Severity.NORMAL: (1.0, 1.0, 1.0),
    Severity.WARNING: (0.96, 0.71, 0.10),
    Severity.CRITICAL: (1.0, 0.36, 0.36),
}


def draw_ring(cr: cairo.Context, size: float, fraction: float | None, severity: Severity,
              track_alpha: float = 0.38) -> None:
    thickness = max(2.0, size * 0.15)
    pad = size * 0.08
    radius = (size - thickness) / 2 - pad
    cx = cy = size / 2
    cr.set_line_width(thickness)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_source_rgba(1, 1, 1, track_alpha)
    cr.arc(cx, cy, radius, 0, 2 * math.pi)
    cr.stroke()
    if fraction is not None and fraction > 0.001:
        r, g, b = COLORS.get(severity, COLORS[Severity.NORMAL])
        cr.set_source_rgba(r, g, b, 1.0)
        start = -math.pi / 2
        cr.arc(cx, cy, radius, start, start + 2 * math.pi * min(1.0, fraction))
        cr.stroke()
    if fraction is None:
        # No data yet: a small centred dot so the icon is not an empty circle.
        cr.set_source_rgba(1, 1, 1, 0.6)
        cr.arc(cx, cy, max(1.0, size * 0.08), 0, 2 * math.pi)
        cr.fill()


def surface_to_argb(surface: cairo.ImageSurface) -> bytes:
    """Cairo's premultiplied native-endian ARGB → straight ARGB, big-endian."""
    surface.flush()
    width, height, stride = surface.get_width(), surface.get_height(), surface.get_stride()
    data = bytes(surface.get_data())
    little = sys.byteorder == "little"
    out = bytearray(width * height * 4)
    o = 0
    for y in range(height):
        row = y * stride
        for x in range(width):
            i = row + x * 4
            if little:
                b, g, r, a = data[i], data[i + 1], data[i + 2], data[i + 3]
            else:
                a, r, g, b = data[i], data[i + 1], data[i + 2], data[i + 3]
            if 0 < a < 255:
                r = min(255, r * 255 // a)
                g = min(255, g * 255 // a)
                b = min(255, b * 255 // a)
            out[o:o + 4] = bytes((a, r, g, b))
            o += 4
    return bytes(out)


def ring_pixmaps(fraction: float | None, severity: Severity) -> list[tuple[int, int, bytes]]:
    pixmaps = []
    for size in SIZES:
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        cr = cairo.Context(surface)
        draw_ring(cr, size, fraction, severity)
        pixmaps.append((size, size, surface_to_argb(surface)))
    return pixmaps
