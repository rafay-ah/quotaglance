from datetime import datetime, timedelta, timezone

import pytest

from conftest import NOW, FakeHttp, FakeRunner, http_error, load_json, load_text, make_ctx, write
from quotaglance.providers.amp import (
    AmpProvider,
    next_free_reset,
    parse_balance_response,
    parse_usage_text,
)
from quotaglance.providers.base import AuthError, NotConfigured, ProviderError

BALANCE_URL = "https://ampcode.com/api/internal?userDisplayBalanceInfo"
LEGACY_KEY = "sgamp_user_01JFIXTURE0000000000000000_0123456789abcdef0123456789abcdef"


def make_cli(home):
    amp = write(home / ".local/bin/amp", "#!/bin/sh\n")
    amp.chmod(0o755)
    return amp


def test_tier_uses_exact_dollars_and_period_dates():
    windows, plan, account = parse_usage_text(load_text("amp", "usage_tier.txt"), NOW)
    assert (plan, account) == ("Megawatt", "dev@example.com")
    agent, orb, credits = windows
    assert (agent.id, agent.label) == ("agent", "Agent usage")
    assert agent.used_percent == pytest.approx(29.0)  # from $14.20 of $20, not the rounded 71%
    assert (agent.used, agent.limit, agent.unit) == (pytest.approx(5.8), 20, "USD")
    assert agent.resets_at.isoformat() == "2026-10-21T00:00:00+00:00"
    assert agent.window_seconds == 30 * 86400
    assert orb.used_percent == pytest.approx(31.6667, abs=1e-3)
    assert (orb.unit, orb.detail) == ("hours", "512h left")
    assert (credits.id, credits.used, credits.unit) == ("credits", 20, "USD")
    assert credits.used_percent is None


def test_free_daily_and_legacy_subscription_with_workspace():
    windows, plan, account = parse_usage_text(
        load_text("amp", "usage_free_subscription.txt"), NOW)
    assert (plan, account) == ("Gigawatt", "fixture@example.test")
    assert [w.id for w in windows] == ["other", "orb", "free", "credits", "workspace_meow"]
    other, orb, free, credits, workspace = windows
    assert (other.used_percent, orb.used_percent) == (27, 9)
    assert other.resets_at.isoformat() == "2026-11-03T12:00:00+00:00"  # "in 1 month"
    assert free.used_percent == 53
    assert free.resets_at.isoformat() == "2026-10-04T00:00:00+00:00"  # 20:00 in New York
    assert credits.used == pytest.approx(17.23)
    assert (workspace.label, workspace.detail) == ("meow", "Workspace balance")
    assert workspace.used == pytest.approx(5.33)


def test_markdown_bold_and_ansi_colours_are_ignored():
    text = load_text("amp", "usage_markdown.txt").replace(
        "**Amp Free:**", "\x1b[1m**Amp Free:**\x1b[22m").replace("0%", "\x1b[31m0%\x1b[39m")
    windows, plan, account = parse_usage_text(text, NOW)
    assert (plan, account) == ("Megawatt", "you@example.com")
    by_id = {w.id: w for w in windows}
    assert (by_id["other"].used_percent, by_id["orb"].used_percent) == (32, 3)
    assert by_id["other"].resets_at == NOW + timedelta(days=5)
    assert by_id["free"].used_percent == 100
    assert by_id["credits"].used == pytest.approx(3.23)


def test_legacy_dollar_free_tier_refills_hourly():
    windows, plan, _account = parse_usage_text(load_text("amp", "usage_legacy_free.txt"), NOW)
    free, credits, test_team, alpha = windows
    assert plan == "Amp Free"
    assert free.used_percent == 40
    assert (free.used, free.limit, free.unit) == (4, 10, "USD")
    assert free.window_seconds == 20 * 3600
    assert free.resets_at == NOW + timedelta(hours=8)  # $4 back at $0.50/hour
    assert free.detail == "Refills $0.50/hour"
    assert credits.used == 12.5
    assert (test_team.id, test_team.label) == ("workspace_test_team", "Test Team")
    assert (alpha.label, alpha.used) == ("Alpha Team", pytest.approx(1234.56))


