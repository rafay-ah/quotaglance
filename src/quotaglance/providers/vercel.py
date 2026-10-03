"""Vercel AI Gateway: the team's remaining credit balance.

Source: the official ``GET /v1/credits`` endpoint of the AI Gateway with an
AI Gateway API key (GNOME Keyring, ``$AI_GATEWAY_API_KEY`` or OpenCode's
``auth.json``). The balance is team-wide and has no limit or reset; the
key decides which team you see.
"""

from __future__ import annotations

import re
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
from quotaglance.providers.keysources import opencode_keys
from quotaglance.timefmt import format_amount

CREDITS_URL = "https://ai-gateway.vercel.sh/v1/credits"
ENV = ("AI_GATEWAY_API_KEY",)
DECIMAL = re.compile(r"-?\d+(?:\.\d+)?")


def find_key(ctx: FetchContext) -> tuple[str | None, str]:
    """Return (key, where it came from)."""
    stored = ctx.secret("api_key", (), provider="vercel")
    if stored:
        return stored, _("Keyring")
    for name in ENV:
        value = (ctx.env.get(name) or "").strip()
        if value:
            return value, f"${name}"
    key = opencode_keys(ctx).get("vercel")
    if key:
        return key, _("OpenCode sign-in")
    return None, ""


def _money(value: float) -> str:
    text = format_amount(abs(value), "USD")
    return f"−{text}" if value < 0 else text


def parse_credits(payload: Any) -> UsageWindow:
    """``{"balance": "95.50", "total_used": "4.50"}``: USD as decimal strings."""
    values = [payload.get(key) if isinstance(payload, dict) else None
              for key in ("balance", "total_used")]
    if not all(isinstance(value, str) and DECIMAL.fullmatch(value.strip()) for value in values):
        raise ProviderError(_("Vercel AI Gateway returned an unrecognized credit balance"))
    balance, spent = (float(value) for value in values)
    if spent < 0:
        raise ProviderError(_("Vercel AI Gateway returned an unrecognized credit balance"))
    left = _money(balance) if balance < 0 else _("{amount} left").format(amount=_money(balance))
    return UsageWindow(
        id="balance", label=_("Credits"),
        used_percent=100.0 if balance <= 0 else None,  # nothing left to spend
        used=balance, unit="USD",
        detail=_("{left} · {spent} spent").format(left=left, spent=format_amount(spent, "USD")))


class VercelProvider(Provider):
    id = "vercel"
    name = "Vercel AI Gateway"
    short = "▲"
    color = "#525252"
    category = "api"
    homepage = "https://vercel.com/d?to=%2F%5Bteam%5D%2F%7E%2Fai-gateway"
    source_summary = _("AI Gateway credits API (API key)")
    setup_hint = _("Create an API key in the Vercel dashboard → AI Gateway → API Keys, "
                   "and paste it below.")
    settings = (api_key_setting(ENV),)

    def detect(self, ctx: FetchContext) -> bool:
        return find_key(ctx)[0] is not None

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        key, origin = find_key(ctx)
        if not key:
            raise NotConfigured(_("No AI Gateway API key found"), self.setup_hint)
        try:
            payload = ctx.http.get_json(CREDITS_URL, headers={"Authorization": f"Bearer {key}"})
        except HttpError as exc:
            if exc.status == 401:
                raise AuthError(_("Vercel rejected the AI Gateway API key"),
                                self.setup_hint) from None
            if exc.status == 403:
                raise AuthError(_("This AI Gateway key may not read the team's credits"),
                                self.setup_hint) from None
            raise from_http_error(exc, login_hint=self.setup_hint, service=self.name) from None
        window = parse_credits(payload)
        return self.snapshot(ctx, [window], plan=_("Team credits"), source=origin or None)
