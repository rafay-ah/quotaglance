import json

import pytest

from conftest import FakeHttp, FakeRunner, http_error, load_json, load_text, make_ctx, write
from quotaglance.providers.base import NotConfigured, ProviderError
from quotaglance.providers.copilot import (
    USER_URL,
    CopilotProvider,
    find_token,
    parse_copilot_user,
    token_from_gh_hosts,
)


def test_pro_plan_premium_pool_and_unlimited_chat():
    windows, plan = parse_copilot_user(load_json("copilot", "user_pro.json"))
    assert plan == "Pro"
    assert [w.id for w in windows] == ["premium"]
    premium = windows[0]
    assert premium.used_percent == pytest.approx(41.0)
    assert premium.used == 123 and premium.limit == 300
    assert premium.resets_at.isoformat() == "2026-11-01T00:00:00+00:00"


def test_free_plan_has_chat_and_completions_but_no_placeholder_premium():
    windows, plan = parse_copilot_user(load_json("copilot", "user_free.json"))
    assert plan == "Free"
    by_id = {w.id: w for w in windows}
    assert set(by_id) == {"chat", "completions"}
    assert by_id["chat"].used_percent == pytest.approx(38.0)
    assert by_id["completions"].used == 11


def test_business_token_billing_shows_credit_counter_only():
    windows, plan = parse_copilot_user(load_json("copilot", "user_business.json"))
    assert plan == "Business"
    assert len(windows) == 1
    assert windows[0].id == "credits"
    assert windows[0].used == 31
    assert windows[0].used_percent is None


def test_legacy_monthly_quotas():
    windows, plan = parse_copilot_user(load_json("copilot", "user_legacy.json"))
    assert plan == "Pro"
    by_id = {w.id: w for w in windows}
    assert by_id["premium"].used_percent == pytest.approx(98.5)
    assert by_id["chat"].used_percent == pytest.approx(75.0)
    assert by_id["chat"].resets_at.isoformat() == "2026-10-15T00:00:00+00:00"


def test_gh_hosts_yaml_host_level_token():
    assert token_from_gh_hosts(load_text("copilot", "gh_hosts.yml")) == "gho_cliToken0123456789"


def test_token_order_prefers_plugin_file_over_gh(home):
    write(home / ".config/github-copilot/apps.json", json.dumps({
        "ghe.corp.example:Iv1.x": {"oauth_token": "gho_enterprise"},
        "github.com:Iv1.b507a08c87ecfe98": {"user": "octocat", "oauth_token": "ghu_plugin"},
    }))
    write(home / ".config/gh/hosts.yml", load_text("copilot", "gh_hosts.yml"))
    token, origin = find_token(make_ctx(home))
    assert token == "ghu_plugin"
    assert origin == "Copilot plugin sign-in"


def test_token_from_gh_cli_when_no_files(home):
    gh = write(home / ".local/bin/gh", "#!/bin/sh\n")
    gh.chmod(0o755)
    runner = FakeRunner({"gh": (0, "gho_fromCli\n", "")})
    token, _origin = find_token(make_ctx(home, runner=runner))
    assert token == "gho_fromCli"
    assert runner.calls[0][1:] == ["auth", "token", "--hostname", "github.com"]


def test_keyring_token_wins(home):
    write(home / ".config/github-copilot/apps.json",
          json.dumps({"github.com": {"oauth_token": "gho_file"}}))
    ctx = make_ctx(home, secrets={("copilot", "token"): "gho_keyring"})
    assert find_token(ctx)[0] == "gho_keyring"


def test_fetch_sends_token_scheme_and_editor_headers(home):
    write(home / ".config/github-copilot/hosts.json",
          json.dumps({"github.com": {"oauth_token": "gho_file"}}))
    http = FakeHttp({("GET", USER_URL): load_json("copilot", "user_pro.json")})
    snap = CopilotProvider().fetch(make_ctx(home, http=http))
    headers = http.last(USER_URL)["headers"]
    assert headers["Authorization"] == "token gho_file"
    assert headers["Editor-Version"].startswith("vscode/")
    assert snap.account == "octocat"
    assert snap.plan == "Pro"


def test_no_subscription_404(home):
    write(home / ".config/github-copilot/hosts.json",
          json.dumps({"github.com": {"oauth_token": "gho_file"}}))
    http = FakeHttp({("GET", USER_URL): http_error(404, {"message": "Not Found"})})
    with pytest.raises(ProviderError, match="no Copilot subscription"):
        CopilotProvider().fetch(make_ctx(home, http=http))


def test_nothing_found(home):
    with pytest.raises(NotConfigured):
        CopilotProvider().fetch(make_ctx(home))
