"""Amp: the daily Amp Free allowance, subscription usage and credit balances.

Source: the official ``amp usage`` command; QuotaGlance only reads what it
prints. Without the CLI, the same text comes from Amp's balance endpoint,
called with an access token you paste into Preferences (kept in GNOME
Keyring), ``$AMP_API_KEY``, or the legacy key in the CLI's
``~/.local/share/amp/secrets.json``. Tokens are only read, never refreshed.
"""

from __future__ import annotations

import calendar
import math
import os
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
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
    strip_ansi,
)
from quotaglance.timefmt import format_amount
from quotaglance.util import UTC, clamp, dig, read_json, to_float

DEFAULT_URL = "https://ampcode.com"
BALANCE_PATH = "/api/internal?userDisplayBalanceInfo"
LEGACY_KEY = "apiKey@https://ampcode.com/"
LOGIN_HINT = _("Run `amp login` in a terminal.")
TOKEN_HINT = _("Create a new access token at ampcode.com → Settings and paste it in Preferences.")

# Patterns follow CodexBar's AmpUsageParser; `amp usage` and the API's displayText share them.
AMOUNT = r"([0-9][0-9,]*(?:\.[0-9]+)?)"
IDENTITY_RE = re.compile(r"(?im)^\s*Signed in as\s+([^\s(]+)(?:\s+\(([^\r\n)]+)\))?\s*$")
FREE_DOLLARS_RE = re.compile(
    r"(?im)^\s*Amp Free:\s*\$?" + AMOUNT + r"\s*/\s*\$?" + AMOUNT
    + r"\s+remaining(?:\s*\(replenishes\s*\+\$?" + AMOUNT + r"\s*/\s*hour\))?")
FREE_PERCENT_RE = re.compile(
    r"(?im)^\s*Amp Free:\s*" + AMOUNT
    + r"\s*%\s+remaining(?:\s+today)?(?:\s*(\(resets\s+daily\)))?")
TIER_RE = re.compile(
    r"(?im)^\s*Amp\s+([^\r\n]+?)\s+Tier:\s*agent\s+usage\s+\$" + AMOUNT + r"\s+of\s+\$" + AMOUNT
    + r"\s+remaining\b([^\r\n]*?)resets\s+upon\s+renewal\s+in\s+([0-9][0-9,]*)\s+(days?|months?)\b")
ORB_RE = re.compile(r"(?i)\borb\s+usage\s+" + AMOUNT + r"h\s+of\s+" + AMOUNT
                    + r"h\s+a1\.small\s+orb\s+hours\s+remaining\b")
PERIOD_RE = re.compile(r"\bperiod\s+(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})\b")
_SUBSCRIPTION_TAIL = (
    r"\s*" + AMOUNT + r"\s*%\s+other\s+usage\s+and\s+" + AMOUNT
    + r"\s*%\s+orb\s+usage\s+remaining\s*-\s*resets\s+upon\s+renewal\s+in\s+([0-9][0-9,]*)\s+"
    + r"(days?|months?)(?:\s+-\s+https?://\S+)?\s*$")
SUBSCRIPTION_RES = (
    re.compile(r"(?im)^\s*Subscription\s+(.+?):" + _SUBSCRIPTION_TAIL),
    re.compile(r"(?im)^\s*Amp\s+(.+?)\s+Subscription:" + _SUBSCRIPTION_TAIL),
)
CREDITS_RE = re.compile(r"(?im)^\s*Individual credits:\s*\$?" + AMOUNT + r"\s+remaining")
WORKSPACE_RE = re.compile(r"(?im)^\s*Workspace\s+(.+?):\s*\$?" + AMOUNT + r"\s+remaining")
SIGNED_OUT_RE = re.compile(r"(?i)sign in|log in|login")


# -- reset times ------------------------------------------------------------------


def _new_york_offset(day: date) -> timedelta:
    """UTC offset of New York at 20:00 on ``day``."""
    try:
        from zoneinfo import ZoneInfo

        offset = datetime.combine(day, time(20), tzinfo=ZoneInfo("America/New_York")).utcoffset()
        if offset is not None:
            return offset
    except (ImportError, KeyError, OSError, ValueError):
        pass
    # No time zone database: US daylight saving runs from March's 2nd to November's 1st Sunday.
    march, november = date(day.year, 3, 1), date(day.year, 11, 1)
    dst_start = march + timedelta(days=(6 - march.weekday()) % 7 + 7)
    dst_end = november + timedelta(days=(6 - november.weekday()) % 7)
    return timedelta(hours=-4 if dst_start <= day < dst_end else -5)


