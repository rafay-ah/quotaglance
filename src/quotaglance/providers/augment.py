"""Augment Code: the monthly credits (or included USD usage) of your plan.

Source: the official ``auggie account status --json`` command. When auggie
can't run (no Node.js on the desktop's PATH, a timeout), QuotaGlance sends
the session ``auggie login`` saved in ``~/.augment/session.json`` (or
``$AUGMENT_SESSION_AUTH``) to the same ``get-billing-summary`` call auggie
makes. The session is only read: QuotaGlance never rewrites or deletes it,
and the token is only ever sent to Augment's own servers.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import uuid
from datetime import datetime
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
    strip_ansi,
)
from quotaglance.timefmt import format_amount
from quotaglance.util import UTC, clamp, parse_time, read_json, to_float

LOGIN_HINT = _("Run `auggie login` in a terminal.")
UNIT_CODES = {1: "credits", 2: "USD"}
SESSION_ID = str(uuid.uuid4())  # auggie also keeps one x-request-session-id per run

# `auggie account status` text. Credit counts follow the process locale (forced to en_US
# when QuotaGlance runs it); dollar amounts are always en-US formatted.
CREDITS_LEFT_RE = re.compile(r"(\d[\d,.\u00a0\u202f]*)\s+credits\s+remaining")
CREDITS_MONTH_RE = re.compile(r"(\d[\d,.\u00a0\u202f]*)\s+credits\s*/\s*month")
USD_LEFT_RE = re.compile(r"\$(\d[\d,]*(?:\.\d+)?)\s+remaining")
USD_MONTH_RE = re.compile(r"\$(\d[\d,]*(?:\.\d+)?)\s+included usage\s*/\s*month")
LEGACY_USED_RE = re.compile(r"([\d,]+)\s+remaining\s*·\s*([\d,]+)\s*/\s*([\d,]+)\s+credits used")
LEGACY_PLAN_RE = re.compile(r"(?m)^\s*(\S.*?)\s+[\d,]+\s+credits\s*/\s*month")
BOX_PLAN_RE = re.compile(r"(?m)remaining[ \t]{2,}([^\s│].*?)[ \t]*(?:│|$)")
CYCLE_END_RE = re.compile(r"\bend(?:s|ed)\s+(\d{1,2})/(\d{1,2})/(\d{4})")


# -- parsing ----------------------------------------------------------------------


def _unit(value: Any) -> str | None:
    """Usage unit as a QuotaGlance unit: the API sends an enum, the CLI a name."""
    number = to_float(value)
    if number is not None:
        return UNIT_CODES.get(int(number))
    text = str(value or "").lower()
    if "credit" in text:
        return "credits"
    if "usd" in text or "dollar" in text:
        return "USD"
    return None


def _credit_count(text: str) -> float | None:
    """Credits are whole numbers, so any locale's grouping characters can go."""
    return to_float(re.sub(r"[,.\u00a0\u202f\s]", "", text))


def build_windows(remaining: float | None, included: float | None, unit: str | None,
                  resets_at: datetime | None, used: float | None = None,
                  total: float | None = None) -> list[UsageWindow]:
    label = _("Included") if unit == "USD" else _("Credits")
    if used is None and remaining is not None and included is not None:
        used, total = max(0.0, included - remaining), included
    if used is None or not total or total <= 0:
        if remaining is None:
            return []
        return [UsageWindow(id="credits", label=label, resets_at=resets_at, used=remaining,
                            unit=unit, detail=_("Remaining balance"))]
    detail = None
    if remaining is not None:
        detail = _("{amount} left").format(amount=format_amount(remaining, unit))
        if included is not None and remaining > included:  # top-ups or rollover
            detail = _("{amount} left · above the monthly allowance").format(
                amount=format_amount(remaining, unit))
    return [UsageWindow(
        id="credits", label=label, used_percent=clamp(used / total * 100, 0, 100),
        resets_at=resets_at, window_seconds=30 * 86400, used=used, limit=total, unit=unit,
        detail=detail)]


