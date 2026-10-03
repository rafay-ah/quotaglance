import json

import pytest

from conftest import FakeHttp, http_error, load_json, make_ctx, write
from quotaglance.providers.base import AuthError, NotConfigured, ProviderError
from quotaglance.providers.moonshot import MoonshotProvider, find_key, parse_balance

INTL_URL = "https://api.moonshot.ai/v1/users/me/balance"
CN_URL = "https://api.moonshot.cn/v1/users/me/balance"


def test_international_balance_with_voucher():
    window = parse_balance(load_json("moonshot", "international.json"), "USD")
    assert (window.id, window.label, window.used_percent) == ("balance", "Balance", None)
    assert (window.used, window.unit) == (pytest.approx(73.41286), "USD")
    assert window.detail == "$73.41 left · $20.00 voucher"


def test_china_balance_in_deficit():
    window = parse_balance(load_json("moonshot", "china_deficit.json"), "CNY")
    assert window.unit == "CNY"
    assert window.detail == "¥12.50 left · ¥0.58 in deficit"


def test_empty_or_negative_balance_is_critical():
    window = parse_balance(load_json("moonshot", "negative.json"), "USD")
    assert window.used_percent == 100
    assert window.detail == "−$2.38 · $2.38 in deficit"
    zero = {"code": 0, "scode": "0x0", "status": True,
            "data": {"available_balance": 0, "voucher_balance": 0, "cash_balance": 0}}
    assert parse_balance(zero, "USD").detail == "$0.00 left"


def test_envelope_errors_and_malformed_payloads():
    with pytest.raises(AuthError):
        parse_balance(load_json("moonshot", "envelope_unauthorized.json"), "USD")
    with pytest.raises(ProviderError, match="code 500"):
        parse_balance({"code": 500, "data": {}, "scode": "0x1", "status": False}, "USD")
    bad = load_json("moonshot", "international.json")
    bad["data"]["available_balance"] = "73.41"
    with pytest.raises(ProviderError):
        parse_balance(bad, "USD")


def test_fetch_binds_the_key_to_its_region(home):
    http = FakeHttp({("GET", CN_URL): load_json("moonshot", "china_deficit.json")})
    ctx = make_ctx(home, http=http, secrets={("moonshot", "api_key"): "sk-cn"},
                   settings={"region": "china"})
    snap = MoonshotProvider().fetch(ctx)
    assert http.last(CN_URL)["headers"]["Authorization"] == "Bearer sk-cn"
    assert snap.windows[0].unit == "CNY"
    assert (snap.plan, snap.source) == ("Pay as you go (China)", "Keyring")


def test_env_key_strips_quotes_and_reads_region(home):
    ctx = make_ctx(home, env={"MOONSHOT_API_KEY": '"sk-quoted"', "MOONSHOT_REGION": "china"})
    assert find_key(ctx) == ("sk-quoted", "china", "$MOONSHOT_API_KEY")
    assert find_key(make_ctx(home, env={"MOONSHOT_KEY": "sk-x"}))[1] == "international"


def test_keys_from_local_files(home):
    write(home / ".claude/settings.json", json.dumps({"env": {
        "ANTHROPIC_BASE_URL": "https://api.moonshot.cn/anthropic",
        "ANTHROPIC_AUTH_TOKEN": "sk-claude"}}))
    assert find_key(make_ctx(home)) == ("sk-claude", "china", "Claude Code settings")
    write(home / ".local/share/opencode/auth.json",
          json.dumps({"moonshotai": {"type": "api", "key": "sk-oc"}}))
    assert find_key(make_ctx(home)) == ("sk-oc", "international", "OpenCode sign-in")


def test_rejected_key_and_not_configured(home):
    body = {"error": {"message": "Invalid Authentication", "type": "invalid_authentication_error"}}
    http = FakeHttp({("GET", INTL_URL): http_error(401, body)})
    with pytest.raises(AuthError, match="rejected"):
        MoonshotProvider().fetch(make_ctx(home, http=http, env={"MOONSHOT_API_KEY": "sk"}))
    assert not MoonshotProvider().detect(make_ctx(home))
    with pytest.raises(NotConfigured):
        MoonshotProvider().fetch(make_ctx(home))
