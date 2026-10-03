"""Synthetic (synthetic.new): rolling 5-hour requests, weekly credits and search.

Source: the official ``GET /v2/quotas`` endpoint with your API key (calls to
it don't count against your limits). The 5-hour and weekly pools refill
gradually rather than resetting at once, so their reset time is when they
would be full again. The key can come from GNOME Keyring,
``$SYNTHETIC_API_KEY`` or OpenCode's ``auth.json``.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from typing import Any

from quotaglance.i18n import _
from quotaglance.models import ProviderSnapshot, UsageWindow
from quotaglance.net import HttpError
from quotaglance.providers.base import (
    AuthError,
    FetchContext,
    NotConfigured,
    Provider,
    ProviderError,
    api_key_setting,
    from_http_error,
)
from quotaglance.util import clamp, first, parse_time, read_json, to_float

QUOTAS_URL = "https://api.synthetic.new/v2/quotas"
ENV = ("SYNTHETIC_API_KEY",)
TICK_SECONDS = 15 * 60  # the 5-hour pool gets tickPercent back about every 15 minutes
REGEN_SECONDS = 168 * 3600 / 50  # the weekly pool gets ~2% back every 3.36 hours
PLAN_KEYS = ("plan", "planName", "plan_name", "subscription", "subscriptionPlan", "tier",
             "package", "packageName")
# Older or alternative payloads: generic quota objects.
LIMIT_KEYS = ("limit", "max", "total", "quota", "capacity", "allowance")
USED_KEYS = ("used", "usage", "requests", "consumed", "spent")
REMAINING_KEYS = ("remaining", "left", "available")
RESET_KEYS = ("resetAt", "reset_at", "resetsAt", "resets_at", "renewsAt", "renews_at",
              "periodEnd", "period_end", "expiresAt", "expires_at")


def _opencode_keys(ctx: FetchContext) -> dict[str, str]:
    """API keys saved by ``opencode auth login``, by provider id (read-only)."""
    path = ctx.data_home / "opencode" / "auth.json"
    try:
        data = read_json(path) if path.is_file() else {}
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        return {}
    return {name: entry["key"].strip() for name, entry in data.items()
            if isinstance(entry, dict) and isinstance(entry.get("key"), str)
            and entry["key"].strip()}


def find_key(ctx: FetchContext) -> tuple[str | None, str]:
    """Return (key, where it came from)."""
    stored = ctx.secret("api_key", (), provider="synthetic")
    if stored:
        return stored, _("Keyring")
    for name in ENV:
        value = (ctx.env.get(name) or "").strip().strip("\"'").strip()
        if value:
            return value, f"${name}"
    key = _opencode_keys(ctx).get("synthetic")
    if key:
        return key, _("OpenCode sign-in")
    return None, ""


def _full_at(next_refill: datetime | None, used_percent: float, step_percent: float,
             step_seconds: float) -> datetime | None:
    """When a gradually refilling pool is full again (``next_refill`` is the first step)."""
    if next_refill is None or used_percent <= 0 or step_percent <= 0:
        return None
    steps = math.ceil(round(used_percent / step_percent, 6))
    return next_refill + timedelta(seconds=(steps - 1) * step_seconds)


def _rolling(slot: dict[str, Any]) -> UsageWindow | None:
    limit, remaining = to_float(slot.get("max")), to_float(slot.get("remaining"))
    if not limit or limit <= 0 or remaining is None:
        return None
    used = clamp(limit - remaining, 0, limit)
    percent = 100.0 if slot.get("limited") is True else used / limit * 100
    tick = to_float(slot.get("tickPercent")) or 0.0
    tick = tick * 100 if tick <= 1 else tick  # a 0..1 fraction (0.05 = 5%)
    return UsageWindow(
        id="session", label=_("5-hour"), used_percent=percent,
        resets_at=_full_at(parse_time(slot.get("nextTickAt")), percent, tick, TICK_SECONDS),
        window_seconds=5 * 3600, used=used, limit=limit, unit="requests")


def _weekly(slot: dict[str, Any]) -> UsageWindow | None:
    limit = to_float(slot.get("maxCredits"))  # "$36.00"
    remaining = to_float(slot.get("remainingCredits"))
    if limit and limit > 0 and remaining is not None:
        used = clamp(limit - remaining, 0, limit)
        percent = used / limit * 100
    else:
        left = to_float(slot.get("percentRemaining"))
        if left is None:
            return None
        used, percent = None, clamp(100 - left, 0, 100)
    regen = to_float(slot.get("nextRegenCredits"))
    step = regen / limit * 100 if regen and limit else 2.0
    return UsageWindow(
        id="weekly", label=_("Weekly"), used_percent=percent,
        resets_at=_full_at(parse_time(slot.get("nextRegenAt")), percent, step, REGEN_SECONDS),
        window_seconds=7 * 86400, used=used, limit=limit if used is not None else None,
        unit="USD" if used is not None else None)


def _generic(item: dict[str, Any], wid: str, label: str,
             seconds: int | None = None) -> UsageWindow | None:
    """A plain quota object: ``search.hourly``, the legacy ``subscription``, ``quotas[]``."""
    limit = to_float(first(item, *LIMIT_KEYS))
    used = to_float(first(item, *USED_KEYS))
    remaining = to_float(first(item, *REMAINING_KEYS))
    if limit is None and used is not None and remaining is not None:
        limit = used + remaining
    if used is None and limit is not None and remaining is not None:
        used = limit - remaining
    counted = bool(limit and limit > 0 and used is not None)
    percent = to_float(first(item, "percentUsed", "usedPercent", "used_percent"))
    left = to_float(first(item, "percentRemaining", "remainingPercent", "remaining_percent"))
    if percent is None and left is not None:
        percent = 100 - left
    if percent is None and counted:
        percent = used / limit * 100
    if percent is None:
        return None
    minutes = to_float(first(item, "windowMinutes", "window_minutes"))
    return UsageWindow(
        id=wid, label=label, used_percent=clamp(percent, 0, 100),
        resets_at=parse_time(first(item, *RESET_KEYS)),
        window_seconds=seconds or (int(minutes * 60) if minutes else None),
        used=used if counted else None, limit=limit if counted else None,
        unit="requests" if counted else None)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "quota"


def _slot(sources: tuple[dict[str, Any], ...], *path: str) -> Any:
    """The first value at ``path`` in the payload root or its ``data`` wrapper."""
    for source in sources:
        value: Any = source
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if value is not None:
            return value
    return None


def parse_quotas(payload: Any) -> tuple[list[UsageWindow], str | None]:
    root = {"quotas": payload} if isinstance(payload, list) else payload
    if not isinstance(root, dict):
        raise ProviderError(_("Unexpected response from Synthetic"))
    sources = (root, root["data"]) if isinstance(root.get("data"), dict) else (root,)
    rolling = _slot(sources, "rollingFiveHourLimit")
    weekly = _slot(sources, "weeklyTokenLimit")
    search = _slot(sources, "search", "hourly")
    legacy = _slot(sources, "subscription")
    windows: list[UsageWindow | None] = []
    if any(isinstance(slot, dict) for slot in (rolling, weekly, search)):
        windows = [_rolling(rolling) if isinstance(rolling, dict) else None,
                   _weekly(weekly) if isinstance(weekly, dict) else None,
                   _generic(search, "search", _("Search"), 3600)
                   if isinstance(search, dict) else None]
    elif isinstance(legacy, dict):
        # Before the rolling pool, the subscription was the 5-hour request quota.
        windows = [_generic(legacy, "session", _("5-hour"), 5 * 3600)]
    else:
        items = _slot(sources, "quotas")
        items = [item for item in items if isinstance(item, dict)] \
            if isinstance(items, list) else []
        for index, item in enumerate(items, 1):
            name = str(first(item, "name", "label", "type", "period", "title") or "").strip()
            windows.append(_generic(item, _slug(name) if name else f"quota_{index}",
                                    name[:12] or _("Quota {n}").format(n=index)))
    result = [window for window in windows if window]
    if not result:
        raise ProviderError(_("Synthetic returned no quota data"))
    plan = None
    for key in PLAN_KEYS:
        value = _slot(sources, key)
        if isinstance(value, str) and value.strip():
            plan = value.strip()
            break
    return result, plan or _("Subscription")


class SyntheticProvider(Provider):
    id = "synthetic"
    name = "Synthetic"
    short = "Sy"
    color = "#22C55E"
    category = "api"
    homepage = "https://synthetic.new"
    source_summary = _("Synthetic quotas API (API key)")
    setup_hint = _("Create an API key in your synthetic.new account (see "
                   "dev.synthetic.new/docs/api/getting-started), and paste it below.")
    settings = (api_key_setting(ENV),)

    def detect(self, ctx: FetchContext) -> bool:
        return find_key(ctx)[0] is not None

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        key, origin = find_key(ctx)
        if not key:
            raise NotConfigured(_("No Synthetic API key found"), self.setup_hint)
        try:
            payload = ctx.http.get_json(QUOTAS_URL, headers={"Authorization": f"Bearer {key}"})
        except HttpError as exc:
            if exc.status in (401, 403):
                raise AuthError(_("Synthetic rejected the API key"), self.setup_hint) from None
            raise from_http_error(exc, login_hint=self.setup_hint, service=self.name) from None
        windows, plan = parse_quotas(payload)
        return self.snapshot(ctx, windows, plan=plan, source=origin or None)
