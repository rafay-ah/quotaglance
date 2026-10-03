import json
import sqlite3

import pytest

from conftest import NOW, FakeHttp, FakeRunner, load_json, load_text, make_ctx, write
from quotaglance.providers.base import AuthError, NotConfigured, ProviderError
from quotaglance.providers.kiro import (
    KiroProvider,
    build_windows,
    parse_usage_limits,
    parse_usage_output,
    plan_display,
)

ENDPOINT = "https://codewhisperer.us-east-1.amazonaws.com/"
ARN = "arn:aws:codewhisperer:us-east-1:123456789012:profile/FIXTUREPROF1"


def install_cli(home, usage_text, whoami="Logged in with Google\nEmail: person@example.com\n"):
    cli = write(home / ".local/bin/kiro-cli", "#!/bin/sh\n")
    cli.chmod(0o755)

    class Runner(FakeRunner):
        def __call__(self, argv, **kwargs):
            self.calls.append(list(argv))
            import subprocess

            if argv[1:] == ["whoami"]:
                return subprocess.CompletedProcess(argv, 0, whoami, "")
            return subprocess.CompletedProcess(argv, 0, usage_text, "")

    return Runner()


def make_db(home, expires="2026-10-03T13:00:00Z"):
    path = home / ".local/share/kiro-cli/data.sqlite3"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE auth_kv (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE state (key TEXT PRIMARY KEY, value BLOB)")
    conn.execute("INSERT INTO auth_kv VALUES (?, ?)", ("kirocli:odic:token", json.dumps({
        "access_token": "aoaFIXTURE", "expires_at": expires, "refresh_token": "aorFIXTURE",
        "region": "us-east-1"})))
    conn.execute("INSERT INTO state VALUES (?, ?)", ("api.codewhisperer.profile",
                                                     json.dumps({"arn": ARN}).encode()))
    conn.commit()
    conn.close()


def test_free_plan_with_bonus_and_ansi_bar():
    result = parse_usage_output(load_text("kiro", "usage_free_bonus.txt"), NOW)
    windows, plan = build_windows(result, None, NOW)
    assert plan == "Kiro Free"
    credits, bonus = windows
    assert credits.used_percent == pytest.approx(35.0)
    assert (credits.used, credits.limit) == (17.5, 50)
    assert credits.resets_at.isoformat() == "2026-11-01T00:00:00+00:00"
    assert bonus.used_percent == pytest.approx(62.48)
    assert bonus.detail == "Expires in 11 days"


def test_pro_plus_with_cli_overage_rows():
    result = parse_usage_output(load_text("kiro", "usage_pro_overage.txt"), NOW)
    windows, plan = build_windows(result, None, NOW)
    assert plan == "Kiro Pro+"
    assert windows[0].used_percent == 100
    overage = windows[1]
    assert overage.id == "overage" and overage.used == 52.75
    assert overage.detail == "52.75 credits · $2.11"


def test_summary_and_managed_forms_have_plan_only():
    summary = parse_usage_output(load_text("kiro", "usage_summary.txt"), NOW)
    assert build_windows(summary, None, NOW) == ([], "Kiro Pro Max")
    managed = parse_usage_output(load_text("kiro", "usage_managed.txt"), NOW)
    windows, plan = build_windows(managed, None, NOW)
    assert plan == "Q Developer Pro"
    assert windows[0].detail == "Managed by your organization"


def test_legacy_box_format_with_month_day_reset():
    result = parse_usage_output(load_text("kiro", "usage_legacy_box.txt"), NOW)
    windows, plan = build_windows(result, None, NOW)
    assert plan == "Kiro Free"
    assert windows[0].used_percent == pytest.approx(24.0)
    assert windows[0].resets_at.isoformat() == "2026-11-01T00:00:00+00:00"
    assert windows[1].used == 40


def test_not_logged_in_and_unknown_format():
    with pytest.raises(AuthError):
        parse_usage_output(load_text("kiro", "not_logged_in.txt"), NOW)
    with pytest.raises(ProviderError):
        parse_usage_output("Something completely different", NOW)


def test_api_splits_plan_and_overage():
    api = parse_usage_limits(load_json("kiro", "limits_power.json"))
    windows, plan = build_windows(None, api, NOW)
    assert plan == "Kiro Power"
    credits, overage = windows
    assert credits.used_percent == pytest.approx(100.0)
    assert credits.resets_at.isoformat() == "2026-11-01T00:00:00+00:00"
    assert overage.used_percent == pytest.approx(24.8037)
    assert overage.detail == "$99.21 so far"


def test_api_rejects_inconsistent_rows():
    data = load_json("kiro", "limits_free.json")
    data["usageBreakdownList"].append(dict(data["usageBreakdownList"][0]))
    with pytest.raises(ProviderError):
        parse_usage_limits(data)


def test_fetch_cli_then_enriches_from_db_token(home):
    runner = install_cli(home, load_text("kiro", "usage_free_bonus.txt"))
    make_db(home)
    http = FakeHttp({("POST", ENDPOINT): load_json("kiro", "limits_free.json")})
    snap = KiroProvider().fetch(make_ctx(home, http=http, runner=runner))
    call = http.last(ENDPOINT)
    assert call["headers"]["X-Amz-Target"] == "AmazonCodeWhispererService.GetUsageLimits"
    assert call["headers"]["Authorization"] == "Bearer aoaFIXTURE"
    assert call["json"] == {"profileArn": ARN}
    assert runner.calls[0][1:] == ["chat", "--no-interactive", "/usage"]
    assert snap.account == "person@example.com"
    assert snap.source == "kiro-cli + Kiro API"
    assert snap.windows[0].used_percent == pytest.approx(35.0)


def test_expired_db_token_skips_enrichment(home):
    runner = install_cli(home, load_text("kiro", "usage_pro_overage.txt"))
    make_db(home, expires="2026-10-03T11:00:00Z")
    snap = KiroProvider().fetch(make_ctx(home, runner=runner))  # FakeHttp would raise if used
    assert snap.source == "kiro-cli"


def test_ide_token_fallback_without_cli(home):
    write(home / ".aws/sso/cache/kiro-auth-token.json", json.dumps({
        "accessToken": "aoaIDE", "expiresAt": "2026-10-03T12:30:00Z", "authMethod": "social",
        "provider": "Google", "profileArn": ARN}))
    http = FakeHttp({("POST", ENDPOINT): load_json("kiro", "limits_power.json")})
    snap = KiroProvider().fetch(make_ctx(home, http=http))
    assert snap.source == "Kiro IDE sign-in"
    assert snap.plan == "Kiro Power"


def test_expired_ide_token(home):
    write(home / ".aws/sso/cache/kiro-auth-token.json", json.dumps({
        "accessToken": "aoaIDE", "expiresAt": "2026-10-03T10:00:00Z", "profileArn": ARN}))
    with pytest.raises(AuthError, match="expired"):
        KiroProvider().fetch(make_ctx(home))


def test_untrusted_region_is_never_contacted(home):
    write(home / ".aws/sso/cache/kiro-auth-token.json", json.dumps({
        "accessToken": "aoaIDE", "expiresAt": "2026-10-03T12:30:00Z",
        "profileArn": "arn:aws:codewhisperer:ap-south-9:1:profile/X"}))
    with pytest.raises(NotConfigured):
        KiroProvider().fetch(make_ctx(home))


def test_plan_display():
    assert plan_display("KIRO PRO+") == "Kiro Pro+"
    assert plan_display("Q Developer Pro") == "Q Developer Pro"
    assert plan_display(None) is None


def test_nothing_installed(home):
    provider = KiroProvider()
    assert not provider.detect(make_ctx(home))
    with pytest.raises(NotConfigured):
        provider.fetch(make_ctx(home))
