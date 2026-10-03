"""OpenRouter: the API key's spending cap, account credits and free-model requests.

Source: the official ``GET /api/v1/key`` endpoint (the key's limit, its
daily/weekly/monthly reset, spend counters and the daily free-model request
quota) plus the best-effort ``GET /api/v1/credits`` (account balance, which
OpenRouter may reserve for management keys). The key can come from GNOME
Keyring, ``$OPENROUTER_API_KEY`` or OpenCode's ``auth.json``.
"""

from __future__ import annotations

import math
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
from quotaglance.util import UTC, clamp, to_float

API_URL = "https://openrouter.ai/api/v1"
ENV = ("OPENROUTER_API_KEY",)
HEADERS = {"HTTP-Referer": "https://github.com/rafay-ah/quotaglance", "X-Title": "QuotaGlance"}
KEY_FIELDS = ("limit", "limit_remaining", "usage", "usage_daily", "usage_weekly", "usage_monthly")
CAP_LABELS = {"daily": _("Daily cap"), "weekly": _("Weekly cap"), "monthly": _("Monthly cap")}
ORDER = {"key_limit": 0, "spend": 0, "credits": 1, "free_requests": 2}


def api_url(ctx: FetchContext) -> str:
    override = (ctx.env.get("OPENROUTER_API_URL") or "").strip().rstrip("/")
    return override if override.startswith("https://") else API_URL


def find_key(ctx: FetchContext) -> tuple[str | None, str]:
    """Return (key, where it came from)."""
    stored = ctx.secret("api_key", (), provider="openrouter")
    if stored:
        return stored, _("Keyring")
    for name in ENV:
        value = (ctx.env.get(name) or "").strip()
        if value:
            return value, f"${name}"
    key = opencode_keys(ctx).get("openrouter")
    if key:
        return key, _("OpenCode sign-in")
    return None, ""


