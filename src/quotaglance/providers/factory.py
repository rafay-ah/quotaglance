"""Factory (Droid): 5-hour, weekly and monthly limits, or legacy token pools.

Source: Factory's billing API, called with a Factory API key (``fk-…``)
from GNOME Keyring, ``$FACTORY_API_KEY`` or ``~/.factory/.env``. Droid's
own sign-in is deliberately left alone: it is encrypted, and refreshing it
would rotate its tokens and sign you out of Droid.
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
    SettingSpec,
    api_key_setting,
    from_http_error,
)
from quotaglance.util import clamp, dig, parse_time, title_case_plan, to_float

API_BASE = "https://api.factory.ai"
APP_BASE = "https://app.factory.ai"
EU_API_BASE = "https://api.eu.factory.ai"
HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://app.factory.ai",
    "Referer": "https://app.factory.ai/",
    "x-factory-client": "web-app",
}
KEY_HINT = _("Create a new API key at app.factory.ai/settings/api-keys and paste it in "
             "Preferences.")
UNLIMITED_TOKENS = 1e12  # legacy allowances above this mean "unlimited"

# (API key, window id, label, length) of the token-rate-limits windows.
LIMIT_WINDOWS = (
    ("fiveHour", "session", _("5-hour"), 5 * 3600),
    ("weekly", "weekly", _("Weekly"), 7 * 86400),
    ("monthly", "monthly", _("Monthly"), None),
)
CORE_LABELS = {"session": _("Core 5-hour"), "weekly": _("Core weekly"),
               "monthly": _("Core monthly")}


# -- API key ------------------------------------------------------------------------


def key_from_dotenv(text: str) -> str | None:
    """``FACTORY_API_KEY`` from a dotenv file (``export`` and quotes allowed)."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        name, sep, value = line.partition("=")
        if not sep or name.strip() != "FACTORY_API_KEY":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1].strip()
        return value or None
    return None


