"""Kilo Code: the credit balance and Kilo Pass monthly usage.

Source: Kilo's tRPC API, authenticated with the token the Kilo CLI saved
via ``kilo auth login`` (``~/.local/share/kilo/auth.json``; Kilo issues it
for a year and it is never refreshed here), or with a Kilo API token from
GNOME Keyring or ``$KILO_API_KEY``. The CLI's files are only read.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass
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
    api_key_setting,
    from_http_error,
)
from quotaglance.timefmt import format_amount
from quotaglance.util import clamp, dig, first, parse_time, read_json, to_float

PROCEDURES = ("user.getCreditBlocks", "kiloPass.getState", "user.getAutoTopUpPaymentMethod")
OPTIONAL = {2}  # the auto top-up lookup may fail without hiding the usage
BATCH_URL = ("https://app.kilo.ai/api/trpc/" + ",".join(PROCEDURES) + "?batch=1&input="
             + urllib.parse.quote(json.dumps({str(i): {"json": None} for i in range(3)},
                                             separators=(",", ":")), safe=""))
DEFAULT_API_URL = "https://api.kilo.ai"
ORG_HEADER = "X-KILOCODE-ORGANIZATIONID"
LOGIN_HINT = _("Run `kilo auth login`, or paste a Kilo API token in Preferences.")
TIER_NAMES = {"tier_19": "Starter", "tier_49": "Pro", "tier_199": "Expert"}
ACTIVE_PASS = {"active", "past_due", "trialing"}  # what the Kilo CLI accepts


@dataclass
class Credential:
    token: str
    origin: str
    organization: str | None = None
    expires_at: datetime | None = None


# -- credentials ------------------------------------------------------------------


def auth_path(ctx: FetchContext) -> Path:
    return ctx.data_home / "kilo" / "auth.json"


def _cli_entry(data: Any) -> tuple[str | None, str | None, datetime | None]:
    """(token, organization, expiry) of the ``kilo`` entry in an opencode-style auth.json."""
    entry = data.get("kilo") if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return None, None, None
    kind = entry.get("type")
    token = entry.get("key") if kind == "api" else entry.get("access")
    org = entry.get("accountId")  # set when the sign-in is scoped to an organization
    return (token.strip() if isinstance(token, str) and token.strip() else None,
            org.strip() if isinstance(org, str) and org.strip() else None,
            parse_time(entry.get("expires")) if kind == "oauth" else None)


def _legacy_config(ctx: FetchContext) -> tuple[str | None, str | None]:
    """The pre-opencode Kilo Code CLI's ``~/.kilocode/cli/config.json``."""
    try:
        data = read_json(ctx.path("~/.kilocode/cli/config.json"))
    except (OSError, ValueError):
        return None, None
    providers = data.get("providers") if isinstance(data, dict) else None
    for item in providers if isinstance(providers, list) else []:
        if isinstance(item, dict) and item.get("provider") == "kilocode":
            token, org = item.get("kilocodeToken"), item.get("kilocodeOrganizationId")
            if isinstance(token, str) and token.strip():
                return token.strip(), org if isinstance(org, str) and org.strip() else None
    return None, None


def find_credentials(ctx: FetchContext) -> list[Credential]:
    """Every usable token, best first: the CLI's sign-in, then a pasted or env API token."""
    found: list[Credential] = []
    raw = (ctx.env.get("KILO_AUTH_CONTENT") or "").strip()  # overrides the file in the CLI
    try:
        data = json.loads(raw) if raw else read_json(auth_path(ctx))
    except (OSError, ValueError):
        data = None
    token, org, expires = _cli_entry(data)
    if token:
        found.append(Credential(token, _("Kilo CLI sign-in"), org, expires))
    elif not auth_path(ctx).exists():
        token, org = _legacy_config(ctx)
        if token:
            found.append(Credential(token, _("Kilo CLI sign-in"), org))
    stored = ctx.secret("api_key", (), provider="kilo")
    if stored:
        found.append(Credential(stored, _("Keyring")))
    env = (ctx.env.get("KILO_API_KEY") or "").strip()
    if env and env != stored:
        found.append(Credential(env, "$KILO_API_KEY"))
    return found


# -- parsing ----------------------------------------------------------------------


def _flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    if text in ("true", "1", "yes", "enabled", "active", "on"):
        return True
    if text in ("false", "0", "no", "disabled", "inactive", "off", "none"):
        return False
    return None


def _entries(root: Any) -> dict[int, dict[str, Any]]:
    """Batch entries by procedure index: a list, a single entry or a sparse indexed object."""
    if isinstance(root, list):
        return {i: e for i, e in enumerate(root[:len(PROCEDURES)]) if isinstance(e, dict)}
    if isinstance(root, dict):
        if "result" in root or "error" in root:
            return {0: root}
        indexed = {int(k): v for k, v in root.items()
                   if str(k).isdigit() and int(k) < len(PROCEDURES) and isinstance(v, dict)}
        if indexed:
            return indexed
    raise ProviderError(_("Unexpected response from Kilo"))


