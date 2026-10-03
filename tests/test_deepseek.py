import json

import pytest

from conftest import FakeHttp, http_error, load_json, make_ctx, write
from quotaglance.providers.base import AuthError, NotConfigured, ProviderError
from quotaglance.providers.deepseek import BALANCE_URL, DeepSeekProvider, find_key, parse_balance


def test_usd_balance_with_granted_credit():
    window = parse_balance(load_json("deepseek", "usd.json"))
    assert (window.id, window.label, window.used_percent) == ("balance", "Balance", None)
    assert (window.used, window.unit) == (pytest.approx(18.47), "USD")
    assert window.detail == "$18.47 left · $2.00 granted"


def test_cny_balance():
    window = parse_balance(load_json("deepseek", "cny.json"))
    assert (window.used, window.unit) == (pytest.approx(236.9), "CNY")
    assert window.detail == "¥236.90 left · ¥10.00 granted"


def test_currency_selection_prefers_funded_usd():
    window = parse_balance(load_json("deepseek", "mixed_usd_funded.json"))
    assert (window.used, window.unit) == (pytest.approx(20.4), "USD")
    assert window.detail.endswith("also ¥100.00")
    window = parse_balance(load_json("deepseek", "mixed_usd_empty.json"))
    assert (window.used, window.unit, window.detail) == (pytest.approx(88.12), "CNY", "¥88.12 left")


def test_empty_or_blocked_balance_is_critical():
    empty = parse_balance(load_json("deepseek", "empty.json"))
    assert (empty.used_percent, empty.used, empty.detail) == (100, 0, "$0.00 left · add credits")
    blocked = parse_balance(load_json("deepseek", "unavailable.json"))
    assert (blocked.used_percent, blocked.used) == (100, 5)
    assert "not usable" in blocked.detail
    none = parse_balance({"is_available": False, "balance_infos": []})
    assert (none.used, none.unit, none.used_percent) == (0, "USD", 100)


def test_non_numeric_balance_is_an_error():
    with pytest.raises(ProviderError, match="non-numeric"):
        parse_balance({"is_available": True,
                       "balance_infos": [{"currency": "USD", "total_balance": "N/A"}]})
    with pytest.raises(ProviderError):
        parse_balance([])


def test_fetch_sends_bearer_key(home):
    http = FakeHttp({("GET", BALANCE_URL): load_json("deepseek", "usd.json")})
    ctx = make_ctx(home, http=http, secrets={("deepseek", "api_key"): "sk-6f1d"})
    snap = DeepSeekProvider().fetch(ctx)
    assert http.last(BALANCE_URL)["headers"]["Authorization"] == "Bearer sk-6f1d"
    assert (snap.plan, snap.source) == ("Pay as you go", "Keyring")


def test_key_sources(home):
    assert find_key(make_ctx(home, env={"DEEPSEEK_KEY": "sk-env"})) == ("sk-env", "$DEEPSEEK_KEY")
    write(home / ".claude/settings.json", json.dumps({"env": {
        "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
        "ANTHROPIC_AUTH_TOKEN": "sk-claude"}}))
    assert find_key(make_ctx(home)) == ("sk-claude", "Claude Code settings")
    write(home / ".local/share/opencode/auth.json",
          json.dumps({"deepseek": {"type": "api", "key": "sk-oc"}}))
    assert find_key(make_ctx(home)) == ("sk-oc", "OpenCode sign-in")
    assert DeepSeekProvider().detect(make_ctx(home))


def test_rejected_key_and_server_trouble(home):
    body = {"error": {"message": "Authentication Fails, Your api key: ****cd7f is invalid",
                      "type": "authentication_error", "param": None,
                      "code": "invalid_request_error"}}
    http = FakeHttp({("GET", BALANCE_URL): http_error(401, body)})
    with pytest.raises(AuthError):
        DeepSeekProvider().fetch(make_ctx(home, http=http, env={"DEEPSEEK_API_KEY": "sk"}))
    http = FakeHttp({("GET", BALANCE_URL): http_error(503)})
    with pytest.raises(ProviderError) as exc:
        DeepSeekProvider().fetch(make_ctx(home, http=http, env={"DEEPSEEK_API_KEY": "sk"}))
    assert exc.value.transient


def test_not_configured(home):
    assert not DeepSeekProvider().detect(make_ctx(home))
    with pytest.raises(NotConfigured):
        DeepSeekProvider().fetch(make_ctx(home))
