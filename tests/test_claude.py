import json
import os

import pytest

from conftest import NOW, FakeHttp, http_error, load_json, load_text, make_ctx, write
from quotaglance import claude_statusline
from quotaglance.providers import claude
from quotaglance.providers.base import AuthError, NotConfigured, ProviderError
from quotaglance.providers.claude import (
    USAGE_URL,
    ClaudeProvider,
    estimate_windows,
    parse_statusline,
    parse_usage,
    plan_label,
    scan_transcripts,
)

HOUR_MS = 3600 * 1000


@pytest.fixture(autouse=True)
def clean_caches():
    claude.reset_caches()
    yield
    claude.reset_caches()


def write_credentials(home, *, expires_ms=None, scopes=None, sub="max",
                      tier="default_claude_max_20x"):
    expires_ms = expires_ms if expires_ms is not None else NOW.timestamp() * 1000 + 8 * HOUR_MS
    write(home / ".claude/.credentials.json", json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat01-FIXTURE", "refreshToken": "sk-ant-ort01-FIXTURE",
        "expiresAt": expires_ms,
        "scopes": scopes or ["user:inference", "user:profile", "user:sessions:claude_code"],
        "subscriptionType": sub, "rateLimitTier": tier}}))
    write(home / ".claude.json", json.dumps({"oauthAccount": {
        "emailAddress": "alex@example.com", "accountUuid": "3c0f"}}))


def by_id(windows):
    return {w.id: w for w in windows}


def test_current_shape_with_scoped_sonnet_and_ignored_keys():
    windows = by_id(parse_usage(load_json("claude", "usage_max20x.json")))
    assert set(windows) == {"session", "weekly", "weekly_sonnet"}
    assert windows["session"].used_percent == 23
    assert windows["session"].resets_at.isoformat().startswith("2026-10-03T14:00:00")
    assert windows["weekly"].used_percent == 41
    assert windows["weekly_sonnet"].label == "Sonnet only"
    assert windows["weekly_sonnet"].used_percent == 12


def test_promotional_scope_and_all_models_dedupe():
    windows = by_id(parse_usage(load_json("claude", "usage_promo_scoped.json")))
    assert windows["weekly_fable"].label == "Fable only"
    assert windows["weekly_fable"].used_percent == 27
    assert not any("all" in wid for wid in windows)


def test_legacy_flat_shape_with_idle_opus():
    windows = by_id(parse_usage(load_json("claude", "usage_legacy_pro.json")))
    assert windows["session"].used_percent == 9
    assert windows["weekly_opus"].used_percent == 0
    assert windows["weekly_opus"].resets_at is None


def test_extra_usage_is_in_cents():
    windows = by_id(parse_usage(load_json("claude", "usage_extra.json")))
    extra = windows["extra"]
    assert (extra.used, extra.limit, extra.unit) == (6.4, 25.0, "USD")
    assert extra.used_percent == pytest.approx(25.6)
    assert windows["session"].used_percent == 100


def test_enterprise_spend_limit_only():
    windows = parse_usage(load_json("claude", "usage_enterprise.json"))
    assert [(w.id, w.label) for w in windows] == [("spend", "Spend limit")]
    assert windows[0].used_percent == pytest.approx(36.5)
    assert windows[0].detail == "€182.50 of €500.00 this month"


def test_routines_alias_and_design_keys_ignored():
    windows = by_id(parse_usage(load_json("claude", "usage_routines.json")))
    assert windows["routines"].used_percent == 8
    assert "weekly_omelette" not in windows


@pytest.mark.parametrize(("sub", "tier", "expected"), [
    ("max", "default_claude_max_20x", "Max 20x"),
    ("pro", "default_claude_max_5x", "Max 5x"),
    ("pro", "default_claude_ai", "Pro"),
    ("team", "default_claude_team_5x", "Team"),
    (None, "claude_enterprise", "Enterprise"),
    (None, None, None),
])
def test_plan_label(sub, tier, expected):
    assert plan_label(sub, tier) == expected


