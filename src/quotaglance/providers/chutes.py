"""Chutes: subscription 4-hour and monthly caps, or the daily request quota.

Source: the official ``/users/me/subscription_usage`` endpoint (usage valued
at pay-as-you-go prices against the plan's caps). Accounts without a
subscription fall back to their per-day request quota, and ``/users/me``
adds the pay-as-you-go balance. Use an admin API key or one scoped for user
info, from GNOME Keyring, ``$CHUTES_API_KEY`` or OpenCode's ``auth.json``.
"""

from __future__ import annotations

import urllib.parse
from datetime import datetime, timedelta
from typing import Any

from quotaglance.i18n import _
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
from quotaglance.util import UTC, clamp, parse_time, to_float

API_URL = "https://api.chutes.ai"
ENV = ("CHUTES_API_KEY",)
KEY_HINT = _("Use an admin API key, or one scoped for user info.")


def api_url(ctx: FetchContext) -> str:
    override = (ctx.env.get("CHUTES_API_URL") or "").strip().rstrip("/")
    return override if override.startswith("https://") else API_URL


def find_key(ctx: FetchContext) -> tuple[str | None, str]:
    """Return (key, where it came from)."""
    stored = ctx.secret("api_key", (), provider="chutes")
    if stored:
        return stored, _("Keyring")
    for name in ENV:
        value = (ctx.env.get(name) or "").strip()
        if value:
            return value, f"${name}"
    key = opencode_keys(ctx).get("chutes")
    if key:
        return key, _("OpenCode sign-in")
    return None, ""


def parse_subscription(data: Any) -> tuple[list[UsageWindow], str | None]:
    """Windows and plan from ``subscription_usage``; ``([], None)`` without a subscription."""
    if not isinstance(data, dict):
        raise ProviderError(_("Unexpected response from Chutes"))
    if data.get("subscription") is not True:
        return [], None
    windows: list[UsageWindow] = []
    # The 4-hour window is a fixed UTC bucket (00, 04, 08…); the month runs from the
    # subscription anchor. Usage is valued at pay-as-you-go prices, in USD.
    for wid, label, key, seconds in (("session", _("4-hour"), "four_hour", 4 * 3600),
                                     ("monthly", _("Monthly"), "monthly", 30 * 86400)):
        pool = data.get(key)
        if not isinstance(pool, dict):
            continue
        if pool.get("uncapped") is True:
            windows.append(UsageWindow(id=wid, label=label, detail=_("Uncapped")))
            continue
        usage, cap = to_float(pool.get("usage")), to_float(pool.get("cap"))
        if usage is None or not cap or cap <= 0:
            continue
        windows.append(UsageWindow(
            id=wid, label=label, used_percent=clamp(usage / cap * 100, 0, 100),
            resets_at=parse_time(pool.get("reset_at")), window_seconds=seconds,
            used=usage, limit=cap, unit="USD"))
    price = to_float(data.get("monthly_price"))
    plan = f"${price:g}/mo" if price else _("Subscription")
    if data.get("custom") is True:
        plan = f"{plan} {_('(custom)')}"
    return windows, plan


def quota_rows(data: Any) -> list[tuple[str, float | None]]:
    """(chute id, daily request quota) pairs; ``*`` means every chute."""
    if isinstance(data, list):
        return [(str(row.get("chute_id") or "*"), to_float(row.get("quota")))
                for row in data if isinstance(row, dict)]
    if isinstance(data, dict):  # ``{"*": 0}`` is the server default, ``{}`` means none
        return [(str(chute), to_float(quota)) for chute, quota in data.items()]
    return []


def parse_quota_usage(data: Any, now: datetime) -> UsageWindow | None:
    """Today's requests against the quota; the counter rolls over at 00:00 UTC."""
    if not isinstance(data, dict):
        return None
    quota = data.get("quota")
    if isinstance(quota, str) and quota.strip().lower() == "unlimited":
        return UsageWindow(id="daily_requests", label=_("Daily"), detail=_("Unlimited"))
    limit, used = to_float(quota), to_float(data.get("used"))
    if not limit or limit <= 0 or used is None:
        return None
    now = now.astimezone(UTC)
    midnight = datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1)
    return UsageWindow(
        id="daily_requests", label=_("Daily"), used_percent=clamp(used / limit * 100, 0, 100),
        resets_at=midnight, window_seconds=86400, used=used, limit=limit, unit="requests")


def parse_balance(data: Any) -> UsageWindow | None:
    """The pay-as-you-go balance (USD) from ``/users/me``."""
    balance = to_float(data.get("balance")) if isinstance(data, dict) else None
    if balance is None:
        return None
    return UsageWindow(id="balance", label=_("Balance"), used=balance, unit="USD",
                       detail=_("{amount} left").format(amount=format_amount(balance, "USD")))


def _optional(ctx: FetchContext, url: str, headers: dict[str, str]) -> Any:
    """Extra endpoints may fail without hiding the subscription numbers."""
    try:
        return ctx.http.get_json(url, headers=headers)
    except (HttpError, NetworkError):
        return None


class ChutesProvider(Provider):
    id = "chutes"
    name = "Chutes"
    short = "Ch"
    color = "#FB7185"
    category = "api"
    homepage = "https://chutes.ai"
    source_summary = _("Chutes subscription and quota API (API key)")
    setup_hint = _("Create an API key at chutes.ai (an admin key, or one allowed to read "
                   "user info), and paste it below.")
    settings = (api_key_setting(ENV),)

    def detect(self, ctx: FetchContext) -> bool:
        return find_key(ctx)[0] is not None

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        key, origin = find_key(ctx)
        if not key:
            raise NotConfigured(_("No Chutes API key found"), self.setup_hint)
        base = api_url(ctx)
        headers = {"Authorization": f"Bearer {key}"}
        try:
            usage = ctx.http.get_json(f"{base}/users/me/subscription_usage", headers=headers)
        except HttpError as exc:
            if exc.status in (401, 403):
                raise AuthError(_("Chutes rejected the API key"), KEY_HINT) from None
            raise from_http_error(exc, login_hint=KEY_HINT, service=self.name) from None
        windows, plan = parse_subscription(usage)
        if plan is None:
            plan = _("Pay as you go")
            rows = quota_rows(_optional(ctx, f"{base}/users/me/quotas", headers))
            # The wildcard row covers every chute; otherwise show the first quota.
            row = next((r for r in rows if r[0] == "*" and r[1]), None) or \
                next((r for r in rows if r[1]), None)
            if row:
                chute = urllib.parse.quote(row[0], safe="")
                daily = parse_quota_usage(_optional(
                    ctx, f"{base}/users/me/quota_usage/{chute}", headers), ctx.now())
                if daily:
                    windows.append(daily)
        balance = parse_balance(_optional(ctx, f"{base}/users/me", headers))
        if balance:
            windows.append(balance)
        if not windows:
            raise ProviderError(_("Chutes reported no subscription or quota for this account"))
        return self.snapshot(ctx, windows, plan=plan, source=origin or None)