def test_tier_without_orb_hours_and_zero_dollar_tier():
    windows, plan, account = parse_usage_text(load_text("amp", "usage_tier_no_orb.txt"), NOW)
    assert (plan, account) == ("Example", None)
    assert [w.id for w in windows] == ["agent", "credits"]
    assert windows[0].used_percent == 85
    assert windows[0].resets_at == NOW + timedelta(days=2)
    assert windows[0].window_seconds is None
    zero = ("Amp Example Tier: agent usage $0 of $0 remaining - resets upon renewal in 2 days\n"
            "Individual credits: $11 remaining\n")
    windows, _plan, _account = parse_usage_text(zero, NOW)
    assert [w.id for w in windows] == ["credits"]


def test_free_reset_is_eight_pm_new_york_time():
    summer = next_free_reset(datetime(2026, 7, 1, 23, 59, tzinfo=timezone.utc))
    assert summer.isoformat() == "2026-07-02T00:00:00+00:00"
    winter = next_free_reset(datetime(2026, 12, 1, 1, 0, tzinfo=timezone.utc))
    assert winter.isoformat() == "2026-12-02T01:00:00+00:00"


def test_fetch_runs_amp_usage(home):
    amp = make_cli(home)
    runner = FakeRunner({"amp": (0, load_text("amp", "usage_tier.txt"), "")})
    snap = AmpProvider().fetch(make_ctx(home, runner=runner))
    assert runner.calls == [[str(amp), "usage"]]
    assert (snap.plan, snap.account, snap.source) == ("Megawatt", "dev@example.com", "amp CLI")


def test_signed_out_cli_is_not_retried_with_its_own_key(home):
    make_cli(home)
    write(home / ".local/share/amp/secrets.json", load_text("amp", "secrets.json"))
    runner = FakeRunner({"amp": (1, "", load_text("amp", "not_logged_in.txt"))})
    http = FakeHttp()
    with pytest.raises(AuthError) as info:
        AmpProvider().fetch(make_ctx(home, http=http, runner=runner))
    assert "amp login" in info.value.hint
    assert http.calls == []


def test_pasted_token_is_used_when_cli_is_signed_out(home):
    make_cli(home)
    runner = FakeRunner({"amp": (1, "", load_text("amp", "not_logged_in.txt"))})
    http = FakeHttp({("POST", BALANCE_URL): load_json("amp", "balance_ok.json")})
    ctx = make_ctx(home, http=http, runner=runner, secrets={("amp", "api_key"): "sgamp_pasted"})
    snap = AmpProvider().fetch(ctx)
    call = http.last(BALANCE_URL)
    assert call["headers"]["Authorization"] == "Bearer sgamp_pasted"
    assert call["json"] == {"method": "userDisplayBalanceInfo", "params": {}}
    assert snap.source == "Amp API"
    assert [w.id for w in snap.windows] == ["free", "credits", "workspace_example"]
    assert snap.windows[0].used_percent == 18


def test_without_cli_a_rejected_token_falls_through(home):
    write(home / ".local/share/amp/secrets.json", load_text("amp", "secrets.json"))
    seen = []

    def respond(method, url, headers, body):
        seen.append(headers["Authorization"])
        if headers["Authorization"] == f"Bearer {LEGACY_KEY}":
            return http_error(401, {"error": "unauthorized"})
        return load_json("amp", "balance_ok.json")

    http = FakeHttp({("POST", BALANCE_URL): respond})
    snap = AmpProvider().fetch(make_ctx(home, http=http, env={"AMP_API_KEY": "sgamp_env"}))
    assert seen == [f"Bearer {LEGACY_KEY}", "Bearer sgamp_env"]
    assert (snap.plan, snap.account) == ("Amp Free", "dev@example.com")


def test_balance_api_error_envelopes():
    with pytest.raises(AuthError):
        parse_balance_response(load_json("amp", "balance_auth_required.json"))
    with pytest.raises(ProviderError, match="quota service down"):
        parse_balance_response({"ok": False,
                                "error": {"code": "x", "message": "quota service down"}})
    with pytest.raises(ProviderError):
        parse_balance_response({"ok": True, "result": {"displayText": "  "}})
    with pytest.raises(ProviderError):
        parse_usage_text("Signed in as a@b.c\nSomething else entirely\n", NOW)


def test_nothing_configured(home):
    provider = AmpProvider()
    ctx = make_ctx(home)
    assert provider.detect(ctx) is False
    with pytest.raises(NotConfigured):
        provider.fetch(ctx)
