"""z.ai / Zhipu BigModel GLM Coding Plan: 5-hour, weekly and MCP tool quotas.

Source: the quota "monitor" endpoint used by z.ai's own usage plugin,
authenticated with your API key. The key can come from GNOME Keyring, the
usual environment variables, or files your tools already keep: OpenCode's
``auth.json`` or a Claude Code ``settings.json`` pointed at z.ai.
"""

from __future__ import annotations

from datetime import datetime, timedelta
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
from quotaglance.util import clamp, parse_time, read_json, title_case_plan, to_float

HOSTS = {"global": "https://api.z.ai", "cn": "https://open.bigmodel.cn"}
QUOTA_PATH = "/api/monitor/usage/quota/limit"
GLOBAL_ENV = ("Z_AI_API_KEY", "ZAI_API_KEY", "ZHIPU_API_KEY")
CN_ENV = ("BIGMODEL_API_KEY", "ZHIPUAI_API_KEY", "GLM_API_KEY")
UNIT_MINUTES = {1: 1440, 3: 60, 5: 1, 6: 10080}
AUTH_CODES = {401, 1000, 1001, 1002, 1003, 1004}


def find_key(ctx: FetchContext) -> tuple[str | None, str, str]:
    """Return (key, region, where it came from)."""
    region = str(ctx.settings.get("region") or "auto")
    stored = ctx.secret("api_key", (), provider="zai")
    if stored:
        return stored, "cn" if region == "cn" else "global", _("Keyring")
    for name in GLOBAL_ENV:
        if ctx.env.get(name, "").strip() and region != "cn":
            return ctx.env[name].strip(), "global", f"${name}"
    for name in CN_ENV:
        if ctx.env.get(name, "").strip() and region != "global":
            return ctx.env[name].strip(), "cn", f"${name}"
    auth = ctx.data_home / "opencode" / "auth.json"
    if auth.is_file():
        try:
            data = read_json(auth)
        except (OSError, ValueError):
            data = {}
        for entry, entry_region in (("zai-coding-plan", "global"),
                                    ("zhipuai-coding-plan", "cn"),
                                    ("zai", "global"), ("zhipuai", "cn")):
            key = (data.get(entry) or {}).get("key") if isinstance(data, dict) else None
            if key:
                return str(key).strip(), entry_region, _("OpenCode sign-in")
    settings = ctx.path("~/.claude/settings.json")
    if settings.is_file():
        try:
            env = (read_json(settings) or {}).get("env") or {}
        except (OSError, ValueError, AttributeError):
            env = {}
        base = str(env.get("ANTHROPIC_BASE_URL") or "")
        token = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
        if token and "api.z.ai" in base:
            return str(token).strip(), "global", _("Claude Code settings")
        if token and "bigmodel.cn" in base:
            return str(token).strip(), "cn", _("Claude Code settings")
    return None, "global", ""


