"""Kiro (AWS): monthly credits, bonus credits and overage.

Source: the official CLI report, ``kiro-cli chat --no-interactive /usage``.
When kiro-cli's own state database holds a valid session, QuotaGlance also
asks the Kiro usage API (``GetUsageLimits``) for exact numbers and the
overage cap. Without kiro-cli, the Kiro IDE's token file is used for the
same API. Tokens are only read; they are never refreshed here, because a
refresh can rotate them and sign you out of Kiro.
"""

from __future__ import annotations

import re
import sqlite3
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
    strip_ansi,
)
from quotaglance.util import clamp, parse_time, read_json, to_float

LOGIN_HINT = _("Run `kiro-cli login`, or open Kiro and sign in.")
TOKEN_KEYS = ("kirocli:social:token", "kirocli:odic:token", "kirocli:oidc:token",
              "codewhisperer:odic:token", "codewhisperer:oidc:token")
ENDPOINTS = {"us-east-1": "https://codewhisperer.us-east-1.amazonaws.com/",
             "eu-central-1": "https://q.eu-central-1.amazonaws.com/"}
ARN_RE = re.compile(r"^arn:aws:codewhisperer:([a-z0-9-]+):\d+:profile/[A-Za-z0-9_-]+$")

PERCENT_RE = re.compile(r"█+\s*(\d+(?:\.\d+)?)%")
CREDITS_RE = re.compile(r"\((\d+(?:\.\d+)?)\s+of\s+(\d+(?:\.\d+)?)\s+covered")
RESET_RE = re.compile(r"resets on (\d{4}-\d{2}-\d{2}|\d{2}/\d{2})")
BONUS_RE = re.compile(r"Bonus credits:\s*(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)")
EXPIRY_RE = re.compile(r"expires in (\d+) days?")
PLAN_HEADER_RE = re.compile(r"Estimated Usage\s*\|[^|\n]*\|\s*([^\n|]+)")
SUMMARY_RE = re.compile(r"(?m)^[ \t]*Plan:[ \t]*([^|\r\n]+?)[ \t]*\|[ \t]*[0-9]+[ \t]+"
                        r"usage breakdowns?[ \t]*$")
MANAGED_RE = re.compile(r"(?m)^[ \t]*Plan:[ \t]*(.+?)[ \t]*$")
LEGACY_PLAN_RE = re.compile(r"\|\s*(KIRO [A-Z0-9+ ]+?)\s*(?:\||$)", re.M)
OVERAGE_RE = re.compile(r"(?i)Overages:\s*([^\n]+)")
OVERAGE_USED_RE = re.compile(r"(?i)Credits used:\s*(\d+(?:\.\d+)?)")
OVERAGE_COST_RE = re.compile(r"(?i)Est\.\s*cost:\s*\$?(\d+(?:\.\d+)?)\s*USD")


def plan_display(raw: str | None) -> str | None:
    if not raw:
        return None
    words = []
    for word in raw.strip().split():
        upper = word.upper()
        words.append(upper if upper in ("Q", "AWS") else word.capitalize())
    return " ".join(words) or None