def _trpc_error(entry: dict[str, Any]) -> ProviderError | None:
    error = entry.get("error")
    if not isinstance(error, dict):
        return None
    code = dig(error, "json", "data", "code") or dig(error, "data", "code") or error.get("code")
    message = dig(error, "json", "message") or error.get("message")
    combined = f"{code or ''} {message or ''}".lower()
    if "unauthorized" in combined or "forbidden" in combined:
        return AuthError(_("Kilo rejected the sign-in"), LOGIN_HINT)
    if "not_found" in combined or "not found" in combined:
        return ProviderError(_("Kilo's usage endpoint was not found"))
    return ProviderError(_("Kilo returned an error: {message}").format(
        message=message or code or "?"))


def _payload(entry: dict[str, Any]) -> Any:
    """Unwrap ``result.data.json`` (superjson), ``result.data`` or ``result.json``."""
    result = entry.get("result")
    if not isinstance(result, dict):
        return None
    data = result.get("data")
    if isinstance(data, dict):
        return data.get("json", data)  # an explicit null payload means "no data"
    return result.get("json")


def _credit_window(payload: Any) -> UsageWindow | None:
    if not isinstance(payload, dict):
        return None
    used = total = remaining = None
    unit = "USD"
    blocks = payload.get("creditBlocks")
    blocks = [b for b in blocks if isinstance(b, dict)] if isinstance(blocks, list) else []
    amounts = [to_float(b.get("amount_mUsd")) for b in blocks]
    balances = [to_float(b.get("balance_mUsd")) for b in blocks]
    if any(v is not None for v in amounts + balances):  # micro-USD per credit block
        if any(v is not None for v in amounts):
            total = max(0.0, sum(v or 0.0 for v in amounts) / 1e6)
        if any(v is not None for v in balances):
            remaining = max(0.0, sum(v or 0.0 for v in balances) / 1e6)
    else:
        unit = "credits"
        nested = payload.get("blocks")
        contexts = [b for b in nested if isinstance(b, dict)] if isinstance(nested, list) else []
        contexts.append(payload)
        for context in contexts:
            used = used if used is not None else to_float(first(
                context, "used", "usedCredits", "consumed", "spent", "creditsUsed"))
            total = total if total is not None else to_float(first(
                context, "total", "totalCredits", "creditsTotal", "limit"))
            remaining = remaining if remaining is not None else to_float(first(
                context, "remaining", "remainingCredits", "creditsRemaining"))
        if used is None and total is None and remaining is None:
            balance = to_float(payload.get("totalBalance_mUsd"))
            if balance is None:
                return None
            unit, used, total = "USD", 0.0, max(0.0, balance / 1e6)
            remaining = total
    if total is None and used is not None and remaining is not None:
        total = used + remaining
    if used is None and total is not None and remaining is not None:
        used = max(0.0, total - remaining)
    if total is None or used is None:
        return None
    return UsageWindow(
        id="credits", label=_("Credits"),
        # An empty balance shows as used up rather than as a healthy empty bar.
        used_percent=clamp(used / total * 100, 0, 100) if total > 0 else 100.0,
        used=used, limit=total, unit=unit,
        detail=_("{amount} left").format(amount=format_amount(remaining, unit))
        if remaining is not None else None)


