"""Claude Code: 5-hour session, weekly and model-specific limits, extra usage.

Sources, merged per window (the newest reading wins):

* **Anthropic's usage endpoint** (what Claude Code's ``/usage`` shows), called
  with the login Claude Code saved in ``~/.claude/.credentials.json``. The
  token is used read-only and in memory only, sent to ``api.anthropic.com``
  and nowhere else, and never refreshed: Claude refresh tokens rotate, so a
  refresh here would sign you out of Claude Code. At most one call per five
  minutes, because the endpoint is rate-limited.
* **The status-line bridge** (optional, zero network): Claude Code itself
  reports the 5-hour and weekly usage to its status line on every message.
* **Local transcripts** (``~/.claude/projects``): a "limit reached, resets at"
  marker when you hit a limit, and token estimates when nothing else works.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from quotaglance import claude_statusline
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
from quotaglance.timefmt import format_amount
from quotaglance.util import clamp, parse_time, read_json, to_float

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_MIN_INTERVAL = 300.0  # seconds; the endpoint 429s aggressive pollers
RATE_LIMIT_BACKOFF = 300.0
DEFAULT_CLI_VERSION = "2.1.0"
LOGIN_HINT = _("Run `claude` and sign in (or open it once so it refreshes its login).")

ROUTINE_KEYS = ("seven_day_routines", "seven_day_claude_routines", "claude_routines", "routines",
                "routine", "seven_day_cowork", "cowork")
LIMIT_TEXT_RE = re.compile(r"\|(\d{9,11})\s*$")

_lock = threading.Lock()
_oauth_cache: dict[str, dict[str, Any]] = {}  # credentials path -> last API outcome
_scan_cache: dict[str, tuple[float, int, list[dict[str, Any]]]] = {}


# -- files ------------------------------------------------------------------------


def config_dir(ctx: FetchContext) -> Path:
    custom = ctx.env.get("CLAUDE_CONFIG_DIR", "").strip()
    return ctx.path(custom) if custom else ctx.path("~/.claude")


def credentials_path(ctx: FetchContext) -> Path:
    secure = ctx.env.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")
    if secure is not None:
        return (ctx.path(secure) if secure else ctx.path("~/.claude")) / ".credentials.json"
    return config_dir(ctx) / ".credentials.json"


def load_credentials(path: Path) -> dict[str, Any] | None:
    try:
        data = read_json(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ProviderError(_("Claude Code's credentials file is unreadable")) from exc
    if not isinstance(data, dict):
        raise ProviderError(_("Claude Code's credentials file is unreadable"))
    oauth = data.get("claudeAiOauth")
    return oauth if isinstance(oauth, dict) else {}


def account_email(ctx: FetchContext) -> str | None:
    base = config_dir(ctx)
    custom = bool(ctx.env.get("CLAUDE_CONFIG_DIR", "").strip())
    candidates = [base / ".config.json", base / ".claude.json"] if custom else [
        base / ".config.json", ctx.path("~/.claude.json")]
    for path in candidates:
        try:
            account = read_json(path).get("oauthAccount") or {}
        except (OSError, ValueError, AttributeError):
            continue
        email = account.get("emailAddress") if isinstance(account, dict) else None
        if email:
            return str(email)
    return None


def cli_version(ctx: FetchContext) -> str:
    """Claude Code's version, from the native installer's symlink (no process spawn)."""
    for name in ("~/.local/bin/claude", "~/.claude/local/claude"):
        link = ctx.path(name)
        try:
            target = os.path.realpath(link)
        except OSError:
            continue
        match = re.search(r"/versions/(\d+\.\d+\.\d+)", target)
        if match:
            return match.group(1)
    return DEFAULT_CLI_VERSION


# -- plan -------------------------------------------------------------------------


def plan_label(subscription: Any, tier: Any) -> str | None:
    words_tier = [w for w in re.split(r"[^a-z0-9]+", str(tier or "").lower()) if w]
    words_sub = [w for w in re.split(r"[^a-z0-9]+", str(subscription or "").lower()) if w]
    words = words_sub or words_tier
    base = None
    for key, label in (("max", "Max"), ("pro", "Pro"), ("team", "Team"),
                       ("enterprise", "Enterprise"), ("ultra", "Ultra")):
        if key in words:
            base = label
            break
    if "max" in words_tier:
        base = "Max"  # the tier is what Anthropic enforces; subscriptionType can lag
    if base is None:
        return None
    if base == "Max" and "max" in words_tier:
        index = words_tier.index("max")
        if index + 1 < len(words_tier) and re.fullmatch(r"\d+x", words_tier[index + 1]):
            return f"Max {words_tier[index + 1]}"
    return base


# -- parsing: Anthropic usage API ---------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _window(data: Any) -> tuple[float, datetime | None] | None:
    if not isinstance(data, dict):
        return None
    used = to_float(data.get("utilization"))
    if used is None:
        return None
    return used, parse_time(data.get("resets_at"))


def parse_usage(data: dict[str, Any]) -> list[UsageWindow]:
    """Map ``/api/oauth/usage`` onto windows. Utilisation is already 0..100."""
    if not isinstance(data, dict):
        raise ProviderError(_("Unexpected response from Anthropic"))
    week = 7 * 86400
    windows: dict[str, UsageWindow] = {}

    def add(wid: str, label: str, value, seconds: int | None) -> None:
        if value is not None and wid not in windows:
            used, resets_at = value
            windows[wid] = UsageWindow(id=wid, label=label, used_percent=clamp(used, 0, 100),
                                       resets_at=resets_at, window_seconds=seconds)

    add("session", _("Session"), _window(data.get("five_hour")), 5 * 3600)
    add("weekly", _("Weekly"), _window(data.get("seven_day")), week)

    scoped: dict[str, UsageWindow] = {}
    for entry in data.get("limits") or []:
        if not isinstance(entry, dict):
            continue
        percent = to_float(entry.get("percent"))
        resets_at = parse_time(entry.get("resets_at"))
        kind = entry.get("kind")
        if percent is None:
            continue
        if kind == "session":
            add("session", _("Session"), (percent, resets_at), 5 * 3600)
        elif kind == "weekly_all":
            add("weekly", _("Weekly"), (percent, resets_at), week)
        elif kind == "weekly_scoped" and entry.get("group") == "weekly":
            model = ((entry.get("scope") or {}).get("model") or {})
            name = str(model.get("display_name") or "").strip()
            model_id = _slug(str(model.get("id") or ""))
            if not name or _slug(name) == "all-models" or model_id == "all-models" or \
                    model_id.endswith("-all-models"):
                continue
            wid = f"weekly_{_slug(model.get('id') or name)}"
            label = _("{model} only").format(model=name)
            if len(label) > 12:
                label = name
            scoped[wid] = UsageWindow(id=wid, label=label, used_percent=clamp(percent, 0, 100),
                                      resets_at=resets_at, window_seconds=week)

    scoped_names = {w.id.split("_", 1)[1] for w in scoped.values()}
    for key, wid, label in (("seven_day_sonnet", "weekly_sonnet", _("Sonnet")),
                            ("seven_day_opus", "weekly_opus", _("Opus"))):
        if wid.split("_", 1)[1] not in scoped_names:
            add(wid, label, _window(data.get(key)), week)
    windows.update(scoped)
    add("weekly_apps", _("OAuth apps"), _window(data.get("seven_day_oauth_apps")), week)
    for key in ROUTINE_KEYS:
        value = _window(data.get(key))
        if value is not None:
            add("routines", _("Routines"), value, week)
            break

    extra = data.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled") is True and \
            extra.get("monthly_limit") is not None and extra.get("used_credits") is not None:
        limit = (to_float(extra.get("monthly_limit")) or 0.0) / 100.0
        used = (to_float(extra.get("used_credits")) or 0.0) / 100.0
        percent = to_float(extra.get("utilization"))
        if percent is None:
            percent = used / limit * 100 if limit > 0 else 0.0
        currency = str(extra.get("currency") or "").strip().upper() or "USD"
        only = not windows
        windows["spend" if only else "extra"] = UsageWindow(
            id="spend" if only else "extra",
            label=_("Spend limit") if only else _("Extra usage"),
            used_percent=clamp(percent, 0, 100), used=used, limit=limit, unit=currency,
            detail=_("{used} of {limit} this month").format(
                used=format_amount(used, currency), limit=format_amount(limit, currency)))
    return list(windows.values())


# -- parsing: status-line bridge ------------------------------------------------------


def parse_statusline(record: dict[str, Any]) -> tuple[list[UsageWindow], datetime | None]:
    limits = record.get("rate_limits") if isinstance(record, dict) else None
    if not isinstance(limits, dict):
        return [], None
    observed = parse_time(record.get("observed_at"))
    windows = []
    for key, wid, label, seconds in (("five_hour", "session", _("Session"), 5 * 3600),
                                     ("seven_day", "weekly", _("Weekly"), 7 * 86400)):
        entry = limits.get(key)
        if not isinstance(entry, dict):
            continue
        used = to_float(entry.get("used_percentage"))
        if used is None:
            continue
        windows.append(UsageWindow(id=wid, label=label, used_percent=clamp(used, 0, 100),
                                   resets_at=parse_time(entry.get("resets_at")),
                                   window_seconds=seconds))
    spend = limits.get("spend_limit")
    if isinstance(spend, dict) and to_float(spend.get("used_percentage")) is not None:
        windows.append(UsageWindow(
            id="spend", label=_("Spend limit"),
            used_percent=clamp(to_float(spend.get("used_percentage")) or 0.0, 0, 100),
            resets_at=parse_time(spend.get("resets_at")), used=to_float(spend.get("used_usd")),
            limit=to_float(spend.get("limit_usd")), unit="USD"))
    return windows, observed


def read_statusline(ctx: FetchContext) -> tuple[list[UsageWindow], datetime | None]:
    path = ctx.xdg("XDG_CACHE_HOME", "~/.cache") / "quotaglance" / "claude-statusline.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], None
    return parse_statusline(record)


# -- parsing: local transcripts -----------------------------------------------------------


def transcript_roots(ctx: FetchContext) -> list[Path]:
    custom = ctx.env.get("CLAUDE_CONFIG_DIR", "").strip()
    if custom:
        roots = [ctx.path(custom) / "projects"]
    else:
        roots = [ctx.config_home / "claude" / "projects", ctx.path("~/.claude/projects")]
    seen, result = set(), []
    for root in roots:
        try:
            real = root.resolve()
        except OSError:
            continue
        if real not in seen and root.is_dir():
            seen.add(real)
            result.append(root)
    return result


def parse_transcript_line(line: str) -> dict[str, Any] | None:
    """One usage entry (or a limit-hit marker) from a transcript line."""
    if '"assistant"' not in line:
        return None
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("type") != "assistant":
        return None
    timestamp = parse_time(data.get("timestamp"))
    message = data.get("message") or {}
    if timestamp is None or not isinstance(message, dict):
        return None
    if data.get("isApiErrorMessage"):
        text = " ".join(part.get("text", "") for part in message.get("content") or []
                        if isinstance(part, dict))
        match = LIMIT_TEXT_RE.search(text.strip())
        if "limit" in text.lower():
            return {"ts": timestamp, "limit_reset": parse_time(int(match.group(1)))
                    if match else None, "tokens": 0, "key": None}
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict) or message.get("model") == "<synthetic>":
        return None
    tokens = sum(int(to_float(usage.get(k)) or 0) for k in (
        "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    if tokens <= 0:
        return None
    message_id, request_id = message.get("id"), data.get("requestId")
    key = (message_id, request_id) if message_id and request_id else (
        (data.get("sessionId"), message_id) if message_id and data.get("sessionId") else None)
    return {"ts": timestamp, "tokens": tokens, "key": key, "limit_reset": None}


def scan_transcripts(ctx: FetchContext, since: datetime) -> list[dict[str, Any]]:
    """Deduplicated usage entries newer than ``since`` (cached per file)."""
    cutoff = since.timestamp()
    entries: dict[Any, dict[str, Any]] = {}
    markers: list[dict[str, Any]] = []
    for root in transcript_roots(ctx):
        for path in root.rglob("*.jsonl"):
            name = path.name
            if ".orphaned-" in name or "tool-results" in path.parts:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime < cutoff:
                continue
            key = str(path)
            cached = _scan_cache.get(key)
            if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
                parsed = cached[2]
            else:
                parsed = []
                try:
                    with open(path, encoding="utf-8", errors="replace") as handle:
                        for line in handle:
                            entry = parse_transcript_line(line)
                            if entry is not None:
                                parsed.append(entry)
                except OSError:
                    continue
                _scan_cache[key] = (stat.st_mtime, stat.st_size, parsed)
            for entry in parsed:
                if entry["ts"].timestamp() < cutoff:
                    continue
                if entry.get("limit_reset") is not None or entry["tokens"] == 0:
                    markers.append(entry)
                    continue
                dedupe = entry["key"] or id(entry)
                if dedupe not in entries or entry["tokens"] > entries[dedupe]["tokens"]:
                    entries[dedupe] = entry
    return sorted(entries.values(), key=lambda e: e["ts"]) + markers


def five_hour_block(entries: list[dict[str, Any]], now: datetime
                    ) -> tuple[datetime | None, int]:
    """ccusage-style active 5-hour block: (block end, tokens) or (None, 0)."""
    duration = timedelta(hours=5)
    start = None
    last = None
    tokens = 0
    for entry in entries:
        if entry["tokens"] == 0:
            continue
        ts = entry["ts"]
        if start is None or ts - start > duration or (last and ts - last > duration):
            start = ts.replace(minute=0, second=0, microsecond=0)
            tokens = 0
        tokens += entry["tokens"]
        last = ts
    if start is None or last is None or now - last >= duration or now >= start + duration:
        return None, 0
    return start + duration, tokens


def estimate_windows(entries: list[dict[str, Any]], now: datetime) -> list[UsageWindow]:
    markers = [e for e in entries if e.get("limit_reset")]
    limit_reset = max((e["limit_reset"] for e in markers), default=None)
    windows = []
    if limit_reset and limit_reset > now:
        windows.append(UsageWindow(id="session", label=_("Session"), used_percent=100.0,
                                   resets_at=limit_reset, window_seconds=5 * 3600,
                                   detail=_("Limit reached")))
        return windows
    block_end, tokens = five_hour_block(entries, now)
    if block_end is not None:
        windows.append(UsageWindow(id="session_tokens", label=_("Session"), resets_at=block_end,
                                   used=float(tokens), unit="tokens",
                                   detail=_("{n} tokens (estimate)").format(
                                       n=format_amount(float(tokens)))))
    week = sum(e["tokens"] for e in entries if e["ts"] >= now - timedelta(days=7))
    if week:
        windows.append(UsageWindow(id="week_tokens", label=_("7 days"), used=float(week),
                                   unit="tokens", detail=_("{n} tokens (estimate)").format(
                                       n=format_amount(float(week)))))
    return windows


def merge(*readings: tuple[list[UsageWindow], datetime | None]) -> list[UsageWindow]:
    """Per window id, keep the reading with the newest observation time."""
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


class ClaudeProvider(Provider):
    id = "claude"
    name = "Claude Code"
    short = "Cl"
    color = "#D97757"
    category = "agents"
    homepage = "https://claude.ai/settings/usage"
    source_summary = _("Claude Code sign-in → Anthropic usage API, status line, local logs")
    setup_hint = _("Install Claude Code and sign in with your Claude subscription (run `claude`).")
    refresh_seconds = 60  # local sources are cheap; the API is still called at most every 5 min
    settings = (
        SettingSpec("source", "choice", _("Data source"),
                    _("Where QuotaGlance gets Claude's limits"),
                    choices=(("auto", _("Automatic")),
                             ("statusline", _("Status line only (no network)")),
                             ("logs", _("Local logs only (estimates)"))),
                    default="auto"),
        SettingSpec("statusline", "switch", _("Status line bridge"),
                    _("Let Claude Code report usage to QuotaGlance on every message (wraps your "
                      "status line, keeping its output)"), default=False),
    )

    def detect(self, ctx: FetchContext) -> bool:
        return credentials_path(ctx).is_file() or bool(transcript_roots(ctx))

    def setting_value(self, ctx: FetchContext, spec: SettingSpec) -> Any:
        if spec.key == "statusline":
            return claude_statusline.is_installed(config_dir(ctx) / "settings.json")
        return super().setting_value(ctx, spec)

    def apply_setting(self, ctx: FetchContext, key: str, value: Any) -> str | None:
        if key != "statusline":
            return None
        from quotaglance.autostart import launch_command

        settings = config_dir(ctx) / "settings.json"
        try:
            if value:
                claude_statusline.install(launch_command(), settings)
                return _("Connected. Claude Code now reports usage on every message.")
            claude_statusline.uninstall(settings)
            return _("Disconnected; your previous status line is back.")
        except (OSError, ValueError) as exc:
            raise ProviderError(_("Couldn't update Claude Code's settings: {err}").format(
                err=exc)) from exc

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        now = ctx.now()
        source = ctx.settings.get("source", "auto")
        readings: list[tuple[list[UsageWindow], datetime | None]] = []
        sources: list[str] = []
        plan = None
        api_error: ProviderError | None = None

        line_windows, line_observed = read_statusline(ctx)
        if line_windows and source in ("auto", "statusline"):
            readings.append((line_windows, line_observed))
            sources.append(_("status line"))

        creds_file = credentials_path(ctx)
        if source == "auto":
            try:
                creds = load_credentials(creds_file)
            except ProviderError as exc:
                creds, api_error = None, exc
            if creds is not None:
                plan = plan_label(creds.get("subscriptionType"), creds.get("rateLimitTier"))
                try:
                    windows, fetched = self._api_windows(ctx, creds_file, creds)
                    if windows:
                        readings.append((windows, fetched))
                        sources.append(_("Anthropic API"))
                except ProviderError as exc:
                    api_error = exc
            elif api_error is None and not readings:
                api_error = NotConfigured(_("Not signed in to Claude Code"), LOGIN_HINT)

        if source == "logs" or not readings:
            entries = scan_transcripts(ctx, now - timedelta(days=7))
            estimates = estimate_windows(entries, now)
            if estimates:
                readings.append((estimates, now))
                sources.append(_("local logs"))

        if not readings:
            if api_error is not None:
                raise api_error
            raise NotConfigured(_("No Claude Code usage found yet"), self.setup_hint)
        windows = merge(*readings)
        observed = max((obs for _w, obs in readings if obs), default=None)
        message = api_error.message if api_error and not isinstance(api_error, NotConfigured) \
            else None
        if message and api_error.hint:
            message = f"{message} — {api_error.hint}"
        source_text = " + ".join(dict.fromkeys(sources))
        return self.snapshot(ctx, windows, plan=plan, account=account_email(ctx),
                             source=source_text[:1].upper() + source_text[1:],
                             observed_at=observed, message=message)

    # -- Anthropic API with caching and backoff ---------------------------------------------

    def _api_windows(self, ctx: FetchContext, creds_file: Path, creds: dict[str, Any]
                     ) -> tuple[list[UsageWindow], datetime | None]:
        token = str(creds.get("accessToken") or "").strip()
        if not token:
            raise AuthError(_("No claude.ai login in Claude Code"), LOGIN_HINT)
        scopes = creds.get("scopes")
        if isinstance(scopes, list) and "user:profile" not in scopes:
            raise AuthError(_("This Claude token can't read usage"),
                            _("Sign in with `claude` (a `setup-token` token has no usage "
                              "access)."))
        expires = parse_time(creds.get("expiresAt"))
        now = ctx.now()
        if expires is None or expires <= now + timedelta(seconds=60):
            raise AuthError(_("Claude Code's login has expired"),
                            _("Open Claude Code once; it refreshes its own login."))
        key = str(creds_file)
        mtime = _mtime(creds_file)
        with _lock:
            cached = _oauth_cache.get(key)
        clock = time.monotonic()
        if cached:
            if cached.get("blocked_until", 0) > clock and cached.get("mtime") == mtime:
                if cached.get("windows"):
                    return cached["windows"], cached.get("fetched")
                raise cached["error"]
            fresh = clock - cached.get("at", 0) < OAUTH_MIN_INTERVAL
            if fresh and cached.get("token") == token and cached.get("windows") and \
                    not getattr(ctx, "force", False):
                return cached["windows"], cached.get("fetched")
        headers = {
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
            "User-Agent": f"claude-code/{cli_version(ctx)}",
        }
        try:
            data = ctx.http.get_json(USAGE_URL, headers=headers, timeout=30)
        except HttpError as exc:
            error = self._http_error(exc)
            entry = dict(cached or {})
            entry.update({"error": error, "mtime": mtime, "at": clock})
            if exc.status == 429:
                entry["blocked_until"] = clock + max(RATE_LIMIT_BACKOFF, exc.retry_after or 0)
            elif exc.status == 401:
                entry["blocked_until"] = clock + 10**9  # until the credentials file changes
                entry["windows"] = None
            with _lock:
                _oauth_cache[key] = entry
            if exc.status == 429 and cached and cached.get("windows"):
                return cached["windows"], cached.get("fetched")
            raise error from None
        except NetworkError as exc:
            raise ProviderError(str(exc), transient=True) from None
        windows = parse_usage(data or {})
        if not windows:
            raise ProviderError(_("Anthropic reports no usage limits for this plan"))
        with _lock:
            _oauth_cache[key] = {"windows": windows, "fetched": now, "at": clock,
                                 "token": token, "mtime": mtime}
        return windows, now

    @staticmethod
    def _http_error(exc: HttpError) -> ProviderError:
        if exc.status == 401:
            return AuthError(_("Claude Code's login was rejected"), LOGIN_HINT)
        if exc.status == 403 and "user:profile" in exc.text(2000):
            return AuthError(_("This Claude token can't read usage"),
                             _("Sign in again with `claude`."))
        if exc.status == 429:
            return ProviderError(_("Anthropic is rate-limiting usage checks; retrying in a few "
                                   "minutes"), transient=True,
                                 retry_after=max(RATE_LIMIT_BACKOFF, exc.retry_after or 0))
        return from_http_error(exc, login_hint=LOGIN_HINT, service="Anthropic")


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def reset_caches() -> None:
    with _lock:
        _oauth_cache.clear()
    _scan_cache.clear()