def period_window(period: str | None, now: datetime) -> tuple[datetime | None, int | None]:
    """(next reset, length) of a UTC day, Monday-to-Sunday week or calendar month."""
    now = now.astimezone(UTC)
    today = datetime(now.year, now.month, now.day, tzinfo=UTC)
    if period == "daily":
        return today + timedelta(days=1), 86400
    if period == "weekly":
        return today + timedelta(days=7 - today.weekday()), 7 * 86400
    if period == "monthly":
        end = datetime(now.year + now.month // 12, now.month % 12 + 1, 1, tzinfo=UTC)
        return end, int((end - today.replace(day=1)).total_seconds())
    return None, None


def _number(data: dict[str, Any], field: str) -> float | None:
    """A finite JSON number or null; anything else means the payload is malformed."""
    value = data.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ProviderError(_("OpenRouter sent an invalid {field}").format(field=field))
    return float(value)


def plan_label(data: dict[str, Any]) -> str:
    plan = _("Free tier") if data.get("is_free_tier") is True else _("Pay as you go")
    if data.get("is_management_key") is True or data.get("is_provisioning_key") is True:
        plan = f"{plan} · {_('management key')}"
    return plan


def parse_key(payload: Any, now: datetime) -> tuple[list[UsageWindow], str | None]:
    """Windows from ``/key``: the spending cap (or spend so far) and free-model requests."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ProviderError(_("Unexpected response from OpenRouter"))
    numbers = {field: _number(data, field) for field in KEY_FIELDS}
    period = data.get("limit_reset")
    if period is not None and not isinstance(period, str):
        raise ProviderError(_("OpenRouter sent an invalid {field}").format(field="limit_reset"))
    period = (period or "").strip().lower() or None
    plan = plan_label(data)
    if data.get("is_management_key") is True or data.get("is_provisioning_key") is True:
        return [], plan  # management keys report zero spend of their own

    windows: list[UsageWindow] = []
    limit = numbers["limit"]
    if limit is not None and limit > 0:
        # A spending cap is not a balance: prefer the server's remaining amount.
        if numbers["limit_remaining"] is not None:
            used = limit - clamp(numbers["limit_remaining"], 0, limit)
        elif period in CAP_LABELS and numbers[f"usage_{period}"] is not None:
            used = numbers[f"usage_{period}"]
        else:
            used = numbers["usage"]
        if used is not None:
            resets_at, seconds = period_window(period, now)
            windows.append(UsageWindow(
                id="key_limit", label=CAP_LABELS.get(period or "", _("Key limit")),
                used_percent=clamp(used / limit * 100, 0, 100), resets_at=resets_at,
                window_seconds=seconds, used=used, limit=limit, unit="USD",
                detail=_("{amount} left").format(
                    amount=format_amount(max(0.0, limit - used), "USD"))))
    else:
        monthly = numbers["usage_monthly"]
        spent = monthly if monthly is not None else numbers["usage"]
        if spent is not None:
            spent = max(0.0, spent)
            windows.append(UsageWindow(
                id="spend", label=_("This month") if monthly is not None else _("Key spend"),
                resets_at=period_window("monthly", now)[0] if monthly is not None else None,
                used=spent, unit="USD",
                detail=_("{amount} · no limit set").format(amount=format_amount(spent, "USD"))))

    free = data.get("free_model_daily_requests")
    if isinstance(free, dict):
        free_limit = to_float(free.get("limit"))
        free_used = to_float(free.get("used"))
        free_left = to_float(free.get("remaining"))
        if free_used is None and free_limit is not None and free_left is not None:
            free_used = free_limit - free_left
        if free_limit and free_limit > 0 and free_used is not None:
            resets_at, seconds = period_window("daily", now)
            windows.append(UsageWindow(
                id="free_requests", label=_("Free models"),
                used_percent=clamp(free_used / free_limit * 100, 0, 100), resets_at=resets_at,
                window_seconds=seconds, used=max(0.0, free_used), limit=free_limit,
                unit="requests"))
    return windows, plan


def parse_credits(payload: Any) -> UsageWindow:
    """Purchased credits and total spend from ``/credits`` (USD numbers)."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ProviderError(_("Unexpected response from OpenRouter"))
    total = _number(data, "total_credits")
    used = _number(data, "total_usage")
    if total is None or used is None:
        raise ProviderError(_("OpenRouter didn't report credits"))
    return UsageWindow(
        id="credits", label=_("Credits"),
        used_percent=clamp(used / total * 100, 0, 100) if total > 0 else None,
        used=used, limit=total if total > 0 else None, unit="USD",
        detail=_("{amount} left").format(amount=format_amount(max(0.0, total - used), "USD")))


class OpenRouterProvider(Provider):
    id = "openrouter"
    name = "OpenRouter"
    short = "OR"
    color = "#6366F1"
    category = "api"
    homepage = "https://openrouter.ai/settings/credits"
    source_summary = _("OpenRouter key and credits API (API key)")
    setup_hint = _("Create an API key at openrouter.ai → Settings → API Keys, and paste it below.")
    settings = (api_key_setting(ENV),)

    def detect(self, ctx: FetchContext) -> bool:
        return find_key(ctx)[0] is not None

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        key, origin = find_key(ctx)
        if not key:
            raise NotConfigured(_("No OpenRouter API key found"), self.setup_hint)
        base = api_url(ctx)
        headers = {**HEADERS, "Authorization": f"Bearer {key}"}
        windows: list[UsageWindow] = []
        plan = None
        message = None
        errors: list[Exception] = []
        try:
            windows, plan = parse_key(ctx.http.get_json(f"{base}/key", headers=headers),
                                      ctx.now())
        except HttpError as exc:
            if exc.status in (401, 403):
                raise AuthError(_("OpenRouter rejected the API key"), self.setup_hint) from None
            errors.append(from_http_error(exc, login_hint=self.setup_hint, service=self.name))
        except (NetworkError, ProviderError) as exc:
            errors.append(exc)
        # Each endpoint degrades on its own: a missing balance keeps the key's numbers.
        try:
            windows.append(parse_credits(ctx.http.get_json(f"{base}/credits", headers=headers)))
        except HttpError as exc:
            if exc.status in (401, 403):
                message = _("The account balance needs a management key")
            errors.append(from_http_error(exc, login_hint=self.setup_hint, service=self.name))
        except (NetworkError, ProviderError) as exc:
            errors.append(exc)
        if not windows:
            raise errors[0] if errors else ProviderError(
                _("OpenRouter reported no usage for this key"))
        windows.sort(key=lambda window: ORDER.get(window.id, len(ORDER)))
        return self.snapshot(ctx, windows, plan=plan, source=origin or None, message=message)
