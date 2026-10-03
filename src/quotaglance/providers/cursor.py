"""Cursor: monthly included usage, Auto/API split and on-demand spend.

Source: the access token the Cursor app keeps in its local SQLite state DB
(``~/.config/Cursor/User/globalStorage/state.vscdb``), turned into the
dashboard session cookie and sent to ``cursor.com/api/usage-summary``. The
database is opened read-only and the token is never refreshed, so the
Cursor app's own session is never disturbed.
"""

from __future__ import annotations

import sqlite3
import urllib.parse
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
    from_http_error,
)
from quotaglance.util import clamp, decode_jwt_claims, dig, parse_time, read_json, to_float

SUMMARY_URL = "https://cursor.com/api/usage-summary"
API2_USAGE_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
TOKEN_KEY = "cursorAuth/accessToken"
EMAIL_KEY = "cursorAuth/cachedEmail"
MEMBERSHIP_KEY = "cursorAuth/stripeMembershipType"

PLAN_NAMES = {
    "enterprise": "Enterprise", "express": "Start", "free": "Free", "free_trial": "Pro Trial",
    "hobby": "Hobby", "pro": "Pro", "pro_student": "Pro", "pro_plus": "Pro+", "team": "Team",
    "teams": "Team", "business": "Business", "ultra": "Ultra",
}

LOGIN_HINT = _("Open Cursor and sign in; QuotaGlance reads its session read-only.")


def plan_label(membership: str | None) -> str | None:
    if not membership:
        return None
    key = membership.strip().lower()
    return PLAN_NAMES.get(key, membership.replace("_", " ").title())


# -- local credentials ---------------------------------------------------------


def state_db_path(ctx: FetchContext) -> Path:
    return ctx.config_home / "Cursor" / "User" / "globalStorage" / "state.vscdb"


def decode_value(value: Any) -> str | None:
    """ItemTable values are TEXT or BLOB (sometimes UTF-16LE)."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value
    else:
        raw = bytes(value)
        if raw and len(raw) % 2 == 0 and all(raw[i] == 0 for i in range(1, len(raw), 2)) \
                and all(0 < raw[i] < 128 for i in range(0, len(raw), 2)):
            text = raw.decode("utf-16-le")
        else:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("utf-16-le", errors="ignore")
    text = text.strip().strip('"').strip()
    return text or None


def read_state_values(db_path: Path, keys: list[str]) -> dict[str, str]:
    """Read keys from Cursor's ItemTable without ever writing to the DB."""
    uri_path = urllib.parse.quote(str(db_path))
    values: dict[str, str] = {}
    for suffix in ("?mode=ro", "?mode=ro&immutable=1"):
        try:
            conn = sqlite3.connect(f"file:{uri_path}{suffix}", uri=True, timeout=0.25)
        except sqlite3.Error:
            continue
        try:
            placeholders = ",".join("?" for _ in keys)
            rows = conn.execute(
                f"SELECT key, value FROM ItemTable WHERE key IN ({placeholders})", keys).fetchall()
        except sqlite3.Error:
            conn.close()
            continue
        conn.close()
        for key, value in rows:
            decoded = decode_value(value)
            if decoded:
                values[key] = decoded
        return values
    return values


def find_token(ctx: FetchContext) -> tuple[str | None, dict[str, str]]:
    for name in ("CURSOR_ACCESS_TOKEN", "CURSOR_TOKEN"):
        if ctx.env.get(name):
            return ctx.env[name].strip(), {}
    db = state_db_path(ctx)
    if db.is_file():
        values = read_state_values(db, [TOKEN_KEY, EMAIL_KEY, MEMBERSHIP_KEY])
        if values.get(TOKEN_KEY):
            return values[TOKEN_KEY], values
    # cursor-agent (the CLI) keeps its own login.
    agent = ctx.config_home / "cursor" / "auth.json"
    if agent.is_file():
        try:
            data = read_json(agent)
        except (OSError, ValueError):
            data = {}
        token = data.get("accessToken") or data.get("access_token")
        if token:
            return token, {EMAIL_KEY: data.get("email") or data.get("cachedEmail") or ""}
    return None, {}