def next_free_reset(now: datetime) -> datetime:
    """Amp Free refills daily at 20:00 New York time (00:00 UTC in summer, 01:00 in winter)."""
    day = now.astimezone(UTC).date() - timedelta(days=1)
    while True:
        moment = datetime.combine(day, time(20), tzinfo=UTC) - _new_york_offset(day)
        if moment > now:
            return moment
        day += timedelta(days=1)


def _add_months(moment: datetime, months: int) -> datetime:
    index = moment.month - 1 + months
    year, month = moment.year + index // 12, index % 12 + 1
    return moment.replace(year=year, month=month,
                          day=min(moment.day, calendar.monthrange(year, month)[1]))


def _renewal(now: datetime, count: str, unit: str) -> datetime | None:
    """``resets upon renewal in N days|months`` as a moment, or None when unrepresentable."""
    try:
        value = int(count.replace(",", ""))
        if unit.lower().startswith("month"):
            return _add_months(now, value)
        return now + timedelta(days=value)
    except (OverflowError, ValueError):
        return None


def _period(text: str) -> tuple[datetime, datetime] | None:
    """The ``period YYYY-MM-DD to YYYY-MM-DD`` billing cycle (UTC days), if valid."""
    match = PERIOD_RE.search(text)
    if not match:
        return None
    try:
        start, end = (datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
                      for value in match.groups())
    except ValueError:
        return None
    return (start, end) if end > start else None


# -- parsing ----------------------------------------------------------------------


def _hours_left(hours: float) -> str:
    if 0 < hours < 1:
        return _("< 1h left")
    return _("{n}h left").format(n=math.floor(hours))


def _free_window(text: str, now: datetime) -> UsageWindow | None:
    dollars = FREE_DOLLARS_RE.search(text)  # the legacy $ form wins over the % form
    if dollars:
        remaining, quota = to_float(dollars.group(1)), to_float(dollars.group(2))
        hourly = to_float(dollars.group(3)) or 0.0
        if remaining is not None and quota is not None:
            used = max(0.0, quota - remaining)
            return UsageWindow(
                id="free", label=_("Amp Free"),
                used_percent=min(100.0, used / quota * 100) if quota > 0 else 0.0,
                # Time until the hourly replenishment has topped the allowance back up.
                resets_at=now + timedelta(hours=used / hourly) if hourly > 0 and used > 0
                else None,
                window_seconds=max(1, int(quota / hourly + 0.5)) * 3600 if hourly > 0 else None,
                used=used, limit=quota, unit="USD",
                detail=_("Refills {amount}/hour").format(amount=format_amount(hourly, "USD"))
                if hourly > 0 else None)
    percent = FREE_PERCENT_RE.search(text)
    remaining = to_float(percent.group(1)) if percent else None
    if percent is None or remaining is None:
        return None
    return UsageWindow(
        id="free", label=_("Amp Free"), used_percent=100.0 - clamp(remaining, 0, 100),
        resets_at=next_free_reset(now) if percent.group(2) else None, window_seconds=86400)


def _tier_windows(match: re.Match[str], now: datetime) -> list[UsageWindow]:
    """Current paid tiers: agent dollars plus orb hours, both renewing together."""
    remaining, limit = to_float(match.group(2)), to_float(match.group(3))
    if remaining is None or not limit or limit <= 0:
        return []
    middle = match.group(4)
    period = _period(middle)
    resets_at = period[1] if period else _renewal(now, match.group(5), match.group(6))
    if resets_at is None:
        return []
    span = int((period[1] - period[0]).total_seconds()) if period else None
    # The dollars are exact; the "(71%)" Amp prints next to them is rounded.
    windows = [UsageWindow(
        id="agent", label=_("Agent usage"),
        used_percent=clamp((limit - remaining) / limit * 100, 0, 100),
        resets_at=resets_at, window_seconds=span, used=max(0.0, limit - remaining),
        limit=limit, unit="USD",
        detail=_("{amount} left").format(amount=format_amount(remaining, "USD")))]
    orb = ORB_RE.search(middle)
    orb_left = to_float(orb.group(1)) if orb else None
    orb_limit = to_float(orb.group(2)) if orb else None
    if orb_left is not None and orb_limit and orb_limit > 0:
        windows.append(UsageWindow(
            id="orb", label=_("Orb usage"),
            used_percent=clamp((orb_limit - orb_left) / orb_limit * 100, 0, 100),
            resets_at=resets_at, window_seconds=span, used=max(0.0, orb_limit - orb_left),
            limit=orb_limit, unit="hours", detail=_hours_left(orb_left)))
    return windows


