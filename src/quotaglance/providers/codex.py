"""Codex (OpenAI): 5-hour and weekly limits, model-specific limits, credits.

Sources, merged per window (the newest reading wins):

* **Session logs** (``~/.codex/sessions/**/rollout-*.jsonl``): every Codex
  turn records the current rate limits. Read locally, no network.
* **ChatGPT usage endpoint** (what ``/status`` in Codex shows), called with
  the token in ``~/.codex/auth.json``, but only while that token is fresh.
* **``codex app-server``** (Codex's official JSON-RPC interface) when the
  token is stale or kept in the keyring. Codex refreshes and saves its own
  token there, safely. QuotaGlance never refreshes Codex tokens itself:
  they are single-use, so doing so would sign you out of Codex.
"""

from __future__ import annotations

import json
import math
import os
import re
import selectors
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from quotaglance import __version__
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
    from_http_error,
)
from quotaglance.util import (
    clamp,
    decode_jwt_claims,
    dig,
    iter_lines_reverse,
    parse_time,
    read_json,
    to_float,
)

DEFAULT_BASE = "https://chatgpt.com/backend-api"
API_MIN_INTERVAL = 300.0
STALE_LIVE_MAX = 1800.0  # reuse a live reading this long when ChatGPT is unreachable
LOGIN_HINT = _("Run `codex login` (or open Codex once so it refreshes its login).")
AUTH_CLAIMS = "https://api.openai.com/auth"

PLAN_NAMES = {
    "free": "Free", "go": "Go", "plus": "Plus", "prolite": "Pro", "pro": "Pro (More)",
    "promax": "Pro (Max)", "team": "Team", "business": "Business", "enterprise": "Enterprise",
    "edu": "Edu", "education": "Edu", "edu_plus": "Edu Plus", "edu_pro": "Edu Pro",
    "hc": "Enterprise",
}
KINDS = (("session", 300), ("daily", 1440), ("weekly", 10080), ("monthly", 43200))

_lock = threading.Lock()
_api_cache: dict[str, dict[str, Any]] = {}
_rollout_cache: dict[str, tuple[float, int, Any]] = {}


def codex_home(ctx: FetchContext) -> Path:
    custom = (ctx.settings.get("home") or ctx.env.get("CODEX_HOME") or "").strip()
    return ctx.path(custom) if custom else ctx.path("~/.codex")


def plan_label(raw: Any) -> str | None:
    if not isinstance(raw, str) or not raw.strip() or raw == "unknown":
        return None
    key = raw.strip().lower()
    return PLAN_NAMES.get(key, key.replace("_", " ").title())


# -- window classification -------------------------------------------------------------


def _minutes(window: dict[str, Any]) -> int | None:
    seconds = to_float(window.get("limit_window_seconds"))
    if seconds and seconds > 0:
        return math.ceil(seconds / 60)
    minutes = to_float(window.get("window_minutes", window.get("windowDurationMins")))
    return int(minutes) if minutes and minutes > 0 else None


def kind_of(minutes: int | None) -> str:
    if minutes is None:
        return "unknown"
    for kind, expected in KINDS:
        if expected * 0.95 <= minutes <= expected * 1.05:
            return kind
    return "unknown"


def _reset(window: dict[str, Any], observed: datetime) -> datetime | None:
    for key in ("reset_at", "resets_at", "resetsAt"):
        value = window.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return parse_time(value)
        if isinstance(value, str) and value:
            parsed = parse_time(value)
            if parsed:
                return parsed
    for key in ("reset_after_seconds", "resets_in_seconds"):
        value = to_float(window.get(key))
        if value is not None:
            return observed + timedelta(seconds=value)
    return None


def _used(window: dict[str, Any]) -> float | None:
    return to_float(window.get("used_percent", window.get("usedPercent")))


