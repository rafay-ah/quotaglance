"""GitHub Copilot: premium requests / AI credits, chat and completions quotas.

Source: ``api.github.com/copilot_internal/user`` (what the editor plugins
call), authenticated with a GitHub OAuth token that is already on disk:
the Copilot editor plugins' ``~/.config/github-copilot/apps.json``, the
GitHub CLI (``gh``), or a token you paste into Preferences (kept in GNOME
Keyring). Tokens are only read, never refreshed or rewritten.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from quotaglance.i18n import _
from quotaglance.models import ProviderSnapshot, UsageWindow
from quotaglance.net import HttpError
from quotaglance.providers.base import (
    FetchContext,
    NotConfigured,
    Provider,
    ProviderError,
    SettingSpec,
    from_http_error,
)
from quotaglance.util import clamp, parse_time, read_json, title_case_plan, to_float

USER_URL = "https://api.github.com/copilot_internal/user"
HEADERS = {
    "Editor-Version": "vscode/1.96.2",
    "Editor-Plugin-Version": "copilot-chat/0.26.7",
    "User-Agent": "GitHubCopilotChat/0.26.7",
    "X-Github-Api-Version": "2025-04-01",
}
LOGIN_HINT = _("Sign in to Copilot in your editor, or run `gh auth login`.")

SNAPSHOT_LABELS = {
    "premium_interactions": _("Premium"),
    "chat": _("Chat"),
    "completions": _("Completions"),
}


# -- token discovery --------------------------------------------------------------


def _token_from_hosts_file(path) -> str | None:
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        if key == "github.com" or key.startswith("github.com:"):
            token = value.get("oauth_token")
            if token:
                return str(token).strip()
    return None


def token_from_gh_hosts(text: str) -> str | None:
    """Pull ``oauth_token`` from the ``github.com:`` block of gh's hosts.yml."""
    in_block = False
    block_indent = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            in_block = line.strip().rstrip(":").strip("'\"") == "github.com"
            block_indent = None
            continue
        if not in_block:
            continue
        if block_indent is None:
            block_indent = indent
        if indent != block_indent:
            continue  # nested maps (e.g. "users:") are not the host-level token
        match = re.match(r"\s*oauth_token:\s*['\"]?([^'\"\s]+)", line)
        if match:
            return match.group(1)
    return None


def find_token(ctx: FetchContext) -> tuple[str | None, str | None]:
    """Return (token, where it came from)."""
    stored = ctx.secret("token", (), provider="copilot")
    if stored:
        return stored, _("Keyring")
    base = ctx.config_home / "github-copilot"
    for name in ("apps.json", "hosts.json"):
        token = _token_from_hosts_file(base / name)
        if token:
            return token, _("Copilot plugin sign-in")
    hosts = ctx.config_home / "gh" / "hosts.yml"
    if hosts.is_file():
        try:
            token = token_from_gh_hosts(hosts.read_text(encoding="utf-8"))
        except OSError:
            token = None
        if token:
            return token, _("GitHub CLI")
    gh = ctx.which("gh")
    if gh:
        try:
            result = ctx.run([gh, "auth", "token", "--hostname", "github.com"], timeout=8)
        except ProviderError:
            result = None
        token = (result.stdout or "").strip() if result else ""
        if result and result.returncode == 0 and token and " " not in token:
            return token, _("GitHub CLI")
    for name in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        value = (ctx.env.get(name) or "").strip()
        if value:
            return value, f"${name}"
    return None, None


# -- parsing ----------------------------------------------------------------------


def _reset_time(data: dict[str, Any]) -> datetime | None:
    for key in ("quota_reset_date_utc", "quota_reset_date", "limited_user_reset_date"):
        value = data.get(key)
        if value:
            parsed = parse_time(value)
            if parsed:
                return parsed
    return None


def _is_unlimited(snapshot: dict[str, Any]) -> bool:
    return bool(snapshot.get("unlimited")) or to_float(snapshot.get("entitlement")) == -1 \
        or to_float(snapshot.get("remaining")) == -1


