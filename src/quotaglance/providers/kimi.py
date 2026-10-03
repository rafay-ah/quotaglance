"""Kimi Code (Kimi For Coding): 5-hour, weekly and monthly quotas.

Source: the ``/coding/v1/usages`` API that the official Kimi Code CLI
uses, authenticated with a Kimi Code API key (``sk-kimi-…``) from GNOME
Keyring, the environment, OpenCode's ``auth.json`` or a Claude Code
``settings.json`` pointed at Kimi. As a last resort the CLI's own sign-in
(``~/.kimi-code/credentials``) is borrowed while its short-lived token is
still fresh; it is only read, never refreshed.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from quotaglance import __version__
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
from quotaglance.providers.keysources import claude_code_env, opencode_keys
from quotaglance.timefmt import format_amount
from quotaglance.util import clamp, dig, first, parse_time, read_json, title_case_plan, to_float

HOSTS = {"china": "https://api.kimi.com/coding/v1", "international": "https://api.kimi.ai/coding/v1"}
ENV = ("KIMI_CODE_API_KEY", "KIMI_API_KEY")
# CLI credentials are never forwarded to a custom endpoint.
OVERRIDE_ENV = ("KIMI_CODE_BASE_URL", "KIMI_CODE_OAUTH_HOST", "KIMI_OAUTH_HOST")
OPENCODE_ENTRIES = (("kimi-code-plan-cn", "china"), ("kimi-code-plan-global", "international"))
# The CLI's credential slots: the default one (kimi.com) and the kimi.ai one, whose
# suffix is sha256('{"oauthHost":"https://auth.kimi.ai","baseUrl":"…/coding/v1"}')[:16].
CLI_SLOTS = (("kimi-code.json", "china"), ("kimi-code-env-0e4f99c69cc27850.json", "international"))
CLI_SOURCE = _("Kimi Code CLI")
UNIT_MINUTES = {"TIME_UNIT_SECOND": 1 / 60, "TIME_UNIT_MINUTE": 1, "TIME_UNIT_HOUR": 60,
                "TIME_UNIT_DAY": 1440}
LEVELS = {"LEVEL_FREE": "Adagio", "LEVEL_TRIAL": "Andante", "LEVEL_BASIC": "Moderato",
          "LEVEL_INTERMEDIATE": "Allegretto", "LEVEL_ADVANCED": "Allegro"}
REGION_HINT = _("Check the key and the region (kimi.com or kimi.ai) in Preferences.")


def usages_url(ctx: FetchContext, region: str) -> str:
    override = (ctx.env.get("KIMI_CODE_BASE_URL") or "").strip().rstrip("/")
    if override.startswith("https://"):
        if override.endswith("/coding/v1"):
            return override + "/usages"
        return override + ("/v1/usages" if override.endswith("/coding") else "/coding/v1/usages")
    return HOSTS.get(region, HOSTS["china"]) + "/usages"


def cli_home(ctx: FetchContext) -> Path:
    return ctx.env_path("KIMI_CODE_HOME", "~/.kimi-code")


def cli_token(ctx: FetchContext) -> tuple[str | None, str, bool]:
    """(fresh CLI access token, its region, whether the CLI is signed in at all)."""
    if any((ctx.env.get(name) or "").strip() for name in OVERRIDE_ENV):
        return None, "china", False
    home = cli_home(ctx)
    try:
        marker = (home / "region").read_text(encoding="utf-8").strip().lower()
    except OSError:
        marker = ""
    signed_in = False
    for name, region in (CLI_SLOTS[::-1] if marker == "global" else CLI_SLOTS):
        path = home / "credentials" / name
        if not path.is_file():
            continue
        signed_in = True
        try:
            data = read_json(path)
        except (OSError, ValueError):
            continue
        token = data.get("access_token") if isinstance(data, dict) else None
        expires = to_float(data.get("expires_at")) if isinstance(data, dict) else None
        # Tokens live ~15 minutes; refreshing would rotate the CLI's own refresh token.
        if isinstance(token, str) and token.strip() and expires and \
                expires > ctx.now().timestamp() + 60:
            return token.strip(), region, True
    return None, "china", signed_in


def find_key(ctx: FetchContext) -> tuple[str | None, str, str]:
    """Return (key, region, where it came from)."""
    region = "international" if ctx.settings.get("region") == "international" else "china"
    stored = ctx.secret("api_key", (), provider="kimi")
    if stored:
        return stored, region, _("Keyring")
    for name in ENV:
        value = (ctx.env.get(name) or "").strip()
        if value:
            return value, region, f"${name}"
    keys = opencode_keys(ctx)
    for entry, entry_region in (*OPENCODE_ENTRIES, ("kimi-for-coding", region)):
        if keys.get(entry):
            return keys[entry], entry_region, _("OpenCode sign-in")
    base, token = claude_code_env(ctx)
    if token and base.startswith("https://api.kimi.com/coding"):
        return token, "china", _("Claude Code settings")
    if token and base.startswith("https://api.kimi.ai/coding"):
        return token, "international", _("Claude Code settings")
    token, cli_region = cli_token(ctx)[:2]
    if token:
        return token, cli_region, CLI_SOURCE
    return None, region, ""


def _ascii(value: str) -> str:
    return "".join(char for char in value if " " <= char <= "~").strip() or "unknown"


def cli_headers(ctx: FetchContext) -> dict[str, str]:
    """The device identity the official CLI sends along with its own token."""
    system = os.uname()
    machine = {"aarch64": "arm64"}.get(system.machine, system.machine)
    headers = {
        "X-Msh-Platform": "kimi_code_cli",
        "X-Msh-Version": __version__,
        "X-Msh-Device-Name": _ascii(system.nodename),
        "X-Msh-Device-Model": _ascii(f"Linux {system.release} {machine}"),
        "X-Msh-Os-Version": _ascii(system.release),
    }
    try:
        device = (cli_home(ctx) / "device_id").read_text(encoding="utf-8").strip()
    except OSError:
        device = ""  # only the CLI creates it; QuotaGlance never writes there
    if device:
        headers["X-Msh-Device-Id"] = _ascii(device)
    return headers


# -- parsing ----------------------------------------------------------------------


def _ratio(pool: Any) -> tuple[float, datetime | None] | None:
    """(used percent, reset) of a ``usages.limit_*`` pool; ``used_ratio`` is 0..1."""
    ratio = to_float(pool.get("used_ratio")) if isinstance(pool, dict) else None
    if ratio is None:
        return None
    return clamp(ratio, 0, 1) * 100, parse_time(pool.get("reset_time"))


def _counts(detail: Any) -> tuple[float, float] | None:
    """(used, limit) of a legacy request counter; the numbers usually arrive as strings."""
    limit = to_float(detail.get("limit")) if isinstance(detail, dict) else None
    if not limit or limit <= 0:
        return None
    used = to_float(detail.get("used"))
    if used is not None and used >= 0:
        return used, limit  # may exceed the limit during overage
    remaining = to_float(detail.get("remaining"))
    if remaining is not None and 0 <= remaining <= limit:
        return limit - remaining, limit
    return None


def _count_reset(detail: Any) -> datetime | None:
    if not isinstance(detail, dict):
        return None
    return parse_time(first(detail, "resetTime", "resetAt", "reset_time", "reset_at"))


def _minutes(entry: dict[str, Any]) -> int | None:
    window = entry.get("window") if isinstance(entry.get("window"), dict) else {}
    duration = to_float(window.get("duration"))
    factor = UNIT_MINUTES.get(str(window.get("timeUnit") or "TIME_UNIT_MINUTE"))
    return int(duration * factor) if duration and factor else None


def _rate_limit(data: dict[str, Any]) -> tuple[Any, int]:
    """The legacy request window from ``limits[]`` (the 5-hour one when present)."""
    entries = [(entry, _minutes(entry)) for entry in data.get("limits") or []
               if isinstance(entry, dict)]
    for entry, minutes in entries:
        if minutes == 300:
            return entry.get("detail"), 300
    if entries:
        return entries[0][0].get("detail"), entries[0][1] or 300
    return None, 300


def _booster(wallet: Any) -> UsageWindow | None:
    """The optional "extra usage" wallet; amounts are fixed-point (1,000,000 per cent)."""
    balance = wallet.get("balance") if isinstance(wallet, dict) else None
    total = to_float(balance.get("amount")) if isinstance(balance, dict) else None
    left = to_float(balance.get("amountLeft")) if isinstance(balance, dict) else None
    if not total or total <= 0 or left is None:
        return None
    total, left = total / 1e8, clamp(left / 1e8, 0, total / 1e8)
    return UsageWindow(
        id="extra_usage", label=_("Extra usage"),
        used_percent=clamp((total - left) / total * 100, 0, 100),
        used=total - left, limit=total, unit="USD",
        detail=_("{amount} left").format(amount=format_amount(left, "USD")))


def plan_label(data: dict[str, Any]) -> str | None:
    level = dig(data, "user", "membership", "level")
    if not isinstance(level, str) or level.strip() in ("", "LEVEL_UNSPECIFIED"):
        return None
    level = level.strip()
    if data.get("version") in (None, "GOODS_VERSION_V1") and level in LEVELS:
        return LEVELS[level]
    return title_case_plan(level.removeprefix("LEVEL_"))


def parse_usages(data: Any) -> tuple[list[UsageWindow], str | None]:
    if not isinstance(data, dict):
        raise ProviderError(_("Unexpected response from Kimi"))
    pools = data.get("usages") if isinstance(data.get("usages"), dict) else {}
    monthly = _ratio(pools.get("limit_month_total"))
    rate, rate_minutes = _rate_limit(data)
    windows: list[UsageWindow] = []
    for wid, label, pool, detail, minutes, pool_minutes in (
            ("session", _("5-hour"), "limit_5h", rate, rate_minutes, 300),
            ("weekly", _("Weekly"), "limit_7d", data.get("usage"), 10080, 10080)):
        ratio = _ratio(pools.get(pool))
        counts = _counts(detail)
        count_reset = _count_reset(detail)
        # A zero ratio next to real counters for the same window is a placeholder.
        if ratio and counts and ratio[0] == 0 and monthly is None and counts[0] > 0 and \
                minutes == pool_minutes and count_reset and ratio[1] and \
                abs((count_reset - ratio[1]).total_seconds()) <= 2:
            ratio = None
        if ratio:
            windows.append(UsageWindow(id=wid, label=label, used_percent=ratio[0],
                                       resets_at=ratio[1], window_seconds=pool_minutes * 60))
        elif counts:
            used, limit = counts
            windows.append(UsageWindow(
                id=wid, label=label if minutes == pool_minutes else _("Session"),
                used_percent=clamp(used / limit * 100, 0, 100), resets_at=count_reset,
                window_seconds=minutes * 60, used=used, limit=limit, unit="requests"))
    if monthly:
        code = _ratio(pools.get("limit_month_code"))
        detail = _("Code {code:.1f}% · other {other:.1f}%").format(
            code=code[0], other=max(0.0, monthly[0] - code[0])) if code else None
        windows.append(UsageWindow(id="monthly", label=_("Monthly"), used_percent=monthly[0],
                                   resets_at=monthly[1], window_seconds=30 * 86400,
                                   detail=detail))
    if not windows:
        raise ProviderError(_("Kimi reported no usage for this key"), REGION_HINT)
    extra = _booster(data.get("boosterWallet"))
    if extra:
        windows.append(extra)
    return windows, plan_label(data)


class KimiProvider(Provider):
    id = "kimi"
    name = "Kimi Code"
    short = "Ki"
    color = "#0EA5E9"
    category = "api"
    homepage = "https://www.kimi.com/code/console"
    source_summary = _("Kimi Code usage API (API key or Kimi Code CLI)")
    setup_hint = _("Create an API key in the Kimi Code console (kimi.com/code/console) and "
                   "paste it below, or sign in with the `kimi` CLI.")
    settings = (
        api_key_setting(ENV),
        SettingSpec("region", "choice", _("API region"),
                    _("kimi.com (default) or kimi.ai (international)"),
                    choices=(("china", "kimi.com"), ("international", "kimi.ai")),
                    default="china"),
    )

    def detect(self, ctx: FetchContext) -> bool:
        return find_key(ctx)[0] is not None or cli_token(ctx)[2]

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        key, region, origin = find_key(ctx)
        if not key:
            if cli_token(ctx)[2]:
                raise AuthError(_("The Kimi Code CLI session has expired"),
                                _("Run `kimi` to renew it, or add an API key below."))
            raise NotConfigured(_("No Kimi Code API key found"), self.setup_hint)
        headers = {"Authorization": f"Bearer {key}"}
        if origin == CLI_SOURCE:
            headers.update(cli_headers(ctx))
        try:
            data = ctx.http.get_json(usages_url(ctx, region), headers=headers)
        except HttpError as exc:
            if exc.status in (401, 403) and origin == CLI_SOURCE:
                raise AuthError(_("Kimi rejected the Kimi Code CLI session"),
                                _("Run `kimi` to renew it, or add an API key below.")) from None
            if exc.status in (401, 403):
                raise AuthError(_("Kimi rejected the API key ({status})").format(
                    status=exc.status), REGION_HINT) from None
            if exc.status == 404:
                raise ProviderError(_("Kimi Code usage isn't available for this key"),
                                    REGION_HINT) from None
            raise from_http_error(exc, login_hint=self.setup_hint, service=self.name) from None
        windows, plan = parse_usages(data)
        return self.snapshot(ctx, windows, plan=plan, source=origin or None)
