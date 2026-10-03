"""OpenCode: OpenCode Go limits (5-hour, weekly, monthly) and Zen spend.

Sources, in order:

* OpenCode Go usage API (``opencode.ai/zen/go/v1/usage``) with the Go key
  OpenCode already saved in ``~/.local/share/opencode/auth.json``.
* OpenCode's local history database (``opencode.db``, opened read-only):
  an estimate of the Go windows when the API can't be reached, and the
  Zen pay-as-you-go spend for today / 7 days / 30 days.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.parse
from datetime import datetime, timedelta, timezone, tzinfo
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
from quotaglance.util import clamp, parse_time, read_json, to_float

GO_USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
GO_LIMITS_USD = {"session": 12.0, "weekly": 30.0, "monthly": 60.0}  # Go plan base pools

ROWS_SQL = """
WITH m AS (
  SELECT id AS message_id,
         CAST(COALESCE(json_extract(data,'$.time.created'), time_created) AS INTEGER) AS created_ms,
         CAST(json_extract(data,'$.cost') AS REAL) AS cost,
         json_type(data,'$.cost') IN ('integer','real') AS has_cost,
         COALESCE(json_extract(data,'$.modelID'),'') AS model_id
  FROM message
  WHERE json_valid(data)
    AND json_extract(data,'$.role') = 'assistant'
    AND json_extract(data,'$.providerID') = :provider
)
SELECT CAST(COALESCE(p.time_created, m.created_ms) AS INTEGER) AS created_ms,
       CAST(json_extract(p.data,'$.cost') AS REAL) AS cost, m.model_id
FROM part p JOIN m ON m.message_id = p.message_id
WHERE json_valid(p.data) AND json_extract(p.data,'$.type') = 'step-finish'
  AND json_type(p.data,'$.cost') IN ('integer','real')
UNION ALL
SELECT created_ms, cost, model_id FROM m
WHERE has_cost AND NOT EXISTS (
  SELECT 1 FROM part p WHERE p.message_id = m.message_id AND json_valid(p.data)
    AND json_extract(p.data,'$.type') = 'step-finish'
    AND json_type(p.data,'$.cost') IN ('integer','real'))