def parse_billing_summary(data: Any) -> tuple[list[UsageWindow], str | None]:
    """Raw ``get-billing-summary`` response (snake_case; numbers may be strings)."""
    if not isinstance(data, dict):
        raise ProviderError(_("Unexpected response from Augment"))
    unit = _unit(data.get("usage_unit"))
    windows = build_windows(to_float(data.get("amount_remaining")),
                            to_float(data.get("amount_included_per_cycle")), unit,
                            parse_time(data.get("billing_cycle_end_date_iso")))
    if not windows:
        raise ProviderError(_("Augment didn't report your plan's usage"))
    return windows, data.get("plan_name") or None


def parse_status_json(data: dict[str, Any]) -> tuple[list[UsageWindow], str | None]:
    """``auggie account status --json`` (camelCase version of the same response)."""
    unit = _unit(data.get("usageUnit"))
    windows = build_windows(to_float(data.get("amountRemaining")),
                            to_float(data.get("amountIncludedPerCycle")), unit,
                            parse_time(data.get("billingCycleEndDate")))
    if not windows:
        raise ProviderError(_("Augment didn't report your plan's usage"))
    return windows, data.get("planName") or None


def parse_status_text(text: str) -> tuple[list[UsageWindow], str | None]:
    """The boxed text of ``auggie account status`` (and the pre-2026 line layout)."""
    flat = text.replace("│", " ")
    match = CYCLE_END_RE.search(flat)
    resets_at = None
    if match:
        month, day, year = (int(part) for part in match.groups())
        try:
            resets_at = datetime(year, month, day, tzinfo=UTC)  # auggie prints UTC dates
        except ValueError:
            resets_at = None
    legacy = LEGACY_USED_RE.search(flat)
    if legacy:
        remaining, used, total = (_credit_count(group) for group in legacy.groups())
        plan = LEGACY_PLAN_RE.search(flat)
        return build_windows(remaining, None, "credits", resets_at, used=used, total=total), \
            plan.group(1).strip() if plan else None
    unit, remaining, included = None, None, None
    if (match := CREDITS_LEFT_RE.search(flat)) is not None:
        unit, remaining = "credits", _credit_count(match.group(1))
        included_match = CREDITS_MONTH_RE.search(flat)
        included = _credit_count(included_match.group(1)) if included_match else None
    elif (match := USD_LEFT_RE.search(flat)) is not None:
        unit, remaining = "USD", to_float(match.group(1))
        included_match = USD_MONTH_RE.search(flat)
        included = to_float(included_match.group(1)) if included_match else None
    windows = build_windows(remaining, included, unit, resets_at)
    if not windows:
        raise ProviderError(_("Unexpected output from `auggie account status`"))
    plan = BOX_PLAN_RE.search(text)
    return windows, plan.group(1).strip() if plan else None


def _json_object(text: str) -> dict[str, Any] | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if isinstance(data, dict) and ("amountRemaining" in data or "planName" in data):
        return data
    return None


def parse_status_output(output: str) -> tuple[list[UsageWindow], str | None]:
    """Whatever ``auggie account status`` printed: JSON, the text box, or an error."""
    text = strip_ansi(output)
    lowered = text.lower()
    if "not currently logged in" in lowered or "auggie login" in lowered:
        raise AuthError(_("Not signed in to Augment"), LOGIN_HINT)
    if "not available for your current plan" in lowered:
        raise ProviderError(_("Augment doesn't report usage for your plan"))
    if "api server not available" in lowered:
        raise ProviderError(_("Augment's API server is not available"), transient=True)
    data = _json_object(text)
    return parse_status_json(data) if data is not None else parse_status_text(text)


# -- credentials ------------------------------------------------------------------


def session_path(ctx: FetchContext) -> Path:
    return ctx.path("~/.augment/session.json")


def _session_fields(data: Any) -> tuple[str, str] | None:
    """(accessToken, tenantURL) when ``data`` passes auggie's own validity check."""
    if not isinstance(data, dict) or not isinstance(data.get("scopes"), list):
        return None
    token, tenant = data.get("accessToken"), data.get("tenantURL")
    if not isinstance(token, str) or not token.strip() or not isinstance(tenant, str) \
            or not tenant.strip():
        return None
    return token.strip(), tenant.strip()