def _limit_used(item: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
    """(percent, used, total) for one ``limits[]`` entry."""
    percent = to_float(item.get("percentage"))
    total = to_float(item.get("usage"))
    current = to_float(item.get("currentValue"))
    remaining = to_float(item.get("remaining"))
    used = None
    if total and total > 0 and (remaining is not None or current is not None):
        if remaining is not None:
            used = max(total - remaining, current if current is not None else total - remaining)
        else:
            used = current
        percent = clamp(used, 0, total) / total * 100
    return (None if percent is None else clamp(percent, 0, 100)), used, total


def parse_quota(payload: Any, now: datetime) -> tuple[list[UsageWindow], str | None]:
    if not isinstance(payload, dict):
        raise ProviderError(_("Unsupported z.ai quota format"))
    code = payload.get("code")
    if payload.get("success") is False or (code is not None and to_float(code) != 200):
        message = str(payload.get("msg") or _("z.ai returned an error"))
        if to_float(code) in AUTH_CODES:
            raise AuthError(_("z.ai rejected the API key"),
                            _("Check the key and the API region in Preferences."))
        raise ProviderError(message)
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("limits"), list):
        raise ProviderError(_("Unsupported z.ai quota format"),
                            _("Check the Usage Dashboard on z.ai."))
    plan = None
    for key in ("planName", "plan", "plan_type", "packageName", "level"):
        if isinstance(data.get(key), str) and data[key].strip():
            plan = data[key].strip()
            break
    if plan and plan.islower():
        plan = title_case_plan(plan)

    pools: list[tuple[int | None, dict[str, Any]]] = []
    mcp: dict[str, Any] | None = None
    for item in data["limits"]:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind in ("TOKENS_LIMIT", "CREDIT_LIMIT"):
            minutes = UNIT_MINUTES.get(int(to_float(item.get("unit")) or 0))
            number = to_float(item.get("number"))
            duration = int(minutes * number) if minutes and number else None
            pools.append((duration, item))
        elif kind == "TIME_LIMIT":
            mcp = item

    windows: list[UsageWindow] = []
    pools.sort(key=lambda entry: entry[0] if entry[0] is not None else 10**9)
    chosen = [(True, *pools[0])] if pools else []
    if len(pools) >= 2:
        chosen.append((False, *pools[-1]))
    for is_session, duration, item in chosen:
        percent, used, total = _limit_used(item)
        resets_at = parse_time(item.get("nextResetTime"))
        if is_session and resets_at and duration and resets_at > now + timedelta(
                minutes=duration, seconds=60):
            resets_at = None  # impossible reset time from the server; keep the percent
        unit = "credits" if item.get("type") == "CREDIT_LIMIT" else "tokens"
        if is_session:
            label = _("5-hour") if duration == 300 else _("Session")
        else:
            label = _("Weekly") if duration == 10080 else _("Long window")
        windows.append(UsageWindow(
            id="session" if is_session else "weekly", label=label,
            used_percent=percent, resets_at=resets_at,
            window_seconds=duration * 60 if duration else None,
            used=used, limit=total if used is not None else None,
            unit=unit if used is not None else None))
    if mcp is not None:
        percent, used, total = _limit_used(mcp)
        details = [f"{d.get('modelCode')} {int(to_float(d.get('usage')) or 0)}"
                   for d in mcp.get("usageDetails") or [] if isinstance(d, dict)]
        windows.append(UsageWindow(
            id="mcp", label=_("MCP tools"), used_percent=percent,
            resets_at=parse_time(mcp.get("nextResetTime")), window_seconds=30 * 86400,
            used=used, limit=total if used is not None else None,
            unit="calls" if used is not None else None,
            detail=" · ".join(details) or None))
    return windows, plan


class ZaiProvider(Provider):
    id = "zai"
    name = "z.ai"
    short = "Z"
    color = "#3B5BDB"
    category = "api"
    homepage = "https://z.ai/manage-apikey/coding-plan/personal/my-plan"
    source_summary = _("GLM Coding Plan quota API (API key)")
    setup_hint = _("Paste your z.ai API key below, or sign in to z.ai in OpenCode.")
    settings = (
        api_key_setting(GLOBAL_ENV + CN_ENV),
        SettingSpec("region", "choice", _("API region"), _("z.ai (global) or BigModel (China)"),
                    choices=(("auto", _("Automatic")), ("global", "z.ai"),
                             ("cn", _("BigModel (China)"))), default="auto"),
    )

    def detect(self, ctx: FetchContext) -> bool:
        return find_key(ctx)[0] is not None

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        key, region, origin = find_key(ctx)
        if not key:
            raise NotConfigured(_("No z.ai API key found"), self.setup_hint)
        url = HOSTS[region] + QUOTA_PATH
        headers = {"Accept-Language": "en-US,en"}
        try:
            try:
                payload = ctx.http.get_json(url, headers={**headers,
                                                          "Authorization": f"Bearer {key}"})
            except HttpError as exc:
                if exc.status != 401:
                    raise
                # The official plugin sends the bare key; accept either form.
                payload = ctx.http.get_json(url, headers={**headers, "Authorization": key})
        except HttpError as exc:
            raise from_http_error(exc, login_hint=self.setup_hint, service=self.name) from None
        windows, plan = parse_quota(payload, ctx.now())
        if not windows:
            raise ProviderError(_("No Coding Plan usage on this account"),
                                _("Check the Usage Dashboard on z.ai."))
        return self.snapshot(ctx, windows, plan=plan, source=origin or None)
