"""Turn the plain text providers write into Pango markup for labels."""

from __future__ import annotations

import re

from gi.repository import GLib

_CODE = re.compile(r"`([^`]+)`")


def markup(text: str | None) -> str:
    """Escape ``text`` for Pango and show `commands` in a monospace font."""
    return _CODE.sub(r"<tt>\1</tt>", GLib.markup_escape_text(text or ""))