def parse_copilot_user(data: dict[str, Any]) -> tuple[list[UsageWindow], str | None]:
    resets_at = _reset_time(data)
    snapshots = data.get("quota_snapshots")
    if not isinstance(snapshots, dict) or not snapshots:
        snapshots = _legacy_snapshots(data)

    windows: list[UsageWindow] = []
    order = ["premium_interactions", "chat", "completions"]
    keys = order + sorted(k for k in snapshots if k not in order)
    for key in keys:
        snap = snapshots.get(key)
        if not isinstance(snap, dict) or _is_unlimited(snap):
            continue
        entitlement = to_float(snap.get("entitlement"))
        remaining = to_float(snap.get("remaining"))
        if not entitlement and not remaining:
            continue  # org-managed placeholder: carries no real percentage
        percent_remaining = to_float(snap.get("percent_remaining"))
        if percent_remaining is None and entitlement:
            percent_remaining = (remaining or 0) / entitlement * 100
        if percent_remaining is None:
            continue
        used = None if entitlement is None or remaining is None else max(0.0, entitlement -
                                                                         remaining)
        label = SNAPSHOT_LABELS.get(key, key.replace("_", " ").title())
        windows.append(UsageWindow(
            id="premium" if key == "premium_interactions" else key,
            label=label,
            used_percent=clamp(100.0 - percent_remaining, 0, 100),
            resets_at=resets_at,
            used=used,
            limit=entitlement,
        ))

    credits = None
    for snap in snapshots.values():
        if isinstance(snap, dict) and to_float(snap.get("credits_used")):
            credits = (credits or 0.0) + (to_float(snap.get("credits_used")) or 0.0)
    if credits and (data.get("token_based_billing") or not windows):
        windows.append(UsageWindow(id="credits", label=_("Credits used"), resets_at=resets_at,
                                   used=credits, unit="credits",
                                   detail=_("{n:g} credits this month").format(n=credits)))

    plan = title_case_plan(data.get("copilot_plan"))
    if plan == "Individual":
        plan = "Pro"
    if plan == "Pro Plus":
        plan = "Pro+"
    return windows, plan


def _legacy_snapshots(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    monthly = data.get("monthly_quotas") or {}
    limited = data.get("limited_user_quotas") or {}
    result = {}
    for key, target in (("completions", "premium_interactions"), ("chat", "chat")):
        entitlement = to_float(monthly.get(key))
        remaining = to_float(limited.get(key))
        if entitlement:
            result[target] = {"entitlement": entitlement, "remaining": remaining or 0.0}
    return result


class CopilotProvider(Provider):
    id = "copilot"
    name = "GitHub Copilot"
    short = "Co"
    color = "#8957E5"
    category = "editors"
    homepage = "https://github.com/settings/copilot"
    source_summary = _("Editor or GitHub CLI sign-in → GitHub Copilot API")
    setup_hint = _("Sign in to Copilot in VS Code, JetBrains or Neovim, or run `gh auth login`. "
                   "You can also paste a GitHub OAuth token below.")
    settings = (
        SettingSpec("token", "secret", _("GitHub token"),
                    _("Optional: an OAuth token (gho_…) if no editor or gh sign-in is found")),
    )

    def detect(self, ctx: FetchContext) -> bool:
        base = ctx.config_home / "github-copilot"
        return (base / "apps.json").is_file() or (base / "hosts.json").is_file()

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        token, origin = find_token(ctx)
        if not token:
            raise NotConfigured(_("No GitHub sign-in found"), LOGIN_HINT)
        try:
            data = ctx.http.get_json(USER_URL, headers={**HEADERS,
                                                       "Authorization": f"token {token}"})
        except HttpError as exc:
            if exc.status == 404:
                raise ProviderError(_("This GitHub account has no Copilot subscription")) from None
            raise from_http_error(exc, login_hint=LOGIN_HINT, service="GitHub") from None
        if not isinstance(data, dict):
            raise ProviderError(_("Unexpected response from GitHub"))
        windows, plan = parse_copilot_user(data)
        if not windows:
            if data.get("token_based_billing") or plan in ("Business", "Enterprise"):
                windows = [UsageWindow(id="seat", label=_("Seat"),
                                       detail=_("Managed by your organization"))]
            else:
                windows = [UsageWindow(id="unlimited", label=_("Usage"), detail=_("Unlimited"))]
        return self.snapshot(ctx, windows, plan=plan, account=data.get("login"),
                             source=origin)