def _subscription(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if "subscription" in payload:
        sub = payload["subscription"]
        return sub if isinstance(sub, dict) else None
    shape = ("currentPeriodUsageUsd", "currentPeriodBaseCreditsUsd",
             "currentPeriodBonusCreditsUsd", "tier")
    return payload if any(key in payload for key in shape) else None


def _pass_window(payload: Any) -> UsageWindow | None:
    sub = _subscription(payload)
    if sub is None or str(sub.get("status") or "active").lower() not in ACTIVE_PASS:
        return None
    used = max(0.0, to_float(sub.get("currentPeriodUsageUsd")) or 0.0)
    base = to_float(sub.get("currentPeriodBaseCreditsUsd"))
    if base is None:
        return None
    base = max(0.0, base)
    bonus = max(0.0, to_float(sub.get("currentPeriodBonusCreditsUsd")) or 0.0)
    total = base + bonus
    amounts = {"used": format_amount(used, "USD"), "base": format_amount(base, "USD"),
               "bonus": format_amount(bonus, "USD")}
    detail = (_("{used} / {base} (+ {bonus} bonus)") if bonus > 0
              else _("{used} / {base}")).format(**amounts)
    resets_at = None
    for key in ("nextBillingAt", "nextRenewalAt", "renewsAt", "renewAt"):
        resets_at = parse_time(sub.get(key))
        if resets_at:
            break
    return UsageWindow(id="pass", label=_("Kilo Pass"),
                       used_percent=clamp(used / total * 100, 0, 100) if total > 0 else 100.0,
                       resets_at=resets_at, window_seconds=30 * 86400, used=used, limit=total,
                       unit="USD", detail=detail)


def _plan_name(payload: Any) -> str | None:
    sub = _subscription(payload)
    if sub is not None:
        tier = sub.get("tier")
        if isinstance(tier, str) and tier.strip():
            return TIER_NAMES.get(tier.strip(), tier.strip())
        return "Kilo Pass"
    if not isinstance(payload, dict):
        return None
    for value in [payload.get(key) for key in ("planName", "tier", "tierName", "passName",
                                               "subscriptionName")] + [
            dig(payload, "plan", "name"), dig(payload, "pass", "name")]:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _money(amount: float) -> str:
    return f"${amount:,.0f}" if amount == int(amount) else f"${amount:,.2f}"


def _auto_top_up(credits: Any, top_up: Any) -> str | None:
    enabled, method = None, None
    if isinstance(top_up, dict):
        for key in ("enabled", "isEnabled", "active", "status"):
            enabled = _flag(top_up.get(key))
            if enabled is not None:
                break
        method = first(top_up, "paymentMethod", "paymentMethodType", "method", "cardBrand")
        method = method.strip() if isinstance(method, str) and method.strip() else None
        cents = to_float(top_up.get("amountCents"))
        amount = cents / 100 if cents is not None else to_float(
            first(top_up, "amount", "topUpAmount", "amountUsd"))
        if method is None and amount and amount > 0:
            method = _money(amount)
    if enabled is None and isinstance(credits, dict):
        enabled = _flag(credits.get("autoTopUpEnabled"))
    if enabled is None:
        return None
    if not enabled:
        return _("Auto top-up: off")
    return _("Auto top-up: {method}").format(method=method or _("on"))


def parse_batch(root: Any) -> tuple[list[UsageWindow], str | None]:
    """The tRPC batch: credit blocks, Kilo Pass state and auto top-up (in that order)."""
    payloads: dict[int, Any] = {}
    for index, entry in _entries(root).items():
        error = _trpc_error(entry)
        if error is not None:
            if index in OPTIONAL:
                continue
            raise error
        payloads[index] = _payload(entry)
    windows = [w for w in (_credit_window(payloads.get(0)), _pass_window(payloads.get(1))) if w]
    parts = [_plan_name(payloads.get(1)), _auto_top_up(payloads.get(0), payloads.get(2))]
    return windows, " · ".join(p for p in parts if p) or None


def parse_balance(data: Any) -> list[UsageWindow]:
    """``/api/profile/balance`` (the Kilo CLI's own endpoint): a plain USD balance."""
    balance = to_float(data.get("balance")) if isinstance(data, dict) else None
    if balance is None:
        raise ProviderError(_("Unexpected response from Kilo"))
    return [UsageWindow(id="credits", label=_("Credits"), used=balance, unit="USD",
                        detail=_("Remaining balance"))]


class KiloProvider(Provider):
    id = "kilo"
    name = "Kilo Code"
    short = "Kl"
    color = "#EAB308"
    category = "editors"
    homepage = "https://app.kilo.ai/profile"
    source_summary = _("Kilo CLI sign-in or API token → Kilo usage API")
    setup_hint = _("Run `kilo auth login`, or paste a Kilo API token below.")
    settings = (api_key_setting(("KILO_API_KEY",), title=_("API token")),)

    def detect(self, ctx: FetchContext) -> bool:
        return bool(find_credentials(ctx))

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        credentials = find_credentials(ctx)
        if not credentials:
            raise NotConfigured(_("Not signed in to Kilo"), self.setup_hint)
        rejected: AuthError | None = None
        for credential in credentials:  # a rejected token falls through to the next one
            if credential.expires_at and credential.expires_at <= ctx.now():
                rejected = AuthError(_("The Kilo CLI sign-in has expired"), LOGIN_HINT)
                continue
            try:
                windows, plan = self._usage(ctx, credential)
            except AuthError as exc:
                rejected = exc
                continue
            if not windows:
                raise ProviderError(_("Kilo returned no usage for this account"))
            return self.snapshot(ctx, windows, plan=plan, source=credential.origin)
        raise rejected or AuthError(_("Kilo rejected the sign-in"), LOGIN_HINT)

    def _usage(self, ctx: FetchContext, credential: Credential
               ) -> tuple[list[UsageWindow], str | None]:
        headers = {"Authorization": f"Bearer {credential.token}"}
        if credential.organization:
            headers[ORG_HEADER] = credential.organization
        try:
            return parse_batch(ctx.http.get_json(BATCH_URL, headers=headers, timeout=15))
        except HttpError as exc:
            if exc.status in (401, 403):
                raise AuthError(_("Kilo rejected the sign-in"), LOGIN_HINT) from None
            if exc.status != 404:
                raise from_http_error(exc, login_hint=LOGIN_HINT, service=self.name) from None
        # The tRPC batch moved: fall back to the plain balance endpoint the CLI uses.
        base = (ctx.env.get("KILO_API_URL") or "").strip().rstrip("/")
        url = (base if base.startswith("https://") else DEFAULT_API_URL) + "/api/profile/balance"
        try:
            return parse_balance(ctx.http.get_json(url, headers=headers, timeout=15)), None
        except HttpError as exc:
            if exc.status in (401, 403):
                raise AuthError(_("Kilo rejected the sign-in"), LOGIN_HINT) from None
            raise from_http_error(exc, login_hint=LOGIN_HINT, service=self.name) from None