"""


# -- local files ------------------------------------------------------------------


def data_dir(ctx: FetchContext) -> Path:
    return ctx.data_home / "opencode"


def load_auth(ctx: FetchContext) -> dict[str, Any]:
    content = ctx.env.get("OPENCODE_AUTH_CONTENT")
    try:
        data = json.loads(content) if content else read_json(data_dir(ctx) / "auth.json")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _entry_key(auth: dict[str, Any], provider_id: str) -> str | None:
    entry = auth.get(provider_id)
    key = entry.get("key") if isinstance(entry, dict) else None
    return key.strip() if isinstance(key, str) and key.strip() else None


def find_go_key(ctx: FetchContext, auth: dict[str, Any]) -> tuple[str | None, bool]:
    """(key, explicit?) — explicit keys must surface failures instead of falling back."""
    stored = ctx.secret("api_key", (), provider="opencode")
    if stored:
        return stored, True
    if ctx.env.get("OPENCODE_API_KEY", "").strip():
        return ctx.env["OPENCODE_API_KEY"].strip(), True
    return _entry_key(auth, "opencode-go") or _entry_key(auth, "opencode"), False


def db_path(ctx: FetchContext) -> Path | None:
    override = ctx.env.get("OPENCODE_DB")
    if override and override != ":memory:":
        path = Path(override) if Path(override).is_absolute() else data_dir(ctx) / override
        return path if path.is_file() else None
    default = data_dir(ctx) / "opencode.db"
    if default.is_file():
        return default
    channels = sorted(data_dir(ctx).glob("opencode-*.db"), key=lambda p: p.stat().st_mtime,
                      reverse=True)
    return channels[0] if channels else None


def read_rows(path: Path, provider_id: str, since_ms: int) -> list[tuple[int, float, str]]:
    """Per-step costs for one provider, newest first, from the read-only DB."""
    quoted = urllib.parse.quote(str(path))
    last_error: Exception | None = None
    for suffix in ("?mode=ro", "?mode=ro&immutable=1"):
        try:
            conn = sqlite3.connect(f"file:{quoted}{suffix}", uri=True, timeout=0.25)
        except sqlite3.Error as exc:
            last_error = exc
            continue
        try:
            rows = conn.execute(ROWS_SQL, {"provider": provider_id}).fetchall()
        except sqlite3.Error as exc:
            last_error = exc
            continue
        finally:
            conn.close()
        result = []
        for created, cost, model in rows:
            cost = to_float(cost)
            if created and created > 0 and cost is not None and cost >= 0 and created >= since_ms:
                result.append((int(created), cost, model or ""))
        return sorted(result, reverse=True)
    if last_error and "locked" in str(last_error):
        raise ProviderError(_("OpenCode's history is busy; will retry"), transient=True)
    return []


# -- parsing ----------------------------------------------------------------------

_PERCENT_KEYS = ("percent", "usagePercent", "usedPercent", "percentUsed", "usage_percent",
                 "used_percent")
_RESET_KEYS = ("resetsAt", "resetAt", "reset_at", "resets_at")
_RESET_IN_KEYS = ("resetInSec", "resetInSeconds", "reset_in_sec")


def _window_entry(usage: dict[str, Any], *names: str) -> dict[str, Any] | None:
    for name in names:
        value = usage.get(name)
        if isinstance(value, dict):
            return value
    return None


def parse_go_usage(data: Any, now: datetime) -> list[UsageWindow]:
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        raise ProviderError(_("Unexpected response from OpenCode Go"))
    specs = (
        ("session", _("5-hour"), 18000, ("rolling", "rollingUsage", "rolling_usage")),
        ("weekly", _("Weekly"), 604800, ("weekly", "weeklyUsage", "weekly_usage")),
        ("monthly", _("Monthly"), 30 * 86400, ("monthly", "monthlyUsage", "monthly_usage")),
    )
    windows = []
    for wid, label, seconds, names in specs:
        entry = _window_entry(usage, *names)
        if entry is None:
            if wid == "session":
                raise ProviderError(_("Unexpected response from OpenCode Go"))
            continue
        percent = next((to_float(entry.get(k)) for k in _PERCENT_KEYS
                        if to_float(entry.get(k)) is not None), None)
        if entry.get("status") == "rate-limited":
            percent = 100.0
        resets_at = next((parse_time(entry.get(k)) for k in _RESET_KEYS if entry.get(k)), None)
        if resets_at is None:
            reset_in = next((to_float(entry.get(k)) for k in _RESET_IN_KEYS
                             if to_float(entry.get(k)) is not None), None)
            if reset_in is not None and 0 <= reset_in < 10 * 365 * 86400:
                resets_at = now + timedelta(seconds=reset_in)
        detail = None
        if wid == "session" and resets_at is not None:
            if resets_at > now + timedelta(seconds=seconds + 60):
                resets_at = None  # implausible; keep the percent
            elif (percent or 0) == 0 and abs((resets_at - now).total_seconds() - seconds) < 120:
                resets_at, detail = None, _("Not started")
        windows.append(UsageWindow(id=wid, label=label, used_percent=clamp(percent or 0.0, 0, 100),
                                   resets_at=resets_at, window_seconds=seconds, detail=detail))
    return windows


def _monday(now: datetime) -> datetime:
    start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return start - timedelta(days=start.weekday())


def _add_month(moment: datetime) -> datetime:
    year, month = (moment.year + 1, 1) if moment.month == 12 else (moment.year, moment.month + 1)
    day = min(moment.day, [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28,
                           31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return moment.replace(year=year, month=month, day=day)


def estimate_go_windows(rows: list[tuple[int, float, str]], now: datetime) -> list[UsageWindow]:
    """Local, list-price estimate of the Go pools (this device only)."""
    ms = lambda moment: int(moment.timestamp() * 1000)  # noqa: E731
    ascending = sorted(rows)
    session_start = None
    for created, _cost, _model in ascending:
        if session_start is None or created >= session_start + 5 * 3600_000:
            session_start = created
    windows = []
    if session_start is not None and ms(now) < session_start + 5 * 3600_000:
        used = sum(cost for created, cost, _ in rows if created >= session_start)
        reset = datetime.fromtimestamp((session_start + 5 * 3600_000) / 1000, timezone.utc)
    else:
        used, reset = 0.0, None
    windows.append(("session", _("5-hour"), used, reset, 18000))
    week = _monday(now)
    windows.append(("weekly", _("Weekly"),
                    sum(cost for created, cost, _ in rows if created >= ms(week)),
                    week + timedelta(days=7), 604800))
    if ascending:
        anchor = datetime.fromtimestamp(ascending[0][0] / 1000, timezone.utc)
        while _add_month(anchor) <= now:
            anchor = _add_month(anchor)
        windows.append(("monthly", _("Monthly"),
                        sum(cost for created, cost, _ in rows if created >= ms(anchor)),
                        _add_month(anchor), 30 * 86400))
    result = []
    for wid, label, used, reset, seconds in windows:
        limit = GO_LIMITS_USD[wid]
        result.append(UsageWindow(id=wid, label=label,
                                  used_percent=round(clamp(used / limit * 100, 0, 100), 1),
                                  resets_at=reset, window_seconds=seconds, used=round(used, 4),
                                  limit=limit, unit="USD", detail=_("Estimated on this device")))
    return result


def zen_spend_windows(rows: list[tuple[int, float, str]], now: datetime,
                      tz: tzinfo | None = None) -> list[UsageWindow]:
    local_now = now.astimezone(tz) if tz else now.astimezone()
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    spans = (("today", _("Today"), midnight),
             ("spend_7d", _("7 days"), now - timedelta(days=7)),
             ("spend_30d", _("30 days"), now - timedelta(days=30)))
    windows = []
    for wid, label, since in spans:
        since_ms = int(since.timestamp() * 1000)
        selected = [cost for created, cost, _ in rows if created >= since_ms]
        total = round(sum(selected), 4)
        windows.append(UsageWindow(
            id=wid, label=label, used=total, unit="USD",
            detail=_("${total:.2f} · {n} requests").format(total=total, n=len(selected))))
    return windows


class OpenCodeProvider(Provider):
    id = "opencode"
    name = "OpenCode"
    short = "OC"
    color = "#F59E0B"
    category = "agents"
    homepage = "https://opencode.ai/auth"
    source_summary = _("OpenCode Go usage API + local OpenCode history")
    setup_hint = _("Sign in to OpenCode Go or Zen with `opencode auth login`; QuotaGlance reads "
                   "OpenCode's saved key and history.")
    settings = (api_key_setting(("OPENCODE_API_KEY",), title=_("OpenCode Go API key")),)

    def detect(self, ctx: FetchContext) -> bool:
        auth = load_auth(ctx)
        return bool(_entry_key(auth, "opencode-go") or _entry_key(auth, "opencode")
                    or db_path(ctx))

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        now = ctx.now()
        auth = load_auth(ctx)
        key, explicit = find_go_key(ctx, auth)
        database = db_path(ctx)
        since_ms = int((now - timedelta(days=62)).timestamp() * 1000)
        windows: list[UsageWindow] = []
        plan = None
        source = None
        note = None

        if key:
            try:
                data = ctx.http.get_json(GO_USAGE_URL, headers={"Authorization": f"Bearer {key}"})
                windows = parse_go_usage(data, now)
                plan, source = "Go", _("OpenCode Go API")
            except HttpError as exc:
                if exc.status == 403:
                    note = _("No OpenCode Go subscription on this account")
                elif exc.status == 401 and explicit:
                    raise AuthError(_("OpenCode Go rejected the API key"),
                                    self.setup_hint) from None
                elif explicit or database is None:
                    raise from_http_error(exc, login_hint=self.setup_hint,
                                          service=self.name) from None
                else:
                    note = from_http_error(exc, service=self.name).message
            except NetworkError:
                if database is None:
                    raise

        if database is not None:
            if not windows:
                go_rows = read_rows(database, "opencode-go", since_ms)
                if go_rows:
                    windows = estimate_go_windows(go_rows, now)
                    plan, source = "Go", _("Local history (estimate)")
            zen_rows = read_rows(database, "opencode", since_ms)
            if zen_rows or _entry_key(auth, "opencode"):
                windows += zen_spend_windows(zen_rows, now)
                plan = plan or _("Zen")
                source = source or _("Local history")
        if not windows:
            if note:
                raise NotConfigured(note, self.setup_hint)
            raise NotConfigured(_("No OpenCode Go or Zen usage found"), self.setup_hint)
        return self.snapshot(ctx, windows, plan=plan, source=source, message=note)

