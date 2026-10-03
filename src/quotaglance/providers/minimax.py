"""MiniMax Token Plan / Coding Plan: 5-hour and weekly quotas.

Source: the ``/v1/token_plan/remains`` API (falling back to the legacy
``coding_plan/remains``), authenticated with a Token Plan key (``sk-cp-…``)
from GNOME Keyring, the usual environment variables, OpenCode's
``auth.json`` or a Claude Code ``settings.json`` pointed at MiniMax.
Pay-as-you-go keys (``sk-api-…``) cannot read plan quotas.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
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
    SettingSpec,
    api_key_setting,
    from_http_error,
)
from quotaglance.providers.keysources import claude_code_env, opencode_keys
from quotaglance.timefmt import format_amount
from quotaglance.util import clamp, dig, parse_time, to_float

HOSTS = {"global": "https://api.minimax.io", "cn": "https://api.minimaxi.com"}
PATHS = ("/v1/token_plan/remains", "/v1/api/openplatform/coding_plan/remains")
ENV = ("MINIMAX_CODING_API_KEY", "MINIMAX_API_KEY")
OPENCODE_ENTRIES = (("minimax-coding-plan", "global"), ("minimax-cn-coding-plan", "cn"),
                    ("minimax", "global"), ("minimax-cn", "cn"))
TITLE_KEYS = ("current_subscribe_title", "plan_name", "combo_title", "current_plan_title")
POINT_KEYS = ("points_balance", "point_balance", "credits_balance", "credit_balance", "balance")
AUTH_CODES = {1004, 2049}
# (total, remaining count, remaining %, status, start, end, remains time) per window.
INTERVAL = ("current_interval_total_count", "current_interval_usage_count",
            "current_interval_remaining_percent", "current_interval_status",
            "start_time", "end_time", "remains_time")
WEEKLY = ("current_weekly_total_count", "current_weekly_usage_count",
          "current_weekly_remaining_percent", "current_weekly_status",
          "weekly_start_time", "weekly_end_time", "weekly_remains_time")
KEY_HINT = _("Paste your Token Plan key (sk-cp-…) below and check the API region.")


def _candidates(ctx: FetchContext) -> Iterator[tuple[str, str, str]]:
    """Every MiniMax key on this machine as (key, region, where it came from)."""
    region = "cn" if ctx.settings.get("region") == "cn" else "global"
    stored = ctx.secret("api_key", (), provider="minimax")
    if stored:
        yield stored, region, _("Keyring")
    for name in ENV:
        value = (ctx.env.get(name) or "").strip()
        if value:
            yield value, region, f"${name}"
    keys = opencode_keys(ctx)
    for entry, entry_region in OPENCODE_ENTRIES:
        key = keys.get(entry)
        if key and (entry.endswith("coding-plan") or key.startswith("sk-cp-")):
            yield key, entry_region, _("OpenCode sign-in")
    base, token = claude_code_env(ctx)
    if token and "minimax.io" in base:
        yield token, "global", _("Claude Code settings")
    elif token and ("minimaxi.com" in base or "minimax.cn" in base):
        yield token, "cn", _("Claude Code settings")


def find_key(ctx: FetchContext) -> tuple[str | None, str, str]:
    """Return (key, region, where it came from), skipping pay-as-you-go keys."""
    for key, region, origin in _candidates(ctx):
        if not key.startswith("sk-api-"):
            return key, region, origin
    return None, "global", ""


def _is_text_lane(name: str) -> bool:
    lower = name.strip().lower()
    return lower == "general" or "minimax-m" in lower or lower.startswith("m2.")


def _resets_at(end: datetime | None, remains: float | None, now: datetime) -> datetime | None:
    if end and end > now:
        return end
    if remains and remains > 0:  # milliseconds, or seconds for small values
        return now + timedelta(seconds=remains / 1000 if remains > 1_000_000 else remains)
    return None


def _lane_window(item: dict[str, Any], fields: tuple[str, ...], now: datetime, *,
                 weekly: bool) -> UsageWindow | None:
    """One window of a ``model_remains`` lane. ``*_usage_count`` is what is LEFT."""
    total, left, left_percent, status = (to_float(item.get(key)) for key in fields[:4])
    start, end = parse_time(item.get(fields[4])), parse_time(item.get(fields[5]))
    unused = left_percent is not None and left_percent >= 100
    if weekly and status == 3 and unused:
        return UsageWindow(id="weekly", label=_("Weekly"), detail=_("Unlimited"))
    if status == 3 and not total and not left and unused:
        return None  # a lane that exists in the schema but is not part of the plan
    counts = bool(total and total > 0 and left is not None)
    if left_percent is not None:
        percent = 100 - left_percent
    elif counts:
        percent = (total - left) / total * 100
    else:
        return None
    seconds = int((end - start).total_seconds()) if start and end and end > start else None
    if weekly:
        wid, label, seconds = "weekly", _("Weekly"), seconds or 7 * 86400
    else:
        daily = seconds is not None and 23 * 3600 <= seconds <= 25 * 3600
        wid, label = "session", _("Daily") if daily else _("5-hour")
    return UsageWindow(
        id=wid, label=label, used_percent=clamp(percent, 0, 100),
        resets_at=_resets_at(end, to_float(item.get(fields[6])), now), window_seconds=seconds,
        used=max(0.0, total - left) if counts else None, limit=total if counts else None,
        unit="prompts" if counts else None)


def _check_status(base: Any) -> None:
    """Raise for a ``base_resp`` error envelope (these arrive with HTTP 200)."""
    code = to_float(base.get("status_code")) if isinstance(base, dict) else None
    if not code:
        return
    message = str(base.get("status_msg") or "").strip()
    lower = message.lower()
    if code in AUTH_CODES or lower == "invalid api key" or any(
            word in lower for word in ("cookie", "log in", "login")):
        raise AuthError(_("MiniMax rejected the API key"), KEY_HINT)
    if code == 1002:
        raise ProviderError(_("Rate limited by MiniMax; will retry later"), transient=True)
    if code == 1008:
        raise ProviderError(_("MiniMax reports an insufficient balance"))
    raise ProviderError(message or _("MiniMax returned error {code}").format(code=int(code)))


def plan_label(data: dict[str, Any], lanes: list[dict[str, Any]]) -> str | None:
    for key in TITLE_KEYS:
        if isinstance(data.get(key), str) and data[key].strip():
            return data[key].strip()
    title = dig(data, "current_combo_card", "title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    # A text pool next to a "not included" video lane is how Plus looks without a title.
    names = {str(lane.get("model_name") or "").strip().lower(): lane for lane in lanes}
    video = names.get("video") or {}
    if "general" in names and to_float(video.get("current_interval_status")) == 3 and (
            to_float(video.get("current_interval_remaining_percent")) or 0) >= 100:
        return "Plus"
    return None


def parse_remains(payload: Any, now: datetime) -> tuple[list[UsageWindow], str | None]:
    if not isinstance(payload, dict):
        raise ProviderError(_("Unexpected response from MiniMax"))
    data = payload["data"] if isinstance(payload.get("data"), dict) else payload
    _check_status(data.get("base_resp") or payload.get("base_resp"))
    lanes = [lane for lane in data.get("model_remains") or [] if isinstance(lane, dict)]
    if not lanes:
        raise ProviderError(_("No coding plan data from MiniMax"), KEY_HINT)
    text = [lane for lane in lanes if _is_text_lane(str(lane.get("model_name") or ""))]
    lane = (text or lanes)[0]
    windows = [window for window in (
        _lane_window(lane, INTERVAL, now, weekly=False),
        _lane_window(lane, WEEKLY, now, weekly=True) if text else None) if window]
    if not windows:
        raise ProviderError(_("No coding plan data from MiniMax"), KEY_HINT)
    points = next((to_float(data[key]) for key in POINT_KEYS
                   if to_float(data.get(key)) is not None), None)
    if points is not None:
        windows.append(UsageWindow(id="points", label=_("Points"), used=points, unit="points",
                                   detail=_("{n} points").format(n=format_amount(points))))
    return windows, plan_label(data, lanes)


class MiniMaxProvider(Provider):
    id = "minimax"
    name = "MiniMax"
    short = "MM"
    color = "#E11D48"
    category = "api"
    homepage = "https://platform.minimax.io/user-center/payment/coding-plan?cycle_type=3"
    source_summary = _("MiniMax Token Plan quota API (API key)")
    setup_hint = _("Copy your Token Plan key (sk-cp-…) from platform.minimax.io → "
                   "Token Plan, and paste it below.")
    settings = (
        api_key_setting(ENV),
        SettingSpec("region", "choice", _("API region"),
                    _("minimax.io (global) or minimaxi.com (China)"),
                    choices=(("global", _("Global")), ("cn", _("China mainland"))),
                    default="global"),
    )

    def detect(self, ctx: FetchContext) -> bool:
        return find_key(ctx)[0] is not None

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        key, region, origin = find_key(ctx)
        if not key:
            if next(_candidates(ctx), None):
                raise NotConfigured(_("Pay-as-you-go keys can't read plan usage"), KEY_HINT)
            raise NotConfigured(_("No MiniMax API key found"), self.setup_hint)
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/json",
                   "Content-Type": "application/json", "MM-API-Source": "QuotaGlance"}
        hosts = ("global", "cn") if region == "global" else ("cn",)
        errors: list[Exception] = []
        missing: list[Exception] = []  # endpoints a host doesn't offer (404/405)
        for host in hosts:
            for path in PATHS:
                try:
                    windows, plan = parse_remains(
                        ctx.http.get_json(HOSTS[host] + path, headers=headers), ctx.now())
                except (HttpError, NetworkError, ProviderError) as exc:
                    error = (from_http_error(exc, login_hint=KEY_HINT, service=self.name)
                             if isinstance(exc, HttpError) else exc)
                    if isinstance(exc, HttpError) and exc.status in (404, 405):
                        missing.append(error)
                        continue
                    fatal = getattr(error, "transient", False) or (
                        isinstance(exc, HttpError) and exc.status not in (401, 403))
                    if fatal and host == hosts[0]:
                        raise error from None
                    errors.append(error)
                    continue
                return self.snapshot(ctx, windows, plan=plan, source=origin or None)
            # Only a key the global host rejected is worth a try on the China host.
            if not any(isinstance(error, AuthError) for error in errors):
                break
        if any(isinstance(error, AuthError) for error in errors):
            raise AuthError(_("MiniMax rejected the API key"), KEY_HINT)
        raise (errors or missing)[0]