def find_key(ctx: FetchContext) -> tuple[str | None, str]:
    """Return (key, where it came from)."""
    stored = ctx.secret("api_key", (), provider="factory")
    if stored:
        return stored, _("Keyring")
    env = (ctx.env.get("FACTORY_API_KEY") or "").strip()
    if env:
        return env, "$FACTORY_API_KEY"
    try:
        key = key_from_dotenv(ctx.path("~/.factory/.env").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        key = None
    if key:
        return key, "~/.factory/.env"
    return None, ""


# -- parsing ------------------------------------------------------------------------


def _limit_window(item: Any, window_id: str, label: str, seconds: int | None,
                  now: datetime) -> UsageWindow | None:
    if not isinstance(item, dict):
        return None
    used = to_float(item.get("usedPercent"))
    if used is None:
        return None
    remaining = to_float(item.get("secondsRemaining"))
    end = parse_time(item.get("windowEnd"))
    if remaining is not None and remaining > 0:
        resets_at = now + timedelta(seconds=remaining)
    elif end is not None and end > now:
        resets_at = end
    else:
        resets_at = None
        if end is not None and remaining is None:
            used = 0.0  # the window has rolled over; Factory leaves the old value behind
    return UsageWindow(id=window_id, label=label, used_percent=clamp(used, 0, 100),
                       resets_at=resets_at, window_seconds=seconds)


def _pool_windows(pool: dict[str, Any], now: datetime, core: bool = False
                  ) -> list[UsageWindow]:
    windows = []
    for key, window_id, label, seconds in LIMIT_WINDOWS:
        if core:
            window_id, label = f"core_{window_id}", CORE_LABELS[window_id]
        window = _limit_window(pool.get(key), window_id, label, seconds, now)
        if window:
            windows.append(window)
    return windows


def _has_usage(pool: dict[str, Any]) -> bool:
    return any(isinstance(item, dict) and ((to_float(item.get("usedPercent")) or 0) > 0
                                           or item.get("windowEnd") is not None
                                           or item.get("secondsRemaining") is not None)
               for item in pool.values())


def parse_billing_limits(data: Any, now: datetime) -> list[UsageWindow] | None:
    """``/api/billing/limits`` windows, or None when the account is on legacy billing."""
    if not isinstance(data, dict) or not data.get("usesTokenRateLimitsBilling"):
        return None
    standard = dig(data, "limits", "standard")
    # Without the standard pool Droid itself shows "Unable to fetch credit limits".
    windows = _pool_windows(standard, now) if isinstance(standard, dict) else []
    if not windows:
        return None
    core = dig(data, "limits", "core")
    if isinstance(core, dict) and _has_usage(core):  # the Droid Core pool, once used
        windows += _pool_windows(core, now, core=True)
    cents = to_float(data.get("extraUsageBalanceCents"))
    if cents or data.get("extraUsageAllowed"):
        windows.append(UsageWindow(id="extra_usage", label=_("Extra usage"),
                                   used=(cents or 0.0) / 100, unit="USD",
                                   detail=_("Prepaid balance")))
    return windows


def token_percent(used: float, allowance: float, ratio: float | None) -> float | None:
    """Legacy pool usage (CodexBar's rules), or None for unlimited allowances."""
    reliable = 0 < allowance <= UNLIMITED_TOKENS
    if ratio is not None and not (ratio == 0 and used > 0 and reliable):
        if -0.001 <= ratio <= 1.001:
            return clamp(ratio * 100, 0, 100)
        if not reliable and -0.1 <= ratio <= 100.1:
            return clamp(ratio, 0, 100)  # already a percentage
    if allowance > UNLIMITED_TOKENS:
        return None  # a meter against a made-up scale would only trigger false alerts
    if allowance <= 0:
        return 0.0
    return min(100.0, used / allowance * 100)


def parse_legacy_usage(data: Any) -> list[UsageWindow]:
    """``/api/organization/subscription/usage``: Standard and Premium token pools."""
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        raise ProviderError(_("Unexpected response from Factory"))
    start, end = parse_time(usage.get("startDate")), parse_time(usage.get("endDate"))
    span = int((end - start).total_seconds()) if start and end and end > start else None
    windows = []
    for key, label in (("standard", _("Standard")), ("premium", _("Premium"))):
        pool = usage.get(key)
        if not isinstance(pool, dict):
            continue
        tokens = max(0.0, to_float(pool.get("userTokens")) or 0.0)
        allowance = to_float(pool.get("totalAllowance")) or 0.0
        unlimited = allowance > UNLIMITED_TOKENS
        windows.append(UsageWindow(
            id=key, label=label,
            used_percent=token_percent(tokens, allowance, to_float(pool.get("usedRatio"))),
            resets_at=end, window_seconds=span, used=tokens,
            limit=allowance if 0 < allowance <= UNLIMITED_TOKENS else None, unit="tokens",
            detail=_("Unlimited") if unlimited else None))
    if not windows:
        raise ProviderError(_("Factory reported no token usage"))
    return windows


def plan_label(me: dict[str, Any], overage: str | None = None) -> str | None:
    """Tier and plan (e.g. "Factory Enterprise - Pro") plus Droid's fallback preference."""
    subscription = dig(me, "organization", "subscription")
    if not isinstance(subscription, dict):
        subscription = {}
    tier = title_case_plan(subscription.get("factoryTier"))
    name = dig(subscription, "orbSubscription", "plan", "name")
    parts = [f"Factory {tier}"] if tier else []
    if isinstance(name, str) and name.strip() and "factory" not in name.lower() \
            and name.strip().lower() != (tier or "").lower():
        parts.append(name.strip())
    if overage:
        parts.append(_("Fallback: {value}").format(value=overage))
    return " - ".join(parts) or None


class FactoryProvider(Provider):
    id = "factory"
    name = "Factory Droid"
    short = "F"
    color = "#EA580C"
    category = "agents"
    homepage = "https://app.factory.ai/settings/billing"
    source_summary = _("Factory billing API (API key)")
    setup_hint = _("Create an API key at app.factory.ai/settings/api-keys and paste it below, "
                   "or set FACTORY_API_KEY.")
    settings = (
        api_key_setting(("FACTORY_API_KEY",),
                        subtitle=_("Stored in GNOME Keyring. Also read from $FACTORY_API_KEY "
                                   "and ~/.factory/.env.")),
        SettingSpec("region", "choice", _("Region"), _("Where your Factory account is hosted"),
                    choices=(("us", _("US (default)")), ("eu", "EU")), default="us"),
    )

    def detect(self, ctx: FetchContext) -> bool:
        return find_key(ctx)[0] is not None

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        key, origin = find_key(ctx)
        if not key:
            raise NotConfigured(_("No Factory API key found"), self.setup_hint)
        headers = {**HEADERS, "Authorization": f"Bearer {key}"}
        eu = ctx.settings.get("region") == "eu"
        limits_base = EU_API_BASE if eu else API_BASE
        failures: list[Exception] = []
        for base in (EU_API_BASE,) if eu else (API_BASE, APP_BASE):
            try:
                return self._fetch_from(ctx, base, limits_base, headers, origin)
            except (HttpError, NetworkError, ProviderError) as exc:
                failures.append(exc)
        # A rejected key matters more than a later host's 404.
        for exc in failures:
            if isinstance(exc, AuthError) or (isinstance(exc, HttpError)
                                              and exc.status in (401, 403)):
                raise AuthError(_("Factory rejected the API key"), KEY_HINT) from None
        last = failures[-1]
        if isinstance(last, HttpError):
            raise from_http_error(last, login_hint=KEY_HINT, service=self.name) from None
        if isinstance(last, NetworkError):
            raise ProviderError(str(last), transient=True) from None
        raise last

    def _fetch_from(self, ctx: FetchContext, base: str, limits_base: str,
                    headers: dict[str, str], origin: str) -> ProviderSnapshot:
        me = ctx.http.get_json(f"{base}/api/app/auth/me", headers=headers, timeout=15)
        if not isinstance(me, dict):
            raise ProviderError(_("Unexpected response from Factory"))
        try:
            limits = ctx.http.get_json(f"{limits_base}/api/billing/limits", headers=headers,
                                       timeout=15)
        except (HttpError, NetworkError):
            limits = None  # not on the new billing (or a hiccup): the legacy view still works
        windows = parse_billing_limits(limits, ctx.now())
        overage = None
        if windows is not None:  # only a dict parses into windows
            overage = str(limits.get("overagePreference") or "") or None
        else:
            query = "useCache=true"
            user_id = dig(me, "userProfile", "id")
            if isinstance(user_id, str) and user_id.strip():
                query += "&userId=" + urllib.parse.quote(user_id.strip())
            windows = parse_legacy_usage(ctx.http.get_json(
                f"{base}/api/organization/subscription/usage?{query}", headers=headers,
                timeout=15))
        account = dig(me, "userProfile", "email") or dig(me, "organization", "name")
        return self.snapshot(ctx, windows, plan=plan_label(me, overage), account=account,
                             source=origin)
