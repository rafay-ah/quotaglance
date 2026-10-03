"""DeepSeek: prepaid API balance.

Source: the official ``GET /user/balance`` endpoint with your API key. It
reports the total, granted and topped-up balance per currency; DeepSeek has
no 5-hour, weekly or monthly windows. The key can come from GNOME Keyring,
``$DEEPSEEK_API_KEY``, OpenCode's ``auth.json`` or a Claude Code
``settings.json`` pointed at DeepSeek.
"""

from __future__ import annotations

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
from quotaglance.providers.keysources import claude_code_env, opencode_keys
from quotaglance.timefmt import format_amount
from quotaglance.util import to_float

BALANCE_URL = "https://api.deepseek.com/user/balance"
ENV = ("DEEPSEEK_API_KEY", "DEEPSEEK_KEY")


def find_key(ctx: FetchContext) -> tuple[str | None, str]:
    """Return (key, where it came from)."""
    stored = ctx.secret("api_key", (), provider="deepseek")
    if stored:
        return stored, _("Keyring")
    for name in ENV:
        value = (ctx.env.get(name) or "").strip()
        if value:
            return value, f"${name}"
    key = opencode_keys(ctx).get("deepseek")
    if key:
        return key, _("OpenCode sign-in")
    base, token = claude_code_env(ctx)
    if token and "api.deepseek.com" in base:
        return token, _("Claude Code settings")
    return None, ""


def _amount(entry: dict[str, Any], field: str, default: str | None = None) -> float:
    """Balances are decimal strings such as ``"18.47"``."""
    value = entry.get(field, default)
    number = to_float(value) if isinstance(value, (str, int, float)) else None
    if number is None:
        raise ProviderError(_("DeepSeek sent a non-numeric balance"))
    return number


def parse_balance(payload: Any) -> UsageWindow:
    if not isinstance(payload, dict) or not isinstance(payload.get("balance_infos", []), list):
        raise ProviderError(_("Unexpected response from DeepSeek"))
    entries = [(str(info.get("currency") or "USD").upper(), _amount(info, "total_balance"),
                _amount(info, "granted_balance", "0"))
               for info in payload.get("balance_infos") or [] if isinstance(info, dict)]
    # A funded USD balance first, then any funded one, then USD, then whatever is there.
    usd = [entry for entry in entries if entry[0] == "USD"]
    funded = [entry for entry in entries if entry[1] > 0]
    currency, total, granted = next(iter([e for e in usd if e[1] > 0] + funded + usd + entries),
                                    ("USD", 0.0, 0.0))
    usable = payload.get("is_available") is True and total > 0
    parts = [_("{amount} left").format(amount=format_amount(total, currency))]
    if not usable:
        parts.append(_("add credits") if total <= 0 else _("not usable for API calls"))
    elif granted > 0:
        parts.append(_("{amount} granted").format(amount=format_amount(granted, currency)))
    parts += [_("also {amount}").format(amount=format_amount(other, code))
              for code, other, _granted in funded if code != currency]
    return UsageWindow(
        id="balance", label=_("Balance"),
        used_percent=None if usable else 100.0,  # an empty or blocked balance is critical
        used=total, unit=currency, detail=" · ".join(parts))


class DeepSeekProvider(Provider):
    id = "deepseek"
    name = "DeepSeek"
    short = "DS"
    color = "#4D6BFE"
    category = "api"
    homepage = "https://platform.deepseek.com/usage"
    source_summary = _("DeepSeek balance API (API key)")
    setup_hint = _("Create an API key at platform.deepseek.com → API keys, and paste it below.")
    settings = (api_key_setting(ENV),)

    def detect(self, ctx: FetchContext) -> bool:
        return find_key(ctx)[0] is not None

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        key, origin = find_key(ctx)
        if not key:
            raise NotConfigured(_("No DeepSeek API key found"), self.setup_hint)
        try:
            payload = ctx.http.get_json(BALANCE_URL, headers={"Authorization": f"Bearer {key}"})
        except HttpError as exc:
            if exc.status in (401, 403):
                raise AuthError(_("DeepSeek rejected the API key"), self.setup_hint) from None
            raise from_http_error(exc, login_hint=self.setup_hint, service=self.name) from None
        window = parse_balance(payload)
        return self.snapshot(ctx, [window], plan=_("Pay as you go"), source=origin or None)
