"""Poe: the account's point balance, plus what the last 30 days cost.

Source: the official Usage API with your API key: ``GET
/usage/current_balance`` for the points left and ``GET
/usage/points_history`` (30 days kept, paged) for recent spend. Poe exposes
no allotment or reset date, so there is no percentage. The key can come
from GNOME Keyring, ``$POE_API_KEY`` or OpenCode's ``auth.json``.
"""

from __future__ import annotations

import urllib.parse
from datetime import datetime, timedelta
from typing import Any

from quotaglance.i18n import _, ngettext
from quotaglance.models import ProviderSnapshot, UsageWindow
from quotaglance.net import HttpError, NetworkError
from quotaglance.providers.base import (
    AuthError,
    FetchContext,
    NotConfigured,
    Provider,
    ProviderError,
    api_key_setting,
    from_http_error,
)
from quotaglance.providers.keysources import opencode_keys
from quotaglance.timefmt import format_amount
from quotaglance.util import UTC, first, parse_time, to_float

BALANCE_URL = "https://api.poe.com/usage/current_balance"
HISTORY_URL = "https://api.poe.com/usage/points_history"
ENV = ("POE_API_KEY",)
HISTORY_DAYS = 30
MAX_PAGES = 5


def find_key(ctx: FetchContext) -> tuple[str | None, str]:
    """Return (key, where it came from)."""
    stored = ctx.secret("api_key", (), provider="poe")
    if stored:
        return stored, _("Keyring")
    for name in ENV:
        value = (ctx.env.get(name) or "").strip()
        if value:
            return value, f"${name}"
    key = opencode_keys(ctx).get("poe")
    if key:
        return key, _("OpenCode sign-in")
    return None, ""


def _points(value: float) -> str:
    return _("{n} points").format(n=format_amount(value))


def parse_balance(payload: Any) -> UsageWindow | None:
    """``{"current_point_balance": 742300}``; a missing balance is not an error."""
    if not isinstance(payload, dict):
        raise ProviderError(_("Unexpected response from Poe"))
    value = payload.get("current_point_balance")
    if value is None:
        return None
    balance = to_float(value) if isinstance(value, (int, float, str)) else None
    if balance is None:
        raise ProviderError(_("Poe sent a non-numeric point balance"))
    return UsageWindow(id="balance", label=_("Points"), used=balance, unit="points",
                       detail=_("{points} left").format(points=_points(balance)))


def history_rows(payload: Any) -> list[dict[str, Any]]:
    """The rows of one ``points_history`` page, newest first."""
    rows = first(payload, "data", "items", "results") if isinstance(payload, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _row_time(row: dict[str, Any]) -> datetime | None:
    # ``creation_time`` is in microseconds; parse_time tells µs/ms/s apart by size.
    return parse_time(first(row, "creation_time", "timestamp", "created_at"))


def summarize_history(rows: list[dict[str, Any]], now: datetime) -> list[UsageWindow]:
    """Points spent today (UTC) and over the 30 days Poe keeps."""
    now = now.astimezone(UTC)
    cutoff = now - timedelta(days=HISTORY_DAYS)
    today = now.date()
    spent = {"today": [0.0, 0], "month": [0.0, 0]}
    bots: dict[str, float] = {}
    for row in rows:
        when = _row_time(row)
        if when is None or when < cutoff or when > now + timedelta(days=1):
            continue  # unreadable or out-of-range rows are skipped, not fatal
        points = max(0.0, to_float(first(row, "cost_points", "points", "point_cost")) or 0.0)
        for period in (("today", "month") if when.date() == today else ("month",)):
            spent[period][0] += points
            spent[period][1] += 1
        bot = str(row.get("bot_name") or "").strip() or _("unknown")
        bots[bot] = bots.get(bot, 0.0) + points

    def detail(period: str) -> str:
        points, requests = spent[period]
        return f"{_points(points)} · " + ngettext("{n} request", "{n} requests",
                                                  requests).format(n=requests)

    month = detail("month")
    if bots:
        top = min(bots, key=lambda name: (-bots[name], name))
        month += " · " + _("top {bot}").format(bot=top)
    return [
        UsageWindow(id="spend_today", label=_("Today"), window_seconds=86400,
                    used=spent["today"][0], unit="points", detail=detail("today")),
        UsageWindow(id="spend_30d", label=_("Last 30 days"), window_seconds=HISTORY_DAYS * 86400,
                    used=spent["month"][0], unit="points", detail=month),
    ]


def fetch_history(ctx: FetchContext, headers: dict[str, str]) -> list[dict[str, Any]] | None:
    """Up to five pages of history; None when it can't be read (it never hides the balance)."""
    rows: list[dict[str, Any]] = []
    cutoff = ctx.now() - timedelta(days=HISTORY_DAYS)
    cursor = None
    for _page in range(MAX_PAGES):
        url = f"{HISTORY_URL}?limit=100"
        if cursor:
            url += "&starting_after=" + urllib.parse.quote(cursor, safe="")
        try:
            payload = ctx.http.get_json(url, headers=headers)
        except (HttpError, NetworkError):
            return None
        if not isinstance(payload, dict):
            return None
        page = history_rows(payload)
        rows.extend(page)
        cursor = payload.get("next_cursor") if isinstance(payload.get("next_cursor"), str) \
            else None
        if not cursor and payload.get("has_more") is True and page:
            cursor = page[-1].get("query_id") if isinstance(page[-1].get("query_id"), str) \
                else None
        oldest = _row_time(page[-1]) if page else None
        if not cursor or not cursor.strip() or (oldest and oldest < cutoff):
            break
        cursor = cursor.strip()
    return rows


class PoeProvider(Provider):
    id = "poe"
    name = "Poe"
    short = "Po"
    color = "#5D5CDE"
    category = "api"
    homepage = "https://poe.com/api_key"
    source_summary = _("Poe usage API (API key)")
    setup_hint = _("Copy your API key from poe.com/api_key, and paste it below.")
    settings = (api_key_setting(ENV),)

    def detect(self, ctx: FetchContext) -> bool:
        return find_key(ctx)[0] is not None

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        key, origin = find_key(ctx)
        if not key:
            raise NotConfigured(_("No Poe API key found"), self.setup_hint)
        headers = {"Authorization": f"Bearer {key}"}
        try:
            payload = ctx.http.get_json(BALANCE_URL, headers=headers)
        except HttpError as exc:
            if exc.status in (401, 403):
                raise AuthError(_("Poe rejected the API key"), self.setup_hint) from None
            raise from_http_error(exc, login_hint=self.setup_hint, service=self.name) from None
        balance = parse_balance(payload)
        windows = [balance] if balance else []
        rows = fetch_history(ctx, headers)
        if rows is not None:
            windows += summarize_history(rows, ctx.now())
        if not windows:
            raise ProviderError(_("Poe didn't report a point balance"))
        return self.snapshot(ctx, windows, plan=_("Points balance"), source=origin or None)