def normalize(primary: dict | None, secondary: dict | None
              ) -> tuple[dict | None, dict | None]:
    """(session-ish, weekly-ish) by duration, never by position."""
    kind_p = kind_of(_minutes(primary)) if primary else None
    kind_s = kind_of(_minutes(secondary)) if secondary else None
    if primary and secondary and kind_p == "weekly" and kind_s in ("session", "unknown"):
        return secondary, primary
    if primary and not secondary and kind_p == "weekly":
        return None, primary
    if secondary and not primary and kind_s != "weekly":
        return secondary, None
    return primary, secondary


def _span(minutes: int | None) -> str | None:
    """A compact length for windows Codex has no name for: "9h", "3d"."""
    if not minutes:
        return None
    return f"{round(minutes / 60)}h" if minutes < 2880 else f"{round(minutes / 1440)}d"


def _label(window: dict[str, Any], slot: str) -> tuple[str, str]:
    minutes = _minutes(window)
    kind = kind_of(minutes)
    labels = {"session": _("5-hour"), "daily": _("Daily"), "weekly": _("Weekly"),
              "monthly": _("Monthly")}
    if kind in labels:
        return kind, labels[kind]
    span = _span(minutes)
    if span:
        return slot, _("{span} window").format(span=span)
    return slot, _("Usage") if slot == "session" else _("Secondary")


def bucket_windows(rate_limit: dict[str, Any], observed: datetime, prefix: str = "",
                   name: str | None = None) -> list[UsageWindow]:
    primary = rate_limit.get("primary_window", rate_limit.get("primary"))
    secondary = rate_limit.get("secondary_window", rate_limit.get("secondary"))
    primary = primary if isinstance(primary, dict) else None
    secondary = secondary if isinstance(secondary, dict) else None
    windows = []
    for slot, window in zip(("session", "weekly"), normalize(primary, secondary), strict=True):
        if window is None or _used(window) is None:
            continue
        kind, label = _label(window, slot)
        minutes = _minutes(window)
        if prefix and name:
            short = {"session": "5h", "weekly": _("week"), "daily": _("day"),
                     "monthly": _("month")}.get(kind) or _span(minutes)
            label = f"{name} {short}" if short else name
        windows.append(UsageWindow(
            id=f"{prefix}{kind if kind != 'unknown' else slot}", label=label,
            used_percent=clamp(_used(window) or 0.0, 0, 100), resets_at=_reset(window, observed),
            window_seconds=minutes * 60 if minutes else None))
    return windows


def _extra_name(limit_name: Any, feature: Any, limit_id: Any) -> tuple[str, str]:
    """(id slug, short title) for a model-specific limit such as Codex-Spark."""
    if "spark" in f"{limit_name} {feature} {limit_id}".lower():
        return "spark", "Spark"
    source = feature or limit_name or limit_id or "extra"
    slug = re.sub(r"[^a-z0-9]+", "-", str(source).lower()).strip("-") or "extra"
    # "GPT-6-Codex-Max" → "Max": the last plain word is the distinctive part.
    words = [w for w in re.split(r"[-_\s]+", str(limit_name or feature or limit_id or ""))
             if w.isalpha() and w.lower() not in ("gpt", "codex")]
    title = words[-1].capitalize() if words else str(limit_name or slug)
    return slug, title[:7]


def credit_windows(data: dict[str, Any], observed: datetime) -> list[UsageWindow]:
    windows = []
    individual = None
    for candidate in (data.get("individual_limit"), dig(data, "rate_limit", "individual_limit"),
                      dig(data, "spend_control", "individual_limit"), data.get("individualLimit")):
        if isinstance(candidate, dict):
            individual = candidate
            break
    if individual:
        limit = to_float(individual.get("limit"))
        used = to_float(individual.get("used"))
        remaining = to_float(individual.get("remaining_percent",
                                            individual.get("remainingPercent")))
        percent = 100 - remaining if remaining is not None else (
            used / limit * 100 if used is not None and limit else None)
        windows.append(UsageWindow(
            id="monthly_credits", label=_("Monthly"),
            used_percent=None if percent is None else clamp(percent, 0, 100),
            resets_at=_reset(individual, observed), used=used, limit=limit, unit="credits"))
    credits = data.get("credits")
    if isinstance(credits, dict):
        balance = to_float(credits.get("balance"))
        if credits.get("unlimited"):
            windows.append(UsageWindow(id="credits", label=_("Credits"), detail=_("Unlimited")))
        elif credits.get("has_credits", credits.get("hasCredits")) or (balance or 0) > 0:
            windows.append(UsageWindow(
                id="credits", label=_("Credits"), used=balance, unit="credits",
                detail=_("{n:g} credits left").format(n=balance) if balance is not None
                else _("Workspace credits")))
    return windows