def _subscription_windows(text: str, now: datetime) -> tuple[list[UsageWindow], str | None]:
    tier = TIER_RE.search(text)
    if tier:
        return _tier_windows(tier, now), tier.group(1).strip()
    for pattern in SUBSCRIPTION_RES:
        match = pattern.search(text)
        if not match:
            continue
        plan = match.group(1).strip()
        other, orb = to_float(match.group(2)), to_float(match.group(3))
        resets_at = _renewal(now, match.group(4), match.group(5))
        if other is None or orb is None or resets_at is None:
            return [], plan
        return [
            UsageWindow(id="other", label=_("Other usage"),
                        used_percent=100.0 - clamp(other, 0, 100), resets_at=resets_at),
            UsageWindow(id="orb", label=_("Orb usage"),
                        used_percent=100.0 - clamp(orb, 0, 100), resets_at=resets_at),
        ], plan
    return [], None


def _short(name: str, limit: int = 12) -> str:
    return name if len(name) <= limit else name[:limit - 1].rstrip() + "…"


def _balance_windows(text: str) -> list[UsageWindow]:
    """Prepaid balances: the individual one and one per workspace (no reset, no meter)."""
    windows: list[UsageWindow] = []
    credits = CREDITS_RE.search(text)
    amount = to_float(credits.group(1)) if credits else None
    if amount is not None:
        windows.append(UsageWindow(id="credits", label=_("Credits"), used=amount, unit="USD",
                                   detail=_("Individual balance")))
    seen: set[str] = set()
    for match in WORKSPACE_RE.finditer(text):
        name, amount = match.group(1).strip(), to_float(match.group(2))
        if not name or amount is None:
            continue
        slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "default"
        window_id, suffix = f"workspace_{slug}", 2
        while window_id in seen:
            window_id, suffix = f"workspace_{slug}_{suffix}", suffix + 1
        seen.add(window_id)
        windows.append(UsageWindow(id=window_id, label=_short(name), used=amount, unit="USD",
                                   detail=_("Workspace balance")))
    return windows


def parse_usage_text(text: str, now: datetime) -> tuple[list[UsageWindow], str, str | None]:
    """Parse ``amp usage`` output into (windows, plan, account email)."""
    clean = strip_ansi(text).replace("**", "")
    identity = IDENTITY_RE.search(clean)
    subscription, plan = _subscription_windows(clean, now)
    free = _free_window(clean, now)
    # With a subscription, the monthly meters lead and Amp Free becomes an extra one.
    windows = subscription + ([free] if free else []) + _balance_windows(clean)
    if not windows:
        if identity is None and SIGNED_OUT_RE.search(clean):
            raise AuthError(_("Not signed in to Amp"), LOGIN_HINT)
        raise ProviderError(_("No Amp usage found in the CLI output"))
    if not plan:
        plan = _("Amp Free") if free else "Amp"
    return windows, plan, identity.group(1) if identity else None


def parse_balance_response(data: Any) -> str:
    """The ``displayText`` of a ``userDisplayBalanceInfo`` response."""
    if not isinstance(data, dict):
        raise ProviderError(_("Unexpected response from Amp"))
    if data.get("ok") is not True:
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        if error.get("code") == "auth-required":
            raise AuthError(_("Amp rejected the access token"), TOKEN_HINT)
        raise ProviderError(str(error.get("message") or _("Amp's usage API returned an error")))
    text = dig(data, "result", "displayText")
    if not isinstance(text, str) or not text.strip():
        raise ProviderError(_("Amp returned no usage text"))
    return text


# -- credentials ------------------------------------------------------------------


