"""Moonshot AI / Kimi Open Platform: prepaid API balance.

Source: the official ``GET /v1/users/me/balance`` endpoint with your API
key. There are no quota windows, only the available balance (cash plus
vouchers). Keys are bound to a region: platform.moonshot.ai bills in USD,
platform.moonshot.cn in CNY. The key can come from GNOME Keyring,
``$MOONSHOT_API_KEY``, OpenCode's ``auth.json`` or a Claude Code
``settings.json`` pointed at Moonshot.
"""

from __future__ import annotations

import math
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
    SettingSpec,
    api_key_setting,
    from_http_error,
)
from quotaglance.timefmt import format_amount
from quotaglance.util import read_json

# Only these two origins ever receive the key.
HOSTS = {"international": "https://api.moonshot.ai", "china": "https://api.moonshot.cn"}
CURRENCIES = {"international": "USD", "china": "CNY"}
BALANCE_PATH = "/v1/users/me/balance"
ENV = ("MOONSHOT_API_KEY", "MOONSHOT_KEY")
BALANCE_FIELDS = ("available_balance", "voucher_balance", "cash_balance")
REGION_HINT = _("Check the API region: keys from platform.moonshot.cn don't work on "
                "api.moonshot.ai, and the other way round.")


def _region(value: Any, default: str = "international") -> str:
    value = str(value or "").strip().lower()
    return value if value in HOSTS else default


def _clean(value: str | None) -> str:
    return (value or "").strip().strip("\"'").strip()


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


def _claude_code_env(ctx: FetchContext) -> tuple[str, str | None]:
    """(``ANTHROPIC_BASE_URL``, token) from Claude Code's ``settings.json``."""
    path = ctx.path("~/.claude/settings.json")
    try:
        env = (read_json(path) or {}).get("env") if path.is_file() else None
    except (OSError, ValueError, AttributeError):
        env = None
    if not isinstance(env, dict):
        return "", None
    token = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
    return str(env.get("ANTHROPIC_BASE_URL") or ""), str(token).strip() if token else None


def find_key(ctx: FetchContext) -> tuple[str | None, str, str]:
    """Return (key, region, where it came from)."""
    region = _region(ctx.settings.get("region"))
    stored = ctx.secret("api_key", (), provider="moonshot")
    if stored:
        return stored, region, _("Keyring")
    for name in ENV:
        value = _clean(ctx.env.get(name))
        if value:
            return value, _region(ctx.env.get("MOONSHOT_REGION"), region), f"${name}"
    keys = _opencode_keys(ctx)
    for entry, entry_region in (("moonshotai", "international"), ("moonshotai-cn", "china")):
        if keys.get(entry):
            return keys[entry], entry_region, _("OpenCode sign-in")
    base, token = _claude_code_env(ctx)
    for entry_region, host in HOSTS.items():
        if token and base.startswith(f"{host}/anthropic"):
            return token, entry_region, _("Claude Code settings")
    return None, region, ""


def _money(value: float, currency: str) -> str:
    text = format_amount(abs(value), currency)
    return f"−{text}" if value < 0 else text


def parse_balance(payload: Any, currency: str) -> UsageWindow:
    """The balance window; ``currency`` follows the region, not the payload."""
    if not isinstance(payload, dict) or isinstance(payload.get("code"), bool) or \
            not isinstance(payload.get("code"), int) or \
            not isinstance(payload.get("scode"), str) or \
            not isinstance(payload.get("status"), bool):
        raise ProviderError(_("Unexpected response from Moonshot"))
    code, scode = payload["code"], payload["scode"]
    # Failures can arrive inside an HTTP 200 envelope.
    if code != 0 or not payload["status"]:
        if code in (401, 403) or "unauthorized" in scode.lower():
            raise AuthError(_("Moonshot rejected the API key"), REGION_HINT)
        raise ProviderError(_("Moonshot API error: code {code}, scode {scode}").format(
            code=code, scode=scode))
    data = payload.get("data")
    values = [data.get(field) if isinstance(data, dict) else None for field in BALANCE_FIELDS]
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
               and math.isfinite(value) for value in values):
        raise ProviderError(_("Moonshot sent an invalid balance"))
    available, voucher, cash = (float(value) for value in values)
    parts = [_money(available, currency) if available < 0 else
             _("{amount} left").format(amount=format_amount(available, currency))]
    if cash < 0:  # arrears are paid from the next top-up
        parts.append(_("{amount} in deficit").format(amount=format_amount(-cash, currency)))
    elif voucher > 0:
        parts.append(_("{amount} voucher").format(amount=format_amount(voucher, currency)))
    return UsageWindow(
        id="balance", label=_("Balance"),
        used_percent=100.0 if available <= 0 else None,  # nothing left to spend
        used=available, unit=currency, detail=" · ".join(parts))


class MoonshotProvider(Provider):
    id = "moonshot"
    name = "Moonshot"
    short = "Mo"
    color = "#7C83FD"
    category = "api"
    homepage = "https://platform.moonshot.ai/console/account"
    source_summary = _("Moonshot / Kimi Open Platform balance API (API key)")
    setup_hint = _("Create an API key at platform.moonshot.ai (or platform.moonshot.cn) → "
                   "API Keys, and paste it below.")
    settings = (
        api_key_setting(ENV),
        SettingSpec("region", "choice", _("API region"),
                    _("Where the key was created; it only works there"),
                    choices=(("international", _("International (USD)")),
                             ("china", _("China (CNY)"))),
                    default="international"),
    )

    def detect(self, ctx: FetchContext) -> bool:
        return find_key(ctx)[0] is not None

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        key, region, origin = find_key(ctx)
        if not key:
            raise NotConfigured(_("No Moonshot API key found"), self.setup_hint)
        try:
            payload = ctx.http.get_json(HOSTS[region] + BALANCE_PATH,
                                        headers={"Authorization": f"Bearer {key}"})
        except HttpError as exc:
            if exc.status in (401, 403):
                raise AuthError(_("Moonshot rejected the API key"), REGION_HINT) from None
            raise from_http_error(exc, login_hint=self.setup_hint, service=self.name) from None
        window = parse_balance(payload, CURRENCIES[region])
        plan = _("Pay as you go (China)") if region == "china" else \
            _("Pay as you go (International)")
        return self.snapshot(ctx, [window], plan=plan, source=origin or None)
