"""View models shared by every surface (window, widget, tray, Shell extension).

Keeping formatting here means the top bar, the desktop widget and the
main window always agree on labels, percentages, colours and countdowns.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime

from quotaglance import __version__
from quotaglance.i18n import _
from quotaglance.models import ProviderSnapshot, Severity, Status, UsageWindow, severity_for
from quotaglance.timefmt import (
    format_absolute,
    format_ago,
    format_countdown,
    format_percent,
    format_reset,
    format_used_of,
)


@dataclass
class WindowView:
    id: str
    label: str
    percent: float | None
    percent_text: str
    fraction: float
    severity: int
    countdown: str | None
    reset_text: str | None
    reset_at_text: str | None
    resets_at: str | None
    amount_text: str | None
    detail: str | None

    @property
    def has_meter(self) -> bool:
        return self.percent is not None


@dataclass
class ProviderView:
    id: str
    name: str
    short: str
    color: str
    status: str
    plan: str | None
    account: str | None
    source: str | None
    meta_text: str
    message: str | None
    hint: str | None
    loading: bool
    windows: list[WindowView] = field(default_factory=list)
    peak: WindowView | None = None

    @property
    def is_error(self) -> bool:
        return self.status in (Status.ERROR.value, Status.NOT_CONFIGURED.value)

    @property
    def is_stale(self) -> bool:
        return self.status in (Status.STALE.value, Status.ERROR.value) and bool(self.windows)


def window_view(window: UsageWindow, now: datetime) -> WindowView:
    window = window.effective(now)
    percent = window.used_percent
    severity = severity_for(percent)
    amount = format_used_of(window.used, window.limit, window.unit)
    return WindowView(
        id=window.id,
        label=window.label,
        percent=percent,
        percent_text=format_percent(percent) if percent is not None else (amount or "—"),
        fraction=max(0.0, min(1.0, (percent or 0.0) / 100.0)),
        severity=int(severity),
        countdown=format_countdown(window.resets_at, now),
        reset_text=format_reset(window.resets_at, now),
        reset_at_text=format_absolute(window.resets_at, now),
        resets_at=window.resets_at.isoformat() if window.resets_at else None,
        amount_text=amount,
        detail=window.detail,
    )


def provider_view(provider, snapshot: ProviderSnapshot | None, now: datetime,
                  loading: bool = False) -> ProviderView:
    if snapshot is None:
        return ProviderView(
            id=provider.id, name=provider.name, short=provider.short, color=provider.color,
            status=Status.LOADING.value, plan=None, account=None, source=None,
            meta_text=_("Checking…") if loading else _("Waiting for first update"),
            message=None, hint=None, loading=loading)
    windows = [window_view(w, now) for w in snapshot.windows]
    metered = [w for w in windows if w.percent is not None]
    peak = max(metered, key=lambda w: w.percent or 0.0) if metered else None
    when = snapshot.observed_at or snapshot.fetched_at
    meta_parts = []
    if snapshot.source:
        meta_parts.append(snapshot.source)
    if snapshot.status in (Status.OK, Status.STALE, Status.ERROR) and windows:
        meta_parts.append(format_ago(when, now))
    meta = " · ".join(meta_parts)
    return ProviderView(
        id=provider.id, name=provider.name, short=provider.short, color=provider.color,
        status=snapshot.status.value, plan=snapshot.plan, account=snapshot.account,
        source=snapshot.source, meta_text=meta, message=snapshot.message,
        hint=snapshot.hint, loading=loading, windows=windows, peak=peak)


@dataclass
class Headline:
    provider_id: str
    provider_name: str
    color: str
    window: WindowView

    @property
    def severity(self) -> Severity:
        return Severity(self.window.severity)


def headline(views: list[ProviderView], provider_id: str | None = None) -> Headline | None:
    """The most constrained meter across providers (or for one provider)."""
    best: Headline | None = None
    for view in views:
        if provider_id and view.id != provider_id:
            continue
        if view.peak is None or view.status == Status.NOT_CONFIGURED.value:
            continue
        if best is None or (view.peak.percent or 0) > (best.window.percent or 0):
            best = Headline(view.id, view.name, view.color, view.peak)
    return best


def build_views(engine, now: datetime) -> list[ProviderView]:
    return [provider_view(p, s, now, loading=p.id in engine.in_flight)
            for p, s in engine.visible_snapshots()]


def dbus_payload(engine, config, now: datetime) -> str:
    """JSON document consumed by the GNOME Shell extension."""
    views = build_views(engine, now)
    mode = config.get("panel.mode", "highest")
    head = headline(views, None if mode == "highest" else mode)
    updated = engine.last_updated()
    payload = {
        "version": __version__,
        "generated_at": now.isoformat(),
        "updated_text": _("Updated {ago}").format(ago=format_ago(updated, now)) if updated
        else _("Not updated yet"),
        "refreshing": bool(engine.in_flight),
        "demo": engine.demo,
        "panel": {
            "show_percent": bool(config.get("panel.show_percent", True)),
            "percent": head.window.percent if head else None,
            "percent_text": head.window.percent_text if head else "",
            "severity": head.window.severity if head else 0,
            "provider": head.provider_name if head else None,
            "color": head.color if head else None,
            "tooltip": (f"{head.provider_name} · {head.window.label} {head.window.percent_text}"
                        if head else _("QuotaGlance")),
        },
        "widget": {
            "visible": bool(config.get("widget.visible", False)),
            "pinned": bool(config.get("widget.pinned", False)),
            "position": config.get("widget.position"),
        },
        "providers": [asdict(view) for view in views],
    }
    return json.dumps(payload)
