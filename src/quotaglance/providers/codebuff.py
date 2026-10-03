"""Codebuff: the 5-hour block, weekly limit and credit balance.

Source: Codebuff's usage and subscription APIs, authenticated with the
session the Codebuff CLI saved via ``codebuff login``
(``~/.config/manicode/credentials.json``, read-only), or with a Codebuff
API key from GNOME Keyring or ``$CODEBUFF_API_KEY``, which only unlocks the
credit balance. Nothing is refreshed or rewritten.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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
    api_key_setting,
    from_http_error,
)
from quotaglance.timefmt import format_amount
from quotaglance.util import clamp, dig, first, parse_time, read_json, to_float

DEFAULT_BASE = "https://www.codebuff.com"
LOGIN_HINT = _("Run `codebuff login`, or paste a Codebuff API key in Preferences.")
KEYCHAIN_HINT = _("Codebuff keeps its sign-in in the system keychain; paste a Codebuff API key "
                  "in Preferences instead.")
FREE_PLAN = _("Free")


@dataclass
class Credential:
    token: str
    origin: str
    session: bool = False  # the CLI's web session, which also unlocks the subscription limits
    email: str | None = None


# -- credentials ------------------------------------------------------------------


def credentials_path(ctx: FetchContext) -> Path:
    return ctx.path("~/.config/manicode/credentials.json")


def parse_credentials(data: Any) -> tuple[str | None, str | None, bool]:
    """(session token, email, token kept in the keychain) from ``credentials.json``."""
    if not isinstance(data, dict):
        return None, None, False
    profile = data.get("default") if isinstance(data.get("default"), dict) else {}
    token = next((value.strip() for value in (profile.get("authToken"), data.get("authToken"))
                  if isinstance(value, str) and value.strip()), None)
    email = profile.get("email") if isinstance(profile.get("email"), str) else None
    return token, email or None, profile.get("tokenStore") == "keychain"


def find_credentials(ctx: FetchContext) -> tuple[list[Credential], bool]:
    """(credentials best first, whether the CLI's token sits in the keychain instead)."""
    try:
        token, email, keychain = parse_credentials(read_json(credentials_path(ctx)))
    except (OSError, ValueError):
        token, email, keychain = None, None, False
    found = [Credential(token, _("Codebuff CLI sign-in"), True, email)] if token else []
    stored = ctx.secret("api_key", (), provider="codebuff")
    if stored:
        found.append(Credential(stored, _("Keyring")))
    env = (ctx.env.get("CODEBUFF_API_KEY") or "").strip()
    if env and env != stored:
        found.append(Credential(env, "$CODEBUFF_API_KEY"))
    return found, keychain and not token


def base_url(ctx: FetchContext) -> str:
    raw = (ctx.env.get("CODEBUFF_API_URL") or "").strip().rstrip("/")
    if not raw:
        return DEFAULT_BASE
    if raw.startswith("https://"):
        return raw
    if re.fullmatch(r"[A-Za-z0-9.-]+(?::\d+)?", raw):
        return f"https://{raw}"
    raise ProviderError(_("CODEBUFF_API_URL must use HTTPS or be a bare host name"))


# -- parsing ----------------------------------------------------------------------


def parse_usage(data: Any) -> UsageWindow | None:
    """``POST /api/v1/usage``: credits used this cycle against everything still available."""
    if not isinstance(data, dict):
        raise ProviderError(_("Unexpected response from Codebuff"))
    used = to_float(first(data, "usage", "used"))
    total = to_float(first(data, "quota", "limit"))
    remaining = to_float(first(data, "remainingBalance", "remaining"))
    if total is None and used is not None and remaining is not None:
        total = used + remaining  # the remaining balance includes purchased credits
    if used is None and total is not None and remaining is not None:
        used = max(0.0, total - remaining)
    if used is None and remaining is None:
        return None
    details = []
    if remaining is not None:
        details.append(_("{amount} left").format(amount=format_amount(remaining, "credits")))
    if first(data, "autoTopupEnabled", "auto_topup_enabled") is True:
        details.append(_("auto top-up"))
    return UsageWindow(
        id="credits", label=_("Credits"),
        # No usable total: show it as used up rather than as a misleading healthy bar.
        used_percent=clamp((used or 0.0) / total * 100, 0, 100) if total and total > 0
        else 100.0,
        resets_at=parse_time(data.get("next_quota_reset")), window_seconds=30 * 86400,
        used=used, limit=total if total and total > 0 else None, unit="credits",
        detail=" · ".join(details) or None)


def _plan_text(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(int(value))
    if isinstance(value, str) and value.strip():
        text = value.strip()
        return text.title() if text.islower() else text
    return None


def parse_subscription(data: Any) -> tuple[list[UsageWindow], str | None, str | None]:
    """``GET /api/user/subscription`` → (5-hour block + weekly windows, plan, email)."""
    if not isinstance(data, dict):
        raise ProviderError(_("Unexpected response from Codebuff"))
    sub = data.get("subscription") if isinstance(data.get("subscription"), dict) else {}
    rate = data.get("rateLimit") if isinstance(data.get("rateLimit"), dict) else {}
    limits = data.get("limits") if isinstance(data.get("limits"), dict) else {}
    reason = str(rate.get("reason") or "") if rate.get("limited") else ""
    windows: list[UsageWindow] = []
    block_used, block_limit = to_float(rate.get("blockUsed")), to_float(rate.get("blockLimit"))
    if block_used is not None and block_limit and block_limit > 0:  # absent until a block starts
        hours = to_float(limits.get("blockDurationHours")) or 5.0
        windows.append(UsageWindow(
            id="session", label=_("5-hour"),
            used_percent=clamp(block_used / block_limit * 100, 0, 100),
            resets_at=parse_time(rate.get("blockResetsAt")), window_seconds=int(hours * 3600),
            used=block_used, limit=block_limit, unit="credits",
            detail=_("Limit reached") if reason == "block_exhausted" else None))
    weekly_used = to_float(first(rate, "weeklyUsed", "used"))
    weekly_limit = to_float(first(rate, "weeklyLimit", "limit"))
    if weekly_limit and weekly_limit > 0:
        percent = (weekly_used or 0.0) / weekly_limit * 100
    else:
        weekly_limit, percent = None, to_float(rate.get("weeklyPercentUsed"))
    if percent is not None:
        windows.append(UsageWindow(
            id="weekly", label=_("Weekly"), used_percent=clamp(percent, 0, 100),
            resets_at=parse_time(rate.get("weeklyResetsAt")), window_seconds=7 * 86400,
            used=weekly_used if weekly_limit else None, limit=weekly_limit,
            unit="credits" if weekly_limit else None,
            detail=_("Limit reached") if reason == "weekly_limit" else None))
    plan = next((text for text in map(_plan_text, (
        data.get("displayName"), sub.get("displayName"), sub.get("tier"), data.get("tier"),
        sub.get("scheduledTier"))) if text), None)
    if plan is None and data.get("hasSubscription") is False:
        plan = FREE_PLAN
    email = first(data, "email") or dig(data, "user", "email")
    return windows, plan, email if isinstance(email, str) else None


class CodebuffProvider(Provider):
    id = "codebuff"
    name = "Codebuff"
    short = "Cb"
    color = "#84CC16"
    category = "agents"
    homepage = "https://www.codebuff.com/usage"
    source_summary = _("Codebuff CLI sign-in or API key → Codebuff usage API")
    setup_hint = _("Run `codebuff login`, or paste a Codebuff API key below.")
    settings = (api_key_setting(("CODEBUFF_API_KEY",)),)

    def detect(self, ctx: FetchContext) -> bool:
        credentials, keychain = find_credentials(ctx)
        return bool(credentials) or keychain

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        credentials, keychain = find_credentials(ctx)
        if not credentials:
            if keychain:
                raise NotConfigured(_("Codebuff's sign-in is in the system keychain"),
                                    KEYCHAIN_HINT)
            raise NotConfigured(_("Not signed in to Codebuff"), self.setup_hint)
        base = base_url(ctx)
        rejected: AuthError | None = None
        for credential in credentials:  # a rejected token falls through to the next one
            try:
                usage = self._usage(ctx, base, credential.token)
            except AuthError as exc:
                rejected = exc
                continue
            windows, plan, account = [], None, credential.email
            if credential.session:  # API keys can't read the subscription
                subscription = self._subscription(ctx, base, credential.token)
                if subscription:
                    windows, plan, email = subscription
                    account = account or email
            credits = parse_usage(usage)
            if credits:
                windows.append(credits)
            purchased = (to_float(dig(usage, "balanceBreakdown", "purchase")) or 0) > 0
            if plan == FREE_PLAN and purchased:
                plan = _("Pay as you go")
            if not windows:
                raise ProviderError(_("Codebuff reported no usage for this account"))
            return self.snapshot(ctx, windows, plan=plan, account=account,
                                 source=credential.origin)
        raise rejected or AuthError(_("Codebuff rejected the sign-in"), LOGIN_HINT)

    def _usage(self, ctx: FetchContext, base: str, token: str) -> Any:
        # The CLI sends the token in the body, other clients in the header: send both.
        body = {"fingerprintId": "quotaglance-usage", "authToken": token}
        try:
            return ctx.http.post_json(f"{base}/api/v1/usage", body,
                                      headers={"Authorization": f"Bearer {token}"}, timeout=15)
        except HttpError as exc:
            if exc.status in (401, 403):
                raise AuthError(_("Codebuff rejected the sign-in"), LOGIN_HINT) from None
            raise from_http_error(exc, login_hint=LOGIN_HINT, service=self.name) from None

    @staticmethod
    def _subscription(ctx: FetchContext, base: str, token: str
                      ) -> tuple[list[UsageWindow], str | None, str | None] | None:
        """Best effort: the credit balance stands on its own when this call fails."""
        try:
            return parse_subscription(ctx.http.get_json(
                f"{base}/api/user/subscription", timeout=5,
                headers={"Authorization": f"Bearer {token}",
                         "Cookie": f"next-auth.session-token={token};"}))
        except (HttpError, NetworkError, ProviderError):
            return None