def test_fetch_sends_oauth_headers_and_reads_account(home):
    write_credentials(home)
    http = FakeHttp({("GET", USAGE_URL): load_json("claude", "usage_max20x.json")})
    snap = ClaudeProvider().fetch(make_ctx(home, http=http))
    headers = http.last(USAGE_URL)["headers"]
    assert headers["Authorization"] == "Bearer sk-ant-oat01-FIXTURE"
    assert headers["anthropic-beta"] == "oauth-2025-04-20"
    assert headers["User-Agent"] == "claude-code/2.1.0"
    assert snap.plan == "Max 20x"
    assert snap.account == "alex@example.com"
    assert snap.source == "Anthropic API"


def test_cli_version_from_native_installer_symlink(home):
    version_dir = home / ".local/share/claude/versions/2.1.288"
    write(version_dir, "#!/bin/sh\n")
    (home / ".local/bin").mkdir(parents=True)
    os.symlink(version_dir, home / ".local/bin/claude")
    write_credentials(home)
    http = FakeHttp({("GET", USAGE_URL): load_json("claude", "usage_max20x.json")})
    ClaudeProvider().fetch(make_ctx(home, http=http))
    assert http.last(USAGE_URL)["headers"]["User-Agent"] == "claude-code/2.1.288"


def test_api_is_called_at_most_every_five_minutes_unless_forced(home):
    write_credentials(home)
    http = FakeHttp({("GET", USAGE_URL): load_json("claude", "usage_max20x.json")})
    provider = ClaudeProvider()
    provider.fetch(make_ctx(home, http=http))
    provider.fetch(make_ctx(home, http=http))
    assert len(http.calls) == 1
    forced = make_ctx(home, http=http)
    forced.force = True
    provider.fetch(forced)
    assert len(http.calls) == 2


def test_expired_login_never_calls_the_api(home):
    write_credentials(home, expires_ms=NOW.timestamp() * 1000 - 1000)
    with pytest.raises(AuthError, match="expired"):
        ClaudeProvider().fetch(make_ctx(home))  # FakeHttp would fail on any request


def test_missing_usage_scope(home):
    write_credentials(home, scopes=["user:inference"])
    with pytest.raises(AuthError, match="can't read usage"):
        ClaudeProvider().fetch(make_ctx(home))


def test_rate_limit_backs_off_and_keeps_last_good_numbers(home):
    write_credentials(home)
    responses = [load_json("claude", "usage_max20x.json"),
                 http_error(429, {"error": {"type": "rate_limit_error"}})]
    http = FakeHttp({("GET", USAGE_URL): lambda *a: responses.pop(0)})
    provider = ClaudeProvider()
    provider.fetch(make_ctx(home, http=http))
    forced = make_ctx(home, http=http)
    forced.force = True
    snap = provider.fetch(forced)  # 429: falls back to the cached reading
    assert snap.windows[0].used_percent == 23
    blocked = make_ctx(home, http=http)
    blocked.force = True
    provider.fetch(blocked)
    assert len(http.calls) == 2  # still inside the back-off window


def test_401_is_an_auth_error(home):
    write_credentials(home)
    http = FakeHttp({("GET", USAGE_URL): http_error(401, {"type": "error"})})
    with pytest.raises(AuthError):
        ClaudeProvider().fetch(make_ctx(home, http=http))


def test_statusline_snapshot_is_newer_and_wins(home):
    write_credentials(home)
    record = {"observed_at": NOW.timestamp() + 60, "rate_limits": {
        "five_hour": {"used_percentage": 57.5, "resets_at": 1791043200},
        "seven_day": {"used_percentage": 44, "resets_at": 1791363600}}}
    write(home / ".cache/quotaglance/claude-statusline.json", json.dumps(record))
    http = FakeHttp({("GET", USAGE_URL): load_json("claude", "usage_max20x.json")})
    snap = ClaudeProvider().fetch(make_ctx(home, http=http))
    windows = by_id(snap.windows)
    assert windows["session"].used_percent == 57.5
    assert windows["weekly_sonnet"].used_percent == 12  # only the API knows this one
    assert snap.source == "Status line + Anthropic API"