def find_cli(ctx: FetchContext) -> str | None:
    override = (ctx.env.get("AMP_CLI_PATH") or "").strip()
    if override:
        path = str(ctx.path(override))
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return ctx.which("amp")


def secrets_path(ctx: FetchContext) -> Path:
    return ctx.data_home / "amp" / "secrets.json"


def find_tokens(ctx: FetchContext) -> list[tuple[str, str]]:
    """Tokens for the balance API, best first, as (token, kind "file"|"keyring"|"env")."""
    found: list[tuple[str, str]] = []
    try:
        data = read_json(secrets_path(ctx))
    except (OSError, ValueError):
        data = None
    # Only the CLI's static API key; OAuth logins (with refresh tokens) are left alone.
    key = data.get(LEGACY_KEY) if isinstance(data, dict) else None
    if isinstance(key, str) and key.strip():
        found.append((key.strip(), "file"))
    stored = ctx.secret("api_key", (), provider="amp")
    if stored:
        found.append((stored, "keyring"))
    env = (ctx.env.get("AMP_API_KEY") or "").strip()
    if env and env != stored:
        found.append((env, "env"))
    return found


def balance_url(ctx: FetchContext) -> str:
    base = (ctx.env.get("AMP_URL") or "").strip().rstrip("/")
    return (base if base.startswith("https://") else DEFAULT_URL) + BALANCE_PATH


class AmpProvider(Provider):
    id = "amp"
    name = "Amp"
    short = "A"
    color = "#EF4444"
    category = "agents"
    homepage = "https://ampcode.com/settings"
    source_summary = _("`amp usage` (CLI), or Amp's balance API with an access token")
    setup_hint = _("Install the Amp CLI and run `amp login`, or paste an Amp access token below.")
    settings = (
        api_key_setting(("AMP_API_KEY",), title=_("Access token"),
                        subtitle=_("Optional, for when the amp CLI isn't installed. Stored in "
                                   "GNOME Keyring; also read from $AMP_API_KEY.")),
    )

    def detect(self, ctx: FetchContext) -> bool:
        return bool(find_cli(ctx)) or bool(find_tokens(ctx))

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        now = ctx.now()
        binary = find_cli(ctx)
        tokens = find_tokens(ctx)
        if binary:
            try:
                windows, plan, account = parse_usage_text(self._run_cli(ctx, binary), now)
                return self.snapshot(ctx, windows, plan=plan, account=account,
                                     source=_("amp CLI"))
            except AuthError:
                # The CLI already used its own login and $AMP_API_KEY; only a token pasted
                # into Preferences is a different credential worth trying.
                tokens = [entry for entry in tokens if entry[1] == "keyring"]
                if not tokens:
                    raise
            except ProviderError:
                if not tokens:
                    raise
        elif not tokens:
            raise NotConfigured(_("Amp CLI not found"), self.setup_hint)
        rejected: AuthError | None = None
        for token, kind in tokens:  # a rejected token falls through to the next one
            try:
                text = self._display_text(ctx, token, kind)
            except AuthError as exc:
                rejected = exc
                continue
            windows, plan, account = parse_usage_text(text, now)
            return self.snapshot(ctx, windows, plan=plan, account=account, source=_("Amp API"))
        raise rejected or AuthError(_("Amp rejected the access token"), TOKEN_HINT)

    @staticmethod
    def _run_cli(ctx: FetchContext, binary: str) -> str:
        result = ctx.run([binary, "usage"], timeout=15)
        output = result.stdout if (result.stdout or "").strip() else (result.stderr or "")
        if not output.strip():
            raise ProviderError(_("The Amp CLI returned no usage data"))
        return output

    def _display_text(self, ctx: FetchContext, token: str, kind: str) -> str:
        hint = LOGIN_HINT if kind == "file" else TOKEN_HINT
        try:
            data = ctx.http.post_json(
                balance_url(ctx), {"method": "userDisplayBalanceInfo", "params": {}},
                headers={"Authorization": f"Bearer {token}"}, timeout=15,
                follow_redirects=False)  # never hand the token to another host
        except HttpError as exc:
            if exc.status in (401, 403):
                raise AuthError(_("Amp rejected the access token"), hint) from None
            raise from_http_error(exc, login_hint=hint, service=self.name) from None
        try:
            return parse_balance_response(data)
        except AuthError as exc:
            raise AuthError(exc.message, hint) from None