def find_session(ctx: FetchContext) -> tuple[str, str] | None:
    """Return (token, tenant URL), looked up in auggie's own order."""
    raw = (ctx.env.get("AUGMENT_SESSION_AUTH") or "").strip()
    if raw:
        try:
            fields = _session_fields(json.loads(raw))
        except ValueError:
            fields = None
        if fields:
            return fields
    try:
        fields = _session_fields(read_json(session_path(ctx)))
    except (OSError, ValueError):
        fields = None
    if fields:
        return fields
    token = (ctx.env.get("AUGMENT_API_TOKEN") or "").strip()
    url = (ctx.env.get("AUGMENT_API_URL") or "").strip()
    return (token, url) if token and url else None


def billing_url(tenant: str) -> str:
    """``{tenantURL}get-billing-summary``, refusing to send the token anywhere else."""
    base = tenant if tenant.endswith("/") else tenant + "/"  # else the last segment is lost
    parts = urllib.parse.urlsplit(base)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not (host == "augmentcode.com"
                                       or host.endswith(".augmentcode.com")):
        raise ProviderError(_("Augment's sign-in points to an unexpected server ({host})")
                            .format(host=host or tenant))
    return urllib.parse.urljoin(base, "get-billing-summary")


def find_cli(ctx: FetchContext) -> str | None:
    override = (ctx.env.get("AUGGIE_CLI_PATH") or "").strip()
    if override:
        path = str(ctx.path(override))
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return ctx.which("auggie")


class AugmentProvider(Provider):
    id = "augment"
    name = "Augment"
    short = "Au"
    color = "#06B6D4"
    category = "agents"
    homepage = "https://app.augmentcode.com/account"
    source_summary = _("`auggie account status`, or auggie's sign-in → Augment billing API")
    setup_hint = _("Install the auggie CLI (npm install -g @augmentcode/auggie) and run "
                   "`auggie login`.")

    def detect(self, ctx: FetchContext) -> bool:
        return bool(find_cli(ctx)) or find_session(ctx) is not None

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        binary = find_cli(ctx)
        cli_error: ProviderError | None = None
        if binary:
            try:
                windows, plan = parse_status_output(self._run_cli(ctx, binary))
                return self.snapshot(ctx, windows, plan=plan, source=_("auggie CLI"))
            except AuthError:
                raise  # auggie read the same session (and env overrides) we would send
            except ProviderError as exc:
                cli_error = exc
        # Looked up after auggie ran: it removes a session the server has rejected.
        session = find_session(ctx)
        if session is None:
            raise cli_error or NotConfigured(_("Not signed in to Augment"), self.setup_hint)
        windows, plan = self._billing_summary(ctx, *session)
        return self.snapshot(ctx, windows, plan=plan, source=_("Augment API"))

    def _billing_summary(self, ctx: FetchContext, token: str, tenant: str
                         ) -> tuple[list[UsageWindow], str | None]:
        headers = {"Authorization": f"Bearer {token}", "x-request-id": str(uuid.uuid4()),
                   "x-request-session-id": SESSION_ID}
        try:
            data = ctx.http.post_json(billing_url(tenant), {}, headers=headers, timeout=15)
        except HttpError as exc:
            if exc.status == 401:
                raise AuthError(_("Augment rejected the saved sign-in"), LOGIN_HINT) from None
            if exc.status in (404, 501):
                raise ProviderError(_("Augment doesn't report usage for your plan")) from None
            if exc.status == 403:
                raise ProviderError(_("Augment refused the usage request (403)")) from None
            raise from_http_error(exc, login_hint=LOGIN_HINT, service=self.name) from None
        return parse_billing_summary(data)

    @staticmethod
    def _run_cli(ctx: FetchContext, binary: str) -> str:
        # auggie is a Node script: put the node next to it (nvm, npm prefix) on PATH. Credit
        # counts are formatted with the process locale, so pin it to keep them parseable.
        path = os.pathsep.join(part for part in (os.path.dirname(binary), ctx.env.get("PATH"))
                               if part)
        env = {"LC_ALL": "en_US.UTF-8", "PATH": path}
        result = ctx.run([binary, "account", "status", "--json"], timeout=15, extra_env=env)
        output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        if result.returncode != 0 and "unknown option" in output.lower():  # auggie < --json
            result = ctx.run([binary, "account", "status"], timeout=15, extra_env=env)
            output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        if not output.strip():
            raise ProviderError(_("auggie returned no account status"))
        return output