def session_cookie(token: str, now_ts: float) -> str:
    claims = decode_jwt_claims(token)
    subject = str(claims.get("sub") or "")
    user_id = subject.split("|")[-1]
    if not user_id:
        raise AuthError(_("Cursor's saved session is unreadable"), LOGIN_HINT)
    exp = to_float(claims.get("exp"))
    if exp is not None and exp - 60 <= now_ts:
        raise AuthError(_("Cursor's session has expired"),
                        _("Open Cursor once so it refreshes its sign-in."))
    return f"WorkosCursorSessionToken={user_id}%3A%3A{token}"


# -- parsing --------------------------------------------------------------------


def _cents(value: Any) -> float | None:
    number = to_float(value)
    return None if number is None else number / 100.0


def parse_usage_summary(data: dict[str, Any]) -> tuple[list[UsageWindow], str | None]:
    """Map ``/api/usage-summary`` onto windows. Money fields are USD cents."""
    resets_at = parse_time(data.get("billingCycleEnd"))
    plan = dig(data, "individualUsage", "plan") or {}
    overall = dig(data, "individualUsage", "overall") or {}
    pooled = dig(data, "teamUsage", "pooled") or {}
    unlimited = bool(data.get("isUnlimited"))

    auto = to_float(plan.get("autoPercentUsed"))
    api = to_float(plan.get("apiPercentUsed"))
    total = to_float(plan.get("totalPercentUsed"))
    used = _cents(plan.get("used"))
    limit = _cents(plan.get("limit"))
    if total is None:
        if auto is not None and api is not None:
            total = (auto + api) / 2
        elif api is not None or auto is not None:
            total = api if api is not None else auto
        elif used is not None and limit:
            total = used / limit * 100
        elif to_float(overall.get("limit")):
            used = _cents(overall.get("used"))
            limit = _cents(overall.get("limit"))
            total = (used or 0) / limit * 100 if limit else None
        elif to_float(pooled.get("limit")):
            used = _cents(pooled.get("used"))
            limit = _cents(pooled.get("limit"))
            total = (used or 0) / limit * 100 if limit else None
    if unlimited:
        total = 0.0

    windows: list[UsageWindow] = []
    if total is not None:
        windows.append(UsageWindow(
            id="monthly", label=_("Included"), used_percent=clamp(total, 0, 100),
            resets_at=resets_at, used=used, limit=limit, unit="USD" if limit else None))
    if auto is not None and not unlimited:
        windows.append(UsageWindow(id="auto", label=_("Auto"), used_percent=clamp(auto, 0, 100),
                                   resets_at=resets_at))
    if api is not None and not unlimited:
        windows.append(UsageWindow(id="api", label=_("API models"),
                                   used_percent=clamp(api, 0, 100), resets_at=resets_at))

    on_demand = dig(data, "individualUsage", "onDemand") or {}
    team_on_demand = dig(data, "teamUsage", "onDemand") or {}
    od_used = _cents(on_demand.get("used"))
    od_limit = _cents(on_demand.get("limit"))
    if not od_limit and _cents(team_on_demand.get("limit")):
        od_limit = _cents(team_on_demand.get("limit"))
        od_used = _cents(team_on_demand.get("used"))
    if on_demand.get("enabled") and (od_limit or od_used):
        windows.append(UsageWindow(
            id="on_demand", label=_("On-demand"),
            used_percent=clamp((od_used or 0) / od_limit * 100, 0, 100) if od_limit else None,
            resets_at=resets_at, used=od_used or 0.0, limit=od_limit, unit="USD"))
    return windows, plan_label(data.get("membershipType"))


