"""Human-friendly formatting for countdowns, ages, percentages and amounts."""

from __future__ import annotations

from datetime import datetime

from quotaglance.i18n import _, ngettext


def format_duration(seconds: float, max_units: int = 2) -> str:
    """Compact duration: ``"3d 4h"``, ``"2h 13m"``, ``"45m"``, ``"<1m"``."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return _("<1m")
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts: list[str] = []
    if days:
        parts.append(_("{n}d").format(n=days))
    if hours and len(parts) < max_units:
        parts.append(_("{n}h").format(n=hours))
    if minutes and len(parts) < max_units and not days:
        parts.append(_("{n}m").format(n=minutes))
    return " ".join(parts) if parts else _("<1m")


def format_countdown(resets_at: datetime | None, now: datetime) -> str | None:
    """``"2h 13m"`` until reset, or None when the reset time is unknown."""
    if resets_at is None:
        return None
    delta = (resets_at - now).total_seconds()
    if delta <= 0:
        return _("now")
    return format_duration(delta)


def format_reset(resets_at: datetime | None, now: datetime) -> str | None:
    """Sentence form for labels: ``"Resets in 2h 13m"``."""
    countdown = format_countdown(resets_at, now)
    if countdown is None:
        return None
    if countdown == _("now"):
        return _("Resets now")
    return _("Resets in {countdown}").format(countdown=countdown)


def format_absolute(moment: datetime | None, now: datetime) -> str | None:
    """Local wall-clock time of a reset, e.g. ``"Today 14:30"``, ``"Mon 09:00"``."""
    if moment is None:
        return None
    local = moment.astimezone()
    local_now = now.astimezone()
    days = (local.date() - local_now.date()).days
    clock = local.strftime("%H:%M")
    if days == 0:
        return _("Today {time}").format(time=clock)
    if days == 1:
        return _("Tomorrow {time}").format(time=clock)
    if 1 < days < 7:
        return f"{local.strftime('%a')} {clock}"
    return local.strftime("%b %-d, %H:%M")


def format_ago(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return _("never")
    seconds = (now - moment).total_seconds()
    if seconds < 45:
        return _("just now")
    minutes = int(seconds // 60)
    if minutes < 60:
        minutes = max(1, minutes)
        return ngettext("{n} min ago", "{n} min ago", minutes).format(n=minutes)
    hours = minutes // 60
    if hours < 24:
        return ngettext("{n} hour ago", "{n} hours ago", hours).format(n=hours)
    days = hours // 24
    return ngettext("{n} day ago", "{n} days ago", days).format(n=days)


def format_percent(value: float | None) -> str:
    if value is None:
        return "—"
    if 0 < value < 1:
        return "<1%"
    return f"{round(value):d}%"


_CURRENCY = {"USD": "$", "EUR": "€", "GBP": "£", "CNY": "¥", "JPY": "¥"}


def format_amount(value: float | None, unit: str | None = None) -> str:
    """Format money (``unit`` is a currency code) or counts with a unit name."""
    if value is None:
        return "—"
    if unit and unit.upper() in _CURRENCY:
        symbol = _CURRENCY[unit.upper()]
        if abs(value) >= 1000:
            return f"{symbol}{value:,.0f}"
        return f"{symbol}{value:,.2f}"
    if abs(value - round(value)) < 1e-9:
        text = f"{round(value):,}"
    elif abs(value) >= 100:
        text = f"{value:,.0f}"
    else:
        text = f"{value:,.1f}"
    return f"{text} {unit}" if unit else text


def format_used_of(used: float | None, limit: float | None, unit: str | None) -> str | None:
    """``"340 / 1,000 credits"`` or ``"$4.20 / $20.00"``."""
    if used is None:
        return None
    if limit is None:
        return format_amount(used, unit)
    if unit and unit.upper() in _CURRENCY:
        return f"{format_amount(used, unit)} / {format_amount(limit, unit)}"
    used_text = format_amount(used, None)
    limit_text = format_amount(limit, unit)
    return f"{used_text} / {limit_text}"