def test_statusline_only_mode_without_network(home):
    record = {"observed_at": NOW.timestamp() - 30, "rate_limits": {
        "five_hour": {"used_percentage": 12, "resets_at": 1791043200}}}
    write(home / ".cache/quotaglance/claude-statusline.json", json.dumps(record))
    snap = ClaudeProvider().fetch(make_ctx(home, settings={"source": "statusline"}))
    assert [(w.id, w.used_percent) for w in snap.windows] == [("session", 12)]
    windows, _observed = parse_statusline({"rate_limits": {"spend_limit": {
        "used_percentage": 62.8, "used_usd": 314.12, "limit_usd": 500}}})
    assert windows[0].id == "spend" and windows[0].limit == 500


def test_transcripts_dedupe_and_limit_marker(home):
    project = home / ".claude/projects/-home-alex-src-app"
    write(project / "6b1f0e4a.jsonl", load_text("claude", "transcript.jsonl"))
    entries = scan_transcripts(make_ctx(home), NOW.replace(hour=0))
    usage = [e for e in entries if e["tokens"]]
    assert len(usage) == 2  # the duplicated msg_01A counts once
    assert max(e["tokens"] for e in usage) == 6 + 2841 + 18352 + 412
    windows = estimate_windows(entries, NOW)
    assert windows[0].id == "session_tokens"
    assert windows[0].resets_at.isoformat() == "2026-10-03T15:00:00+00:00"
    write(project / "6b1f0e4a-2.jsonl", load_text("claude", "limit_hit.jsonl"))
    claude.reset_caches()
    windows = estimate_windows(scan_transcripts(make_ctx(home), NOW.replace(hour=0)), NOW)
    assert (windows[0].id, windows[0].used_percent) == ("session", 100.0)
    assert windows[0].resets_at.isoformat() == "2026-10-03T16:00:00+00:00"


def test_logs_fallback_when_not_signed_in(home):
    write(home / ".claude/projects/p/s.jsonl", load_text("claude", "transcript.jsonl"))
    snap = ClaudeProvider().fetch(make_ctx(home))
    assert snap.source == "Local logs"
    assert snap.windows[0].unit == "tokens"


def test_not_configured(home):
    provider = ClaudeProvider()
    assert not provider.detect(make_ctx(home))
    with pytest.raises(NotConfigured):
        provider.fetch(make_ctx(home))


def test_unreadable_credentials(home):
    write(home / ".claude/.credentials.json", "{not json")
    with pytest.raises(ProviderError, match="unreadable"):
        ClaudeProvider().fetch(make_ctx(home))


def test_statusline_install_wraps_and_restores(home, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    settings = home / ".claude/settings.json"
    original = {"type": "command", "command": "echo mine"}
    write(settings, json.dumps({"model": "opus", "statusLine": original}))
    claude_statusline.install(["/usr/bin/quotaglance"], settings)
    data = json.loads(settings.read_text())
    assert data["statusLine"]["command"] == "/usr/bin/quotaglance --claude-statusline"
    assert data["model"] == "opus"
    assert claude_statusline.is_installed(settings)
    output = claude_statusline.run_tap(json.dumps({"rate_limits": {
        "five_hour": {"used_percentage": 30, "resets_at": 1791043200}}}).encode())
    assert output == "mine"
    assert json.loads(claude_statusline.snapshot_path().read_text())["rate_limits"]
    claude_statusline.uninstall(settings)
    assert json.loads(settings.read_text())["statusLine"] == original