def parse_usage(data: dict[str, Any], observed: datetime) -> tuple[list[UsageWindow], str | None]:
    """``wham/usage`` → windows (+ plan)."""
    if not isinstance(data, dict):
        raise ProviderError(_("Unexpected response from ChatGPT"))
    windows = []
    if isinstance(data.get("rate_limit"), dict):
        windows += bucket_windows(data["rate_limit"], observed)
    for extra in data.get("additional_rate_limits") or []:
        if not isinstance(extra, dict) or not isinstance(extra.get("rate_limit"), dict):
            continue
        slug, name = _extra_name(extra.get("limit_name"), extra.get("metered_feature"), None)
        try:
            windows += bucket_windows(extra["rate_limit"], observed, prefix=f"{slug}_", name=name)
        except (TypeError, ValueError):
            continue
    review = data.get("code_review_rate_limit")
    if isinstance(review, dict):
        for window in bucket_windows(review, observed, prefix="review_", name=_("Review")):
            window.label = _("Code review")
            windows.append(window)
    windows += credit_windows(data, observed)
    return windows, plan_label(data.get("plan_type"))


def parse_app_server(result: dict[str, Any], observed: datetime
                     ) -> tuple[list[UsageWindow], str | None]:
    """``account/rateLimits/read`` result → windows (+ plan)."""
    main = result.get("rateLimits") if isinstance(result, dict) else None
    if not isinstance(main, dict):
        raise ProviderError(_("Unexpected answer from codex app-server"))
    windows = bucket_windows(main, observed)
    for limit_id, bucket in (result.get("rateLimitsByLimitId") or {}).items():
        if limit_id == "codex" or not isinstance(bucket, dict):
            continue
        slug, name = _extra_name(bucket.get("limitName"), None, limit_id)
        windows += bucket_windows(bucket, observed, prefix=f"{slug}_", name=name)
    windows += credit_windows(main, observed)
    return windows, plan_label(main.get("planType"))


REACHED_NOTES = {
    "rate_limit_reached": _("Usage limit reached"),
    "workspace_owner_credits_depleted": _("Workspace credits are used up"),
    "workspace_member_credits_depleted": _("Workspace credits are used up"),
    "workspace_owner_usage_limit_reached": _("Workspace usage limit reached"),
    "workspace_member_usage_limit_reached": _("Workspace usage limit reached"),
}


def reached_note(value: Any) -> str | None:
    """``rate_limit_reached_type``: an object ``{type}`` in wham, a string elsewhere."""
    kind = value.get("type") if isinstance(value, dict) else value
    if not isinstance(kind, str) or not kind:
        return None
    return REACHED_NOTES.get(kind, _("Usage limit reached"))


# -- rollout logs ---------------------------------------------------------------------------


def parse_rollout_line(raw: str) -> tuple[datetime, dict[str, Any]] | None:
    if '"token_count"' not in raw or '"rate_limits"' not in raw:
        return None
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    payload = obj.get("payload") if isinstance(obj, dict) else None
    if obj.get("type") != "event_msg" or not isinstance(payload, dict) or \
            payload.get("type") != "token_count":
        return None
    limits = payload.get("rate_limits")
    timestamp = parse_time(obj.get("timestamp"))
    if not isinstance(limits, dict) or timestamp is None:
        return None
    if "primary_used_percent" in limits:  # Codex 0.40: flat fields, no reset times
        limits = {"primary": {"used_percent": limits.get("primary_used_percent"),
                              "window_minutes": limits.get("primary_window_minutes")},
                  "secondary": {"used_percent": limits.get("secondary_used_percent",
                                                          limits.get("weekly_used_percent")),
                                "window_minutes": limits.get("secondary_window_minutes",
                                                             limits.get("weekly_window_minutes"))}}
    if not any(isinstance(limits.get(k), dict) for k in ("primary", "secondary")):
        return None  # metadata-only bucket
    return timestamp, limits


