import json

import pytest

from conftest import FakeHttp, http_error, load_json, make_ctx, write
from quotaglance.providers.base import AuthError, NotConfigured, ProviderError
from quotaglance.providers.vercel import CREDITS_URL, VercelProvider, find_key, parse_credits


def test_balance_and_lifetime_spend():
    window = parse_credits(load_json("vercel", "credits.json"))
    assert (window.id, window.label, window.used_percent) == ("balance", "Credits", None)
    assert (window.used, window.unit) == (95.5, "USD")
    assert window.detail == "$95.50 left · $4.50 spent"


def test_empty_and_negative_balances_are_critical():
    empty = parse_credits(load_json("vercel", "exhausted.json"))
    assert (empty.used, empty.used_percent) == (0, 100)
    negative = parse_credits(load_json("vercel", "negative.json"))
    assert (negative.used, negative.used_percent) == (-1.24, 100)
    assert negative.detail == "−$1.24 · $51.24 spent"


def test_malformed_payloads_are_rejected_not_shown_as_zero():
    for payload in ({"balance": 95.5, "total_used": "4.50"}, {"balance": "95.50"},
                    {"balance": "N/A", "total_used": "0"}, {"balance": "1", "total_used": "-2"},
                    []):
        with pytest.raises(ProviderError, match="unrecognized"):
            parse_credits(payload)


def test_fetch_sends_bearer_key(home):
    http = FakeHttp({("GET", CREDITS_URL): load_json("vercel", "credits.json")})
    ctx = make_ctx(home, http=http, env={"AI_GATEWAY_API_KEY": "vck_test"})
    snap = VercelProvider().fetch(ctx)
    assert http.last(CREDITS_URL)["headers"]["Authorization"] == "Bearer vck_test"
    assert (snap.plan, snap.source) == ("Team credits", "$AI_GATEWAY_API_KEY")


def test_http_errors(home):
    ctx_env = {"AI_GATEWAY_API_KEY": "vck"}
    for status in (401, 403):
        http = FakeHttp({("GET", CREDITS_URL): http_error(status)})
        with pytest.raises(AuthError):
            VercelProvider().fetch(make_ctx(home, http=http, env=ctx_env))
    http = FakeHttp({("GET", CREDITS_URL): http_error(429, headers={"Retry-After": "7"})})
    with pytest.raises(ProviderError) as exc:
        VercelProvider().fetch(make_ctx(home, http=http, env=ctx_env))
    assert exc.value.transient and exc.value.retry_after == 7


def test_key_from_keyring_or_opencode_and_not_configured(home):
    assert not VercelProvider().detect(make_ctx(home))
    with pytest.raises(NotConfigured):
        VercelProvider().fetch(make_ctx(home))
    write(home / ".local/share/opencode/auth.json",
          json.dumps({"vercel": {"type": "api", "key": "vck_oc"}}))
    assert find_key(make_ctx(home)) == ("vck_oc", "OpenCode sign-in")
    keyring = make_ctx(home, secrets={("vercel", "api_key"): "vck_ring"})
    assert find_key(keyring) == ("vck_ring", "Keyring")
