"""Zed: edit-prediction allowance and billing status of your Zed account.

Source: the access token the Zed editor stores in GNOME Keyring when you
sign in (item ``zed-github-account``, attributes ``url`` and ``username`` =
your Zed user ID), sent to ``cloud.zed.dev/client/users/me`` exactly as the
editor does. Development builds keep the token in
``~/.config/zed/development_credentials`` instead, which is read as a
fallback. The keyring item and files are only read; Zed manages its own
session and nothing is ever refreshed or written back.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from quotaglance.i18n import _
from quotaglance.models import ProviderSnapshot, UsageWindow
from quotaglance.net import HttpError
from quotaglance.providers.base import (
    FetchContext,
    NotConfigured,
    Provider,
    ProviderError,
    from_http_error,
)
from quotaglance.util import clamp, parse_time, title_case_plan, to_float

DEFAULT_SERVER = "https://zed.dev"
TRUSTED_SERVERS = ("https://zed.dev", "https://staging.zed.dev")
CLOUD_URL = "https://cloud.zed.dev"
USERS_ME_PATH = "/client/users/me"

PLAN_NAMES = {
    "zed_free": "Free", "zed_pro": "Pro", "zed_pro_trial": "Pro Trial",
    "zed_student": "Student", "zed_business": "Business", "zed_vip": "VIP",
}

LOGIN_HINT = _("Sign in again from the Zed editor (command palette → “client: sign in”).")

_GAP_RE = re.compile(r"(?:\s|//[^\n]*|/\*.*?\*/)*", re.S)  # whitespace and comments


def plan_label(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    key = value.strip().lower()
    return PLAN_NAMES.get(key) or title_case_plan(key.removeprefix("zed_"))


# -- Zed's own settings -------------------------------------------------------------


def zed_config_dir(ctx: FetchContext) -> Path:
    return ctx.config_home / "zed"


def strip_jsonc(text: str) -> str:
    """Turn Zed's JSON-with-comments settings into plain JSON.

    Removes ``//`` and ``/* */`` comments and trailing commas, leaving string
    contents untouched.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            end = i + 1
            while end < n and text[end] != '"':
                end += 2 if text[end] == "\\" else 1
            out.append(text[i:end + 1])
            i = end + 1
        elif text.startswith("//", i):
            newline = text.find("\n", i)
            i = n if newline < 0 else newline
        elif text.startswith("/*", i):
            close = text.find("*/", i + 2)
            i = n if close < 0 else close + 2
        elif ch == ",":
            following = _GAP_RE.match(text, i + 1).end()
            if following >= n or text[following] not in "}]":
                out.append(ch)
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def load_client_settings(ctx: FetchContext) -> dict[str, Any]:
    """``server_url`` / ``credentials_url`` from ``~/.config/zed/settings.json`` (or {})."""
    path = zed_config_dir(ctx) / "settings.json"
    try:
        data = json.loads(strip_jsonc(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: data[key] for key in ("server_url", "credentials_url")
            if isinstance(data.get(key), str) and data[key].strip()}


def resolve_endpoints(settings: Mapping[str, Any]) -> tuple[str, str]:
    """Return (keyring URL, users/me API URL) for Zed's configured server.

    zed.dev (and staging) use the cloud API host; a custom server must be
    HTTPS and keep its credentials under its own URL, so a settings file can
    never forward the zed.dev token to another host.
    """
    server = str(settings.get("server_url") or "").strip() or DEFAULT_SERVER
    credentials = str(settings.get("credentials_url") or "").strip()
    trusted = server.rstrip("/") in TRUSTED_SERVERS
    if not trusted and credentials and credentials != server:
        raise ProviderError(_("Zed's credentials_url doesn't match its custom server_url"),
                            _("QuotaGlance only sends Zed credentials to the server they "
                              "belong to."))
    base = CLOUD_URL if trusted else server.rstrip("/")
    parts = urllib.parse.urlsplit(base)
    if parts.scheme.lower() != "https" or not parts.netloc:
        raise ProviderError(_("Zed's custom server_url must use HTTPS"),
                            _("Check “server_url” in Zed's settings.json."))
    return credentials or server, base + USERS_ME_PATH


# -- credentials -------------------------------------------------------------------


def _dev_token(entry: Any) -> tuple[str, str] | None:
    """A ``[user_id, token_bytes]`` pair from ``development_credentials``."""
    if not isinstance(entry, list) or len(entry) != 2:
        return None
    user_id, raw = str(entry[0]).strip(), entry[1]
    if isinstance(raw, list) and all(isinstance(b, int) and 0 <= b < 256 for b in raw):
        token = bytes(raw).decode("utf-8", errors="replace")
    elif isinstance(raw, str):
        token = raw
    else:
        return None
    token = token.strip()
    return (user_id, token) if user_id.isdigit() and token else None


def find_credentials(ctx: FetchContext, credentials_url: str
                     ) -> tuple[str, str, str] | None:
    """Return (user ID, access token, where it came from)."""
    for attrs, secret in ctx.secrets.search({"url": credentials_url}):
        # Zed's user IDs are numeric; this skips unrelated items that happen
        # to carry the same URL (the search result does not expose labels).
        user_id = str(attrs.get("username") or "").strip()
        token = (secret or "").strip()
        if user_id.isdigit() and token:
            return user_id, token, _("Zed sign-in")
    path = zed_config_dir(ctx) / "development_credentials"
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        found = _dev_token(data.get(credentials_url)) if isinstance(data, dict) else None
        if found:
            return found[0], found[1], _("Zed dev credentials")
    return None


# -- parsing --------------------------------------------------------------------


def _bad_response() -> ProviderError:
    return ProviderError(_("Unexpected response from Zed"),
                         _("Zed may have changed its API; try updating QuotaGlance."))


def parse_users_me(data: Any) -> tuple[list[UsageWindow], str | None, str | None]:
    """Map ``/client/users/me`` onto (windows, plan, account).

    Fails closed: a reply whose shape has drifted raises instead of showing
    made-up numbers.
    """
    plan = data.get("plan") if isinstance(data, dict) else None
    user = data.get("user") if isinstance(data, dict) else None
    usage = plan.get("usage") if isinstance(plan, dict) else None
    predictions = usage.get("edit_predictions") if isinstance(usage, dict) else None
    if not isinstance(user, dict) or not isinstance(predictions, dict):
        raise _bad_response()
    used = to_float(predictions.get("used"))
    raw_limit = predictions.get("limit")
    if isinstance(raw_limit, dict):
        raw_limit = raw_limit.get("limited")
    unlimited = isinstance(raw_limit, str) and raw_limit.strip().lower() == "unlimited"
    limit = None if unlimited else to_float(raw_limit)
    if used is None or used < 0 or (not unlimited and (limit is None or limit < 0)):
        raise _bad_response()

    period = plan.get("subscription_period")
    period = period if isinstance(period, dict) else {}
    started = parse_time(period.get("started_at"))
    ended = parse_time(period.get("ended_at"))  # predictions reset with the billing cycle
    seconds = None
    if started and ended and ended > started:
        seconds = int((ended - started).total_seconds())

    windows: list[UsageWindow] = []
    if unlimited:
        windows.append(UsageWindow(id="edit_predictions", label=_("Edit preds"), resets_at=ended,
                                   window_seconds=seconds, used=used, unit="predictions",
                                   detail=_("Unlimited")))
    elif limit:
        windows.append(UsageWindow(id="edit_predictions", label=_("Edit preds"),
                                   used_percent=clamp(used / limit * 100, 0, 100),
                                   resets_at=ended, window_seconds=seconds,
                                   used=min(used, limit), limit=limit, unit="predictions"))
    if plan.get("has_overdue_invoices") is True:
        windows.append(UsageWindow(id="billing", label=_("Billing"),
                                   detail=_("Invoice overdue")))

    plan_name = next((plan.get(key) for key in ("plan_v3", "plan_v2", "plan")
                      if isinstance(plan.get(key), str) and plan.get(key)), None)
    login = user.get("github_login") if isinstance(user.get("github_login"), str) else None
    name = user.get("name") if isinstance(user.get("name"), str) else None
    return windows, plan_label(plan_name), (login or name or "").strip() or None


class ZedProvider(Provider):
    id = "zed"
    name = "Zed"
    short = "Zd"
    color = "#2563EB"
    category = "editors"
    homepage = "https://zed.dev/account"
    source_summary = _("Zed editor sign-in (GNOME Keyring) → Zed cloud API")
    setup_hint = _("Sign in from the Zed editor (command palette → “client: sign in”); "
                   "QuotaGlance reads that sign-in from GNOME Keyring.")

    def detect(self, ctx: FetchContext) -> bool:
        if not zed_config_dir(ctx).is_dir():
            return False  # don't touch the keyring for people without Zed
        try:
            credentials_url, _api_url = resolve_endpoints(load_client_settings(ctx))
        except ProviderError:
            return True  # Zed is set up; fetch() explains the settings problem
        return find_credentials(ctx, credentials_url) is not None

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        credentials_url, api_url = resolve_endpoints(load_client_settings(ctx))
        found = find_credentials(ctx, credentials_url)
        if not found:
            raise NotConfigured(_("Not signed in to Zed"), self.setup_hint)
        user_id, token, origin = found
        try:
            data = ctx.http.get_json(api_url, headers={"Authorization": f"{user_id} {token}",
                                                      "Content-Type": "application/json"})
        except HttpError as exc:
            raise from_http_error(exc, login_hint=LOGIN_HINT, service=self.name) from None
        windows, plan, account = parse_users_me(data)
        if not windows:
            raise ProviderError(_("Zed reported no edit-prediction allowance for this account"))
        return self.snapshot(ctx, windows, plan=plan, account=account, source=origin)