def rollout_files(home: Path, now: datetime, limit: int = 5) -> list[Path]:
    cutoff = now.timestamp() - 7 * 86400
    found = []
    for sub in ("sessions", "archived_sessions"):
        root = home / sub
        if not root.is_dir():
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if name.startswith("rollout-") and name.endswith(".jsonl"):
                    path = Path(dirpath) / name
                    try:
                        mtime = path.stat().st_mtime
                    except OSError:
                        continue
                    if mtime >= cutoff:
                        found.append((mtime, path))
    return [path for _mtime, path in sorted(found, reverse=True)[:limit]]


_CREATOR = re.compile(rb'"creator_account_id"\s*:\s*"([^"]+)"')


def creator_account(path: Path) -> str | None:
    """The workspace that started a session (``session_meta``, first line).

    The first line also carries Codex's base instructions and can be large,
    so only its beginning is read; the account id comes before them.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(8192)
    except OSError:
        return None
    match = _CREATOR.search(head.split(b"\n", 1)[0])
    return match.group(1).decode("utf-8", errors="replace") if match else None


def newest_snapshot(home: Path, now: datetime, account: str | None = None) -> dict[str, Any]:
    """Newest main ("codex") snapshot plus the newest snapshot of each extra bucket.

    Sessions recorded by a different ChatGPT workspace than the signed-in one
    are skipped, so switching accounts never shows the old account's limits.
    """
    main = None
    extras: dict[str, tuple[datetime, dict[str, Any]]] = {}
    for path in rollout_files(home, now):
        try:
            stat = path.stat()
        except OSError:
            continue
        cached = _rollout_cache.get(str(path))
        if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
            creator, file_main, file_extras = cached[2]
        else:
            creator, file_main, file_extras = creator_account(path), None, {}
            try:
                for raw in iter_lines_reverse(path):
                    parsed = parse_rollout_line(raw)
                    if parsed is None:
                        continue
                    limit_id = parsed[1].get("limit_id")
                    if limit_id in (None, "codex"):
                        file_main = parsed
                        break
                    file_extras.setdefault(str(limit_id), parsed)
            except OSError:
                continue
            _rollout_cache[str(path)] = (stat.st_mtime, stat.st_size,
                                         (creator, file_main, file_extras))
        if account and creator and creator != account:
            continue
        if file_main and (main is None or file_main[0] > main[0]):
            main = file_main
        for limit_id, entry in file_extras.items():
            if limit_id not in extras or entry[0] > extras[limit_id][0]:
                extras[limit_id] = entry
    return {"main": main, "extras": extras}


def rollout_windows(snapshot: dict[str, Any]) -> tuple[list[UsageWindow], datetime | None,
                                                         str | None, str | None]:
    """Windows, observed time, plan and a "limit reached" note from the newest snapshot."""
    main = snapshot.get("main")
    if not main:
        return [], None, None, None
    observed, limits = main
    windows = bucket_windows(limits, observed)
    for limit_id, (seen, bucket) in snapshot.get("extras", {}).items():
        if seen < observed - timedelta(days=7):
            continue
        slug, name = _extra_name(bucket.get("limit_name"), None, limit_id)
        windows += bucket_windows(bucket, seen, prefix=f"{slug}_", name=name)
    windows += credit_windows(limits, observed)
    return (windows, observed, plan_label(limits.get("plan_type")),
            reached_note(limits.get("rate_limit_reached_type")))


# -- auth.json --------------------------------------------------------------------------------


def load_auth(path: Path) -> dict[str, Any] | None:
    for attempt in range(3):  # Codex replaces the file atomically; tolerate a racing read
        try:
            data = read_json(path)
            return data if isinstance(data, dict) else None
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            if attempt == 2:
                raise ProviderError(_("Codex's auth.json could not be read")) from None
            time.sleep(0.05)
    return None


def token_info(auth: dict[str, Any], now: datetime) -> dict[str, Any]:
    tokens = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}
    access = tokens.get("access_token") or tokens.get("accessToken")
    id_token = tokens.get("id_token") or tokens.get("idToken")
    claims = decode_jwt_claims(access)
    id_claims = decode_jwt_claims(id_token)
    account = tokens.get("account_id") or tokens.get("accountId") or \
        dig(claims, AUTH_CLAIMS, "chatgpt_account_id") or \
        dig(id_claims, AUTH_CLAIMS, "chatgpt_account_id")
    exp = to_float(claims.get("exp"))
    if exp is not None:
        fresh = exp > now.timestamp() + 300
    else:
        refreshed = parse_time(auth.get("last_refresh"))
        fresh = bool(refreshed and now - refreshed < timedelta(days=8))
    email = id_claims.get("email") or dig(claims, "https://api.openai.com/profile", "email")
    return {
        "access": access, "account": account, "fresh": bool(access) and fresh,
        "email": email, "plan": plan_label(dig(id_claims, AUTH_CLAIMS, "chatgpt_plan_type")),
        "fedramp": bool(dig(id_claims, AUTH_CLAIMS, "chatgpt_account_is_fedramp")),
        "api_key_only": bool(auth.get("OPENAI_API_KEY")) and not access,
    }


def base_url(home: Path) -> str:
    try:
        text = (home / "config.toml").read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_BASE
    match = re.search(r'(?m)^\s*chatgpt_base_url\s*=\s*"([^"]+)"', text)
    if not match:
        return DEFAULT_BASE
    url = match.group(1).strip().rstrip("/")
    if url.startswith(("https://chatgpt.com", "https://chat.openai.com")) and \
            "/backend-api" not in url:
        url += "/backend-api"
    return url


def usage_url(base: str) -> str:
    return f"{base}/wham/usage" if "/backend-api" in base else f"{base}/api/codex/usage"


# -- codex app-server ---------------------------------------------------------------------


def run_app_server(binary: str, home: Path, env: dict[str, str], timeout: float = 8.0
                   ) -> dict[str, Any]:
    """Ask ``codex app-server`` for rate limits over its stdio JSON-RPC.

    Messages are one JSON object per line. Replies are matched by id and
    server notifications in between are skipped. stdout is read straight from
    the pipe (no Python buffering) so ``select`` never misses a line.
    """
    child_env = dict(env)
    child_env["CODEX_HOME"] = str(home)
    proc = subprocess.Popen([binary, "-s", "read-only", "-a", "never", "app-server"],
                            env=child_env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, bufsize=0, start_new_session=True)
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    pending = bytearray()

    def send(message: dict[str, Any]) -> None:
        try:
            proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        except OSError:
            raise ProviderError(_("codex app-server stopped unexpectedly"),
                                transient=True) from None

    def wait(request_id: int, seconds: float) -> Any:
        deadline = time.monotonic() + seconds
        while True:
            while b"\n" in pending:
                index = pending.index(b"\n")
                raw = bytes(pending[:index])
                del pending[:index + 1]
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get("id") == request_id:
                    if "error" in message:
                        error = message["error"] if isinstance(message["error"], dict) else {}
                        raise ProviderError(str(error.get("message") or "error"))
                    return message.get("result")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not selector.select(timeout=min(remaining, 0.25)):
                if proc.poll() is not None:
                    break
                continue
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                break
            pending.extend(chunk)
        raise ProviderError(_("codex app-server didn't answer"), transient=True)

    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "quotaglance", "title": "QuotaGlance", "version": __version__},
            "capabilities": {"optOutNotificationMethods": ["remoteControl/status/changed"]}}})
        wait(1, timeout)
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read",
              "params": {"excludeResetCreditDetails": True}})
        return wait(2, 6.0) or {}
    finally:
        selector.close()
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)  # closing stdin makes app-server exit within ~20 ms
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.wait()
        proc.stdout.close()


def app_server_error(message: str) -> ProviderError:
    lowered = message.lower()
    if "authentication required" in lowered or "401" in lowered or "token_expired" in lowered:
        return AuthError(_("Codex isn't signed in to ChatGPT"), LOGIN_HINT)
    if "429" in lowered:
        return ProviderError(_("ChatGPT is rate-limiting usage checks"), transient=True,
                             retry_after=300)
    return ProviderError(_("codex app-server: {msg}").format(msg=message[:160]), transient=True)


def merge(*readings: tuple[list[UsageWindow], datetime | None]) -> list[UsageWindow]:
    best: dict[str, tuple[float, UsageWindow]] = {}
    order: list[str] = []
    for windows, observed in readings:
        stamp = observed.timestamp() if observed else 0.0
        for window in windows:
            if window.id not in best:
                order.append(window.id)
            if window.id not in best or stamp > best[window.id][0]:
                best[window.id] = (stamp, window)
    return [best[wid][1] for wid in order]


@dataclass
class LiveReading:
    windows: list[UsageWindow]
    observed: datetime
    plan: str | None
    via: str
    note: str | None = None


class CodexProvider(Provider):
    id = "codex"
    name = "Codex"
    short = "Cx"
    color = "#10A37F"
    category = "agents"
    homepage = "https://chatgpt.com/codex/settings/usage"
    source_summary = _("Codex session logs + ChatGPT usage (via Codex's own login)")
    setup_hint = _("Install the Codex CLI and sign in with ChatGPT (`codex login`).")
    refresh_seconds = 60  # logs are cheap; the API is called at most every 5 minutes
    settings = (
        SettingSpec("source", "choice", _("Data source"), _("Where QuotaGlance gets Codex limits"),
                    choices=(("auto", _("Automatic")),
                             ("logs", _("Session logs only (no network)"))), default="auto"),
        SettingSpec("home", "text", _("Codex home"),
                    _("Leave empty for ~/.codex (or $CODEX_HOME)"), default="",
                    placeholder="~/.codex"),
    )

    def detect(self, ctx: FetchContext) -> bool:
        home = codex_home(ctx)
        return (home / "auth.json").is_file() or (home / "sessions").is_dir()

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        now = ctx.now()
        home = codex_home(ctx)
        auth = load_auth(home / "auth.json")
        info = token_info(auth, now) if auth else None
        readings: list[tuple[list[UsageWindow], datetime | None]] = []
        sources: list[str] = []
        error: ProviderError | None = None

        log_windows, log_observed, log_plan, note = rollout_windows(
            newest_snapshot(home, now, account=info["account"] if info else None))
        if log_windows:
            readings.append((log_windows, log_observed))
            sources.append(_("session logs"))

        live = None
        if ctx.settings.get("source", "auto") == "auto":
            try:
                live = self._live(ctx, home, info)
            except ProviderError as exc:
                error = exc
        if live is not None:
            readings.append((live.windows, live.observed))
            sources.append(live.via)
            if live.observed >= (log_observed or live.observed):
                note = live.note
        # A fresh usage reading knows the plan best; the token's claim can be stale.
        plan = (live.plan if live else None) or log_plan or (info["plan"] if info else None)

        if not readings:
            if error is not None:
                raise error
            raise NotConfigured(_("No Codex usage found yet"), self.setup_hint)
        message = note
        if error is not None and not isinstance(error, NotConfigured):
            message = error.message + (f" — {error.hint}" if error.hint else "")
        observed = max((obs for _w, obs in readings if obs), default=None)
        text = " + ".join(dict.fromkeys(sources))
        return self.snapshot(ctx, merge(*readings), plan=plan,
                             account=info["email"] if info else None,
                             source=text[:1].upper() + text[1:], observed_at=observed,
                             message=message)

    # -- live reading ----------------------------------------------------------------------

    def _live(self, ctx: FetchContext, home: Path, info: dict[str, Any] | None
              ) -> LiveReading | None:
        """A live reading, taken at most every 5 minutes (and never during a back-off)."""
        if info and info["api_key_only"]:
            raise NotConfigured(_("Codex signs in with an API key: no ChatGPT plan limits"))
        key = str(home)
        clock = time.monotonic()
        stamp = _auth_stamp(home)
        with _lock:
            cached = dict(_api_cache.get(key) or {})
        recent = cached.get("result") if clock - cached.get("at", -1e12) < STALE_LIVE_MAX \
            else None
        blocked = clock < cached.get("blocked_until", 0)
        if blocked and (cached.get("rate_limited") or not ctx.force) and \
                cached.get("stamp") == stamp:
            if recent is not None or cached.get("error") is None:
                return recent
            raise cached["error"]
        if recent is not None and clock - cached["at"] < API_MIN_INTERVAL and not ctx.force:
            return recent
        try:
            result = self._read_live(ctx, home, info)
        except ProviderError as exc:
            limited = exc.retry_after is not None or "rate-limit" in exc.message
            pause = max(exc.retry_after or 0, API_MIN_INTERVAL if limited or not exc.transient
                        else 60.0)
            cached.update({"blocked_until": clock + pause, "error": exc,
                           "rate_limited": limited, "stamp": stamp})
            with _lock:
                _api_cache[key] = cached
            if exc.transient and recent is not None:
                return recent  # keep showing the last good live numbers for a while
            raise
        if result is not None:
            with _lock:
                _api_cache[key] = {"result": result, "at": clock, "stamp": stamp}
        return result

    def _read_live(self, ctx: FetchContext, home: Path, info: dict[str, Any] | None
                   ) -> LiveReading | None:
        now = ctx.now()
        failure: ProviderError | None = None
        if info and info["fresh"] and info["access"]:
            headers = {"Authorization": f"Bearer {info['access']}"}
            if info["account"]:
                headers["ChatGPT-Account-Id"] = info["account"]
            if info["fedramp"]:
                headers["X-OpenAI-Fedramp"] = "true"
            try:
                data = ctx.http.get_json(usage_url(base_url(home)), headers=headers, timeout=30)
                windows, plan = parse_usage(data or {}, now)
                return LiveReading(windows, now, plan, _("ChatGPT usage API"),
                                   reached_note((data or {}).get("rate_limit_reached_type")))
            except HttpError as exc:
                failure = from_http_error(exc, login_hint=LOGIN_HINT, service="ChatGPT")
                if exc.status == 429:
                    raise ProviderError(_("ChatGPT is rate-limiting usage checks"),
                                        transient=True,
                                        retry_after=max(exc.retry_after or 0, 300)) from None
                if exc.status not in (401, 403):
                    raise failure from None
                # Rejected or behind a Cloudflare check: let Codex itself try.
            except NetworkError as exc:
                raise ProviderError(str(exc), transient=True) from None
        binary = ctx.env.get("CODEX_CLI_PATH") or ctx.which("codex")
        if not binary:
            if failure is not None:
                raise failure
            if info is not None:
                raise AuthError(_("Codex's login needs a refresh"), LOGIN_HINT)
            return None  # no login and no CLI: session logs are all there is
        try:
            answer = run_app_server(binary, home, dict(ctx.env))
        except OSError as exc:
            raise ProviderError(_("Could not start Codex: {err}").format(err=exc)) from None
        except ProviderError as exc:
            if exc.transient:
                raise
            raise app_server_error(exc.message) from None
        windows, plan = parse_app_server(answer, now)
        main = answer.get("rateLimits") or {}
        return LiveReading(windows, now, plan, _("Codex app-server"),
                           reached_note(main.get("rateLimitReachedType")))


def _auth_stamp(home: Path) -> float | None:
    """Changes whenever Codex rewrites its login, which lifts any back-off."""
    try:
        return (home / "auth.json").stat().st_mtime
    except OSError:
        return None


def reset_caches() -> None:
    with _lock:
        _api_cache.clear()
    _rollout_cache.clear()
