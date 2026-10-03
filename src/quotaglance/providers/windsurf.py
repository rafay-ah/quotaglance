"""Windsurf: daily and weekly quota (or prompt / flow-action credits on older plans).

Source: the API key the Windsurf app keeps in its local SQLite state DB
(``~/.config/Windsurf/User/globalStorage/state.vscdb``, key
``windsurfAuthStatus``), sent to the ``GetUserStatus`` call the editor makes
itself. When that is not possible, QuotaGlance shows the plan snapshot
Windsurf caches in the same DB (``windsurf.settings.cachedPlanInfo``), which
only updates while Windsurf runs. The database is opened read-only and the key
is never refreshed or stored anywhere else.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.parse
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from quotaglance.i18n import _
from quotaglance.models import ProviderSnapshot, Status, UsageWindow
from quotaglance.net import HttpError, NetworkError
from quotaglance.providers.base import (
    FetchContext,
    NotConfigured,
    Provider,
    ProviderError,
    from_http_error,
)
from quotaglance.util import clamp, dig, parse_time, to_float

STATUS_PATH = "/exa.seat_management_pb.SeatManagementService/GetUserStatus"
STATUS_URLS = (
    "https://server.self-serve.windsurf.com" + STATUS_PATH,
    "https://server.codeium.com" + STATUS_PATH,
)
AUTH_KEY = "windsurfAuthStatus"
PLAN_KEY = "windsurf.settings.cachedPlanInfo"
APP_DIRS = ("Windsurf", "Windsurf - Next")

PLAN_NAMES = {
    "free": "Free", "pro": "Pro", "team": "Teams", "teams": "Teams",
    "enterprise": "Enterprise", "ultimate": "Ultimate",
}

LOGIN_HINT = _("Open Windsurf and sign in; QuotaGlance reads its session read-only.")


def plan_label(name: Any) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return None
    return PLAN_NAMES.get(name.strip().lower(), name.strip())


# -- local state DB ---------------------------------------------------------------


def state_db_path(ctx: FetchContext) -> Path | None:
    for folder in APP_DIRS:
        path = ctx.config_home / folder / "User" / "globalStorage" / "state.vscdb"
        if path.is_file():
            return path
    return None


def decode_json(value: Any) -> Any:
    """Decode an ItemTable value as JSON.

    Values are TEXT or BLOB, and BLOBs may be UTF-8 or UTF-16LE. Only a
    decoding that actually parses as JSON is accepted, so UTF-16 bytes are
    never misread as (NUL-riddled) UTF-8.
    """
    if value is None:
        return None
    if isinstance(value, str):
        texts = [value]
    else:
        raw = bytes(value)
        texts = []
        for encoding in ("utf-8", "utf-16-le"):
            try:
                texts.append(raw.decode(encoding))
            except UnicodeDecodeError:
                continue
    for text in texts:
        text = text.lstrip("﻿").strip().strip("\x00")
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if isinstance(data, str) and data.lstrip().startswith("{"):
            try:
                data = json.loads(data)  # stored as a JSON-encoded string
            except ValueError:
                continue
        return data
    return None


def read_state_values(db_path: Path, keys: list[str]) -> dict[str, Any]:
    """Read and JSON-decode keys from Windsurf's ItemTable without writing to the DB.

    While Windsurf runs (a ``-wal`` file exists) a read-only connection sees
    its latest commits. When it is closed, the file is opened as immutable
    first: a plain read-only open would create ``-wal``/``-shm`` sidecar
    files next to Windsurf's database.
    """
    uri_path = urllib.parse.quote(str(db_path))
    suffixes = ["?mode=ro", "?mode=ro&immutable=1"]
    if not db_path.with_name(db_path.name + "-wal").exists():
        suffixes.reverse()
    for suffix in suffixes:
        try:
            conn = sqlite3.connect(f"file:{uri_path}{suffix}", uri=True, timeout=0.25)
        except sqlite3.Error:
            continue
        try:
            placeholders = ",".join("?" for _ in keys)
            rows = conn.execute(
                f"SELECT key, value FROM ItemTable WHERE key IN ({placeholders})", keys).fetchall()
        except sqlite3.Error:
            conn.close()
            continue
        conn.close()
        values: dict[str, Any] = {}
        for key, value in rows:
            decoded = decode_json(value)
            if decoded is not None:
                values[key] = decoded
        return values
    return {}


def find_field(data: Any, names: tuple[str, ...], max_depth: int = 4) -> str | None:
    """First non-empty string under any of ``names``, searched breadth-first."""
    level = [data]
    for _depth in range(max_depth + 1):
        children: list[Any] = []
        for node in level:
            if isinstance(node, Mapping):
                for name in names:
                    value = node.get(name)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
                children.extend(node.values())
            elif isinstance(node, list):
                children.extend(node)
        level = children
    return None


def db_modified(db_path: Path) -> datetime | None:
    """When Windsurf last wrote its state DB (the cache is at most this fresh)."""
    stamps = []
    for path in (db_path, db_path.with_name(db_path.name + "-wal")):
        try:
            stamps.append(path.stat().st_mtime)
        except OSError:
            continue
    return datetime.fromtimestamp(max(stamps), timezone.utc) if stamps else None


# -- parsing ----------------------------------------------------------------------


def _pick(sources: list[Any], *names: str) -> Any:
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for name in names:
            value = source.get(name)
            if value is not None and value != "":
                return value
    return None


def quota_windows(daily: Any, weekly: Any, daily_reset: Any = None,
                  weekly_reset: Any = None) -> list[UsageWindow]:
    """Daily/weekly meters from *remaining* percentages (0..100)."""
    windows = []
    for wid, label, remaining, reset, seconds in (
            ("daily", _("Daily"), daily, daily_reset, 86400),
            ("weekly", _("Weekly"), weekly, weekly_reset, 7 * 86400)):
        value = to_float(remaining)
        if value is None:
            continue
        windows.append(UsageWindow(id=wid, label=label, used_percent=clamp(100.0 - value, 0, 100),
                                   resets_at=parse_time(reset), window_seconds=seconds))
    return windows


def _credit_window(wid: str, label: str, total: Any, used: Any,
                   remaining: Any) -> UsageWindow | None:
    """Older plans count credits in hundredths (a Pro plan's 500 prompts = 50000)."""
    total_value = to_float(total)
    if not total_value or total_value <= 0:
        return None
    used_value = to_float(used)
    if used_value is None:
        left = to_float(remaining)
        if left is None:
            return None
        used_value = total_value - left
    used_value = clamp(used_value, 0, total_value)
    return UsageWindow(id=wid, label=label, used_percent=used_value / total_value * 100,
                       used=round(used_value / 100, 2), limit=round(total_value / 100, 2),
                       unit="credits")


def parse_cached_plan_info(data: Any) -> tuple[list[UsageWindow], str | None]:
    """Map ``windsurf.settings.cachedPlanInfo`` onto windows.

    The daily/weekly quota wins; caches without ``quotaUsage`` fall back to
    the monthly prompt and flow-action counters (plus add-on flex credits).
    """
    if not isinstance(data, Mapping):
        return [], None
    quota = data.get("quotaUsage") or {}
    windows = quota_windows(quota.get("dailyRemainingPercent"),
                            quota.get("weeklyRemainingPercent"),
                            quota.get("dailyResetAtUnix"), quota.get("weeklyResetAtUnix"))
    if not windows:
        usage = data.get("usage") or {}
        for wid, label, total, used, left in (
                ("prompts", _("Prompts"), "messages", "usedMessages", "remainingMessages"),
                ("flow_actions", _("Flow actions"), "flowActions", "usedFlowActions",
                 "remainingFlowActions"),
                ("flex", _("Flex"), "flexCredits", "usedFlexCredits", "remainingFlexCredits")):
            window = _credit_window(wid, label, usage.get(total), usage.get(used), usage.get(left))
            if window:
                windows.append(window)
    return windows, plan_label(data.get("planName"))


def parse_user_status(data: Any) -> tuple[list[UsageWindow], str | None, str | None]:
    """Map a ``GetUserStatus`` reply onto (windows, plan, email).

    Fields usually sit under ``userStatus.planStatus``, but flat, ``quota``-
    nested and snake_case variants exist too; percentages may be strings.
    """
    if not isinstance(data, Mapping):
        return [], None, None
    root = _pick([data], "userStatus", "user_status")
    root = root if isinstance(root, Mapping) else data
    status = _pick([root], "planStatus", "plan_status")
    status = status if isinstance(status, Mapping) else {}
    sources = [status, status.get("quota"), root, root.get("quota"), data, data.get("quota")]
    windows = quota_windows(
        _pick(sources, "dailyQuotaRemainingPercent", "daily_quota_remaining_percent"),
        _pick(sources, "weeklyQuotaRemainingPercent", "weekly_quota_remaining_percent"),
        _pick(sources, "dailyQuotaResetAtUnix", "daily_quota_reset_at_unix"),
        _pick(sources, "weeklyQuotaResetAtUnix", "weekly_quota_reset_at_unix"))
    micros = to_float(_pick(sources, "overageBalanceMicros", "overage_balance_micros"))
    if windows and micros and micros > 0:
        dollars = micros / 1_000_000
        windows.append(UsageWindow(id="overage", label=_("Overage"),
                                   detail=_("${amount:,.2f} left").format(amount=dollars)))
    plan = plan_label(dig(status, "planInfo", "planName") or dig(status, "plan_info", "plan_name")
                      or _pick([root, data], "planName", "plan_name"))
    email = _pick([root, data], "email")
    return windows, plan, str(email) if email else None


# -- network ----------------------------------------------------------------------


def fetch_user_status(ctx: FetchContext, api_key: str) -> dict[str, Any]:
    """POST GetUserStatus, trying the Codeium host only on 404/5xx/network errors."""
    headers = {"Authorization": f"Bearer {api_key}", "Connect-Protocol-Version": "1"}
    body = {"metadata": {"apiKey": api_key, "ideName": "windsurf"}}
    failure: Exception | None = None
    for url in STATUS_URLS:
        try:
            data = ctx.http.post_json(url, body, headers=headers)
        except HttpError as exc:
            if exc.status == 404 or exc.status >= 500:
                failure = exc
                continue
            raise from_http_error(exc, login_hint=LOGIN_HINT, service="Windsurf") from None
        except NetworkError as exc:
            failure = exc
            continue
        if not isinstance(data, dict):
            raise ProviderError(_("Unexpected response from Windsurf"))
        return data
    if isinstance(failure, HttpError):
        raise from_http_error(failure, login_hint=LOGIN_HINT, service="Windsurf")
    raise ProviderError(str(failure), transient=True)


class WindsurfProvider(Provider):
    id = "windsurf"
    name = "Windsurf"
    short = "Ws"
    color = "#0D9488"
    category = "editors"
    homepage = "https://windsurf.com/subscription/usage"
    source_summary = _("Windsurf app sign-in → Windsurf usage API (or the app's cache)")
    setup_hint = _("Install Windsurf and sign in. QuotaGlance reuses that session (read-only).")

    def detect(self, ctx: FetchContext) -> bool:
        return state_db_path(ctx) is not None

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        db = state_db_path(ctx)
        if db is None:
            raise NotConfigured(_("Windsurf isn't installed or hasn't been opened yet"),
                                self.setup_hint)
        values = read_state_values(db, [AUTH_KEY, PLAN_KEY])
        auth = values.get(AUTH_KEY)
        api_key = find_field(auth, ("apiKey", "api_key"))
        cached = values.get(PLAN_KEY)
        if not api_key and cached is None:
            raise NotConfigured(_("Not signed in to Windsurf"), LOGIN_HINT)
        account = find_field(auth, ("email",))
        cache_windows, cache_plan = parse_cached_plan_info(cached)
        fallback_plan = cache_plan or plan_label(find_field(auth, ("planName",)))

        def from_cache(error: ProviderError | None = None) -> ProviderSnapshot:
            snap = self.snapshot(ctx, cache_windows, plan=fallback_plan, account=account,
                                 source=_("Windsurf app cache"), observed_at=db_modified(db),
                                 status=Status.STALE if error else Status.OK,
                                 message=_("{error}; showing values cached by Windsurf").format(
                                     error=error.message) if error else None)
            return replace(snap, hint=error.hint) if error else snap

        if not api_key:
            if cache_windows:
                return from_cache()
            raise ProviderError(_("Windsurf hasn't cached any usage yet"),
                                _("Open Windsurf once so it can load your plan."))
        try:
            data = fetch_user_status(ctx, api_key)
        except ProviderError as exc:
            # Blips are left to the engine (it keeps the last live numbers);
            # a rejected key or a changed API falls back to the app's cache.
            if exc.transient or not cache_windows:
                raise
            return from_cache(exc)
        windows, plan, email = parse_user_status(data)
        if windows:
            return self.snapshot(ctx, windows, plan=plan or fallback_plan,
                                 account=email or account, source=_("Windsurf API"))
        if cache_windows:
            return from_cache()
        raise ProviderError(_("Windsurf reported no quota for this account"))