def parse_current_period_usage(data: dict[str, Any]) -> list[UsageWindow]:
    """``api2.cursor.sh`` protobuf-JSON: cents and epoch-ms arrive as strings."""
    resets_at = parse_time(data.get("billingCycleEnd"))
    plan = data.get("planUsage") or {}
    used = _cents(plan.get("totalSpend"))
    limit = _cents(plan.get("limit"))
    total = to_float(plan.get("totalPercentUsed"))
    if total is None and used is not None and limit:
        total = used / limit * 100
    windows = []
    if total is not None:
        windows.append(UsageWindow(id="monthly", label=_("Included"),
                                   used_percent=clamp(total, 0, 100), resets_at=resets_at,
                                   used=used, limit=limit, unit="USD" if limit else None))
    for key, wid, label in (("autoPercentUsed", "auto", _("Auto")),
                            ("apiPercentUsed", "api", _("API models"))):
        value = to_float(plan.get(key))
        if value is not None:
            windows.append(UsageWindow(id=wid, label=label, used_percent=clamp(value, 0, 100),
                                       resets_at=resets_at))
    spend = data.get("spendLimitUsage") or {}
    od_limit = _cents(spend.get("individualLimit"))
    od_used = _cents(spend.get("individualUsed"))
    if od_limit:
        windows.append(UsageWindow(id="on_demand", label=_("On-demand"),
                                   used_percent=clamp((od_used or 0) / od_limit * 100, 0, 100),
                                   resets_at=resets_at, used=od_used or 0.0, limit=od_limit,
                                   unit="USD"))
    return windows


def _is_waf_challenge(exc: HttpError) -> bool:
    content_type = exc.headers.get("content-type", "")
    text = exc.text(2000).lower()
    return exc.status == 403 and ("text/html" in content_type or "<html" in text
                                  or "security checkpoint" in text)


class CursorProvider(Provider):
    id = "cursor"
    name = "Cursor"
    short = "Cu"
    color = "#64748B"
    category = "editors"
    homepage = "https://cursor.com/dashboard"
    source_summary = _("Cursor app sign-in → Cursor usage dashboard")
    setup_hint = _("Install Cursor and sign in. QuotaGlance reuses that session (read-only).")

    def detect(self, ctx: FetchContext) -> bool:
        return state_db_path(ctx).is_file() or bool(ctx.env.get("CURSOR_ACCESS_TOKEN"))

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        token, values = find_token(ctx)
        if not token:
            raise NotConfigured(_("Not signed in to Cursor"), self.setup_hint)
        cookie = session_cookie(token, ctx.now().timestamp())
        account = values.get(EMAIL_KEY) or decode_jwt_claims(token).get("email")
        try:
            data = ctx.http.get_json(SUMMARY_URL, headers={"Cookie": cookie,
                                                          "Origin": "https://cursor.com"})
        except HttpError as exc:
            if not _is_waf_challenge(exc):
                raise from_http_error(exc, login_hint=LOGIN_HINT, service=self.name) from None
            # Vercel's bot check sometimes blocks cursor.com; the IDE API is not affected.
            try:
                data = ctx.http.post_json(API2_USAGE_URL, {}, headers={
                    "Authorization": f"Bearer {token}", "Connect-Protocol-Version": "1"})
            except HttpError as inner:
                raise from_http_error(inner, login_hint=LOGIN_HINT, service=self.name) from None
            windows = parse_current_period_usage(data or {})
            plan = plan_label(values.get(MEMBERSHIP_KEY))
            return self.snapshot(ctx, windows, plan=plan, account=account,
                                 source=_("Cursor API"))
        if not isinstance(data, dict):
            raise ProviderError(_("Unexpected response from Cursor"))
        windows, plan = parse_usage_summary(data)
        plan = plan or plan_label(values.get(MEMBERSHIP_KEY))
        if not windows:
            raise ProviderError(_("Cursor returned no usage for this account"))
        return self.snapshot(ctx, windows, plan=plan, account=account or None,
                             source=_("Cursor dashboard"))