def _reset_date(text: str, now: datetime) -> datetime | None:
    match = RESET_RE.search(text)
    if not match:
        return None
    value = match.group(1)
    if "-" in value:
        return parse_time(value)
    month, day = (int(part) for part in value.split("/"))
    try:
        candidate = datetime(now.year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None
    if candidate < now - timedelta(days=1):
        candidate = candidate.replace(year=now.year + 1)
    return candidate


def parse_usage_output(output: str, now: datetime) -> dict[str, Any]:
    """Parse ``/usage`` text from every kiro-cli generation we know about."""
    text = strip_ansi(output)
    lowered = text.lower()
    if ("not logged in" in lowered or "login required" in lowered or "kiro-cli login" in lowered
            or "oauth error" in lowered or "failed to initialize auth portal" in lowered):
        raise AuthError(_("Not signed in to Kiro"), LOGIN_HINT)
    if "could not retrieve usage information" in lowered:
        raise ProviderError(_("Kiro CLI could not retrieve usage information"), transient=True)
    flat = re.sub(r"[ \t]+", " ", text.replace("┃", " ").replace("│", " "))

    result: dict[str, Any] = {"plan": None, "used": None, "limit": None, "percent": None,
                              "resets_at": _reset_date(flat, now), "bonus": None,
                              "overage": None}
    header = PLAN_HEADER_RE.search(flat)
    summary = SUMMARY_RE.search(flat)
    legacy = LEGACY_PLAN_RE.search(flat)
    if header:
        result["plan"] = header.group(1).strip()
    elif summary:
        result["plan"] = summary.group(1).strip()
    elif legacy:
        result["plan"] = legacy.group(1).strip()

    credits = CREDITS_RE.search(flat)
    percent = PERCENT_RE.search(flat)
    if credits:
        result["used"], result["limit"] = float(credits.group(1)), float(credits.group(2))
        if result["limit"] > 0:
            result["percent"] = result["used"] / result["limit"] * 100
    if percent and result["percent"] is None:
        result["percent"] = float(percent.group(1))

    bonus = BONUS_RE.search(flat)
    if bonus and float(bonus.group(2)) > 0:
        expiry = EXPIRY_RE.search(flat[bonus.end():bonus.end() + 80])
        result["bonus"] = (float(bonus.group(1)), float(bonus.group(2)),
                           int(expiry.group(1)) if expiry else None)
    overage = OVERAGE_RE.search(flat)
    if overage:
        used = OVERAGE_USED_RE.search(flat)
        cost = OVERAGE_COST_RE.search(flat)
        result["overage"] = (overage.group(1).strip(), float(used.group(1)) if used else None,
                             float(cost.group(1)) if cost else None)

    if result["percent"] is None and result["used"] is None:
        managed = MANAGED_RE.search(flat)
        if summary:
            return result  # plan known, usage not reported (organisation accounts)
        if managed and ("managed by admin" in lowered or "managed by organization" in lowered):
            result["plan"] = managed.group(1).strip()
            result["managed"] = True
            return result
        raise ProviderError(_("No recognisable usage in Kiro CLI output; its format may have "
                              "changed"))
    return result


def parse_usage_limits(data: dict[str, Any]) -> dict[str, Any]:
    """``GetUsageLimits`` → plan/overage numbers (credits) with CodexBar's checks."""
    rows = [row for row in data.get("usageBreakdownList") or []
            if isinstance(row, dict) and row.get("resourceType") == "CREDIT"]
    if len(rows) != 1:
        raise ProviderError(_("Unexpected Kiro usage response"))
    row = rows[0]
    total = to_float(row.get("currentUsageWithPrecision", row.get("currentUsage")))
    overage = to_float(row.get("currentOveragesWithPrecision", row.get("currentOverages"))) or 0.0
    limit = to_float(row.get("usageLimitWithPrecision", row.get("usageLimit")))
    if total is None or limit is None or total < 0 or overage < 0 or overage > total:
        raise ProviderError(_("Unexpected Kiro usage response"))
    has_bonus = bool(row.get("bonuses"))
    plan_used = total - overage
    if not has_bonus and limit > 0 and plan_used > limit + 1e-6:
        raise ProviderError(_("Unexpected Kiro usage response"))
    reset = to_float(row.get("nextDateReset", data.get("nextDateReset")))
    resets_at = parse_time(reset) if reset and 1e9 <= reset <= 4.1e9 else None
    status = str((data.get("overageConfiguration") or {}).get("overageStatus") or "").upper()
    cap = to_float(row.get("overageCapWithPrecision", row.get("overageCap"))) or 0.0
    return {
        "plan": (data.get("subscriptionInfo") or {}).get("subscriptionTitle"),
        "plan_used": plan_used, "limit": limit, "has_bonus": has_bonus,
        "resets_at": resets_at, "overage_enabled": status == "ENABLED" if status in (
            "ENABLED", "DISABLED") else None,
        "overage_used": overage, "overage_cap": cap,
        "charges": to_float(row.get("overageCharges")), "rate": to_float(row.get("overageRate")),
    }


def build_windows(cli: dict[str, Any] | None, api: dict[str, Any] | None,
                  now: datetime) -> tuple[list[UsageWindow], str | None]:
    cli = cli or {}
    plan = cli.get("plan")
    used, limit, percent = cli.get("used"), cli.get("limit"), cli.get("percent")
    resets_at = cli.get("resets_at")
    if api:
        plan = api.get("plan") or plan
        resets_at = api.get("resets_at") or resets_at
        if not api["has_bonus"] and api["limit"] > 0:
            used, limit = api["plan_used"], api["limit"]
            percent = used / limit * 100
    windows: list[UsageWindow] = []
    if percent is not None:
        windows.append(UsageWindow(id="credits", label=_("Credits"),
                                   used_percent=clamp(percent, 0, 100), resets_at=resets_at,
                                   window_seconds=30 * 86400, used=used, limit=limit,
                                   unit="credits" if limit else None))
    bonus = cli.get("bonus")
    if bonus:
        bonus_used, bonus_total, days = bonus
        windows.append(UsageWindow(
            id="bonus", label=_("Bonus"), used_percent=clamp(bonus_used / bonus_total * 100, 0,
                                                             100),
            used=bonus_used, limit=bonus_total, unit="credits",
            detail=_("Expires in {n} days").format(n=days) if days is not None else None))
    if api and api.get("overage_enabled") and api.get("overage_cap"):
        cap = api["overage_cap"]
        windows.append(UsageWindow(
            id="overage", label=_("Overage"),
            used_percent=min(100.0, api["overage_used"] / cap * 100), resets_at=resets_at,
            used=api["overage_used"], limit=cap, unit="credits",
            detail=_("${charges:.2f} so far").format(charges=api["charges"])
            if api.get("charges") is not None else None))
    elif cli.get("overage") and cli["overage"][1]:
        status, credits_used, cost = cli["overage"]
        detail = _("{n:g} credits").format(n=credits_used)
        if cost is not None:
            detail += f" · ${cost:.2f}"
        windows.append(UsageWindow(id="overage", label=_("Overage"), used=credits_used,
                                   unit="credits", detail=detail))
    if not windows and cli.get("managed"):
        windows.append(UsageWindow(id="managed", label=_("Plan"),
                                   detail=_("Managed by your organization")))
    return windows, plan_display(plan)


# -- local tokens (read-only) ---------------------------------------------------------


def _decode(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)


def _json(value: Any) -> Any:
    import json

    text = _decode(value)
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text.strip().strip('"')


def db_token(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """(token JSON, profile ARN) from kiro-cli's SQLite state, opened read-only."""
    try:
        conn = sqlite3.connect(f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True,
                               timeout=0.25)
    except sqlite3.Error:
        return None, None
    try:
        token = None
        for key in TOKEN_KEYS:
            row = conn.execute("SELECT value FROM auth_kv WHERE key = ?", (key,)).fetchone()
            if row and isinstance(_json(row[0]), dict):
                token = _json(row[0])
                break
        arn = None
        try:
            row = conn.execute("SELECT value FROM state WHERE key = 'api.codewhisperer.profile'"
                               ).fetchone()
        except sqlite3.Error:
            row = None
        profile = _json(row[0]) if row else None
        if isinstance(profile, dict):
            arn = profile.get("arn")
        elif isinstance(profile, str):
            arn = profile
        return token, arn
    except sqlite3.Error:
        return None, None
    finally:
        conn.close()


def token_fields(token: dict[str, Any]) -> tuple[str | None, datetime | None, str | None]:
    access = token.get("access_token") or token.get("accessToken")
    expires = parse_time(token.get("expires_at") or token.get("expiresAt"))
    arn = token.get("profileArn") or token.get("profile_arn")
    return access, expires, arn


def get_usage_limits(ctx: FetchContext, access: str, arn: str) -> dict[str, Any] | None:
    match = ARN_RE.match(arn or "")
    endpoint = ENDPOINTS.get(match.group(1)) if match else None
    if not endpoint:
        return None  # never send the token to a guessed endpoint
    response = ctx.http.post(endpoint, json_body={"profileArn": arn}, headers={
        "Content-Type": "application/x-amz-json-1.0",
        "X-Amz-Target": "AmazonCodeWhispererService.GetUsageLimits",
        "Authorization": f"Bearer {access}",
    }, timeout=10)
    return parse_usage_limits(response.json() or {})


class KiroProvider(Provider):
    id = "kiro"
    name = "Kiro"
    short = "K"
    color = "#D946EF"
    category = "editors"
    homepage = "https://app.kiro.dev/account/usage"
    source_summary = _("kiro-cli /usage report + Kiro usage API")
    setup_hint = _("Install kiro-cli and run `kiro-cli login`, or sign in to the Kiro IDE.")

    def _db_path(self, ctx: FetchContext) -> Path:
        override = ctx.env.get("KIRO_DATA_DIR")
        base = Path(override) if override else ctx.data_home / "kiro-cli"
        return base / "data.sqlite3"

    def _ide_token(self, ctx: FetchContext) -> Path:
        return ctx.path("~/.aws/sso/cache/kiro-auth-token.json")

    def _cli(self, ctx: FetchContext) -> str | None:
        return ctx.env.get("KIRO_CLI_PATH") or ctx.which("kiro-cli")

    def detect(self, ctx: FetchContext) -> bool:
        return bool(self._cli(ctx)) or self._ide_token(ctx).is_file()

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        now = ctx.now()
        cli = self._cli(ctx)
        cli_result = None
        account = None
        if cli:
            result = ctx.run([cli, "chat", "--no-interactive", "/usage"], timeout=25,
                             extra_env={"TERM": "xterm-256color"}, idle_timeout=4)
            output = (result.stdout or "").strip() + "\n" + (result.stderr or "").strip()
            cli_result = parse_usage_output(output, now)
            account = self._whoami(ctx, cli)
            token, arn = db_token(self._db_path(ctx))
        else:
            path = self._ide_token(ctx)
            if not path.is_file():
                raise NotConfigured(_("Kiro isn't set up"), self.setup_hint)
            try:
                token = read_json(path)
            except (OSError, ValueError):
                token = None
            arn = None
        api = None
        if isinstance(token, dict):
            access, expires, token_arn = token_fields(token)
            arn = token_arn or arn
            fresh = expires is None or expires > now + timedelta(seconds=60)
            if access and arn and fresh:
                try:
                    api = get_usage_limits(ctx, access, arn)
                except (HttpError, NetworkError, ProviderError):
                    if cli_result is None:
                        raise
                    api = None  # the CLI numbers stand on their own
            elif cli_result is None:
                raise AuthError(_("The Kiro sign-in has expired"),
                                _("Open Kiro so it can refresh your session."))
        if cli_result is None and api is None:
            raise NotConfigured(_("Kiro isn't set up"), self.setup_hint)
        windows, plan = build_windows(cli_result, api, now)
        if not windows:
            raise ProviderError(_("Kiro reported a plan but no usage yet"))
        source = _("kiro-cli") if cli_result is not None else _("Kiro IDE sign-in")
        if api is not None and cli_result is not None:
            source = _("kiro-cli + Kiro API")
        return self.snapshot(ctx, windows, plan=plan, account=account, source=source)

    @staticmethod
    def _whoami(ctx: FetchContext, cli: str) -> str | None:
        try:
            result = ctx.run([cli, "whoami"], timeout=4, idle_timeout=1.5)
        except ProviderError:
            return None
        for line in strip_ansi(result.stdout or "").splitlines():
            line = line.strip()
            if line.lower().startswith("email:"):
                return line.split(":", 1)[1].strip() or None
            if "@" in line and " " not in line:
                return line
        return None
