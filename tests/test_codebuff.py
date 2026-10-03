import json

import pytest

from conftest import FakeHttp, http_error, load_json, load_text, make_ctx, write
from quotaglance.providers.base import AuthError, NotConfigured, ProviderError
from quotaglance.providers.codebuff import (
    CodebuffProvider,
    base_url,
    parse_subscription,
    parse_usage,
)

USAGE_URL = "https://www.codebuff.com/api/v1/usage"
SUBSCRIPTION_URL = "https://www.codebuff.com/api/user/subscription"
SESSION = "cb_fixture_session_9f8e7d6c5b4a39281706f5e4d3c2b1a0"
CREDENTIALS = ".config/manicode/credentials.json"


def test_usage_counts_credits_against_everything_available():
    credits = parse_usage(load_json("codebuff", "usage.json"))
    assert (credits.id, credits.label, credits.used_percent) == ("credits", "Credits", 25)
    assert (credits.used, credits.limit, credits.unit) == (1250, 5000, "credits")
    assert credits.resets_at.isoformat() == "2026-11-01T00:00:00+00:00"
    assert credits.detail == "3,750 credits left · auto top-up"


def test_usage_variants():
    strings = parse_usage({"usage": "12", "quota": "100", "remainingBalance": "88"})
    assert (strings.used, strings.limit, strings.used_percent) == (12, 100, 12)
    empty = parse_usage({"type": "usage-response", "usage": 5000, "remainingBalance": 0,
                         "next_quota_reset": "2026-11-01T00:00:00.000Z",
                         "autoTopupEnabled": False})
    assert (empty.used_percent, empty.detail) == (100, "0 credits left")
    unknown = parse_usage({"type": "usage-response", "usage": 20, "remainingBalance": 480,
                           "next_quota_reset": None})
    assert (unknown.used_percent, unknown.resets_at) == (4, None)
    assert parse_usage({}) is None


def test_subscription_five_hour_block_and_weekly_limit():
    (block, weekly), plan, email = parse_subscription(
        load_json("codebuff", "subscription_active.json"))
    assert (plan, email) == ("Pro", None)
    assert (block.id, block.label, block.used_percent) == ("session", "5-hour", 45)
    assert block.resets_at.isoformat() == "2026-10-03T11:42:00+00:00"
    assert (block.window_seconds, weekly.window_seconds) == (5 * 3600, 7 * 86400)
    assert (weekly.label, weekly.used_percent, weekly.used, weekly.limit) == ("Weekly", 30, 2100,
                                                                              7000)
    assert weekly.resets_at.isoformat() == "2026-10-06T00:00:00+00:00"


def test_limited_week_and_older_shapes():
    limited = {"hasSubscription": True, "displayName": "Pro", "rateLimit": {
        "limited": True, "reason": "weekly_limit", "canStartNewBlock": False,
        "weeklyUsed": 7000, "weeklyLimit": 7000, "weeklyResetsAt": "2026-10-06T00:00:00.000Z",
        "weeklyPercentUsed": 100}}
    (weekly,), _plan, _email = parse_subscription(limited)  # no block before the next request
    assert (weekly.id, weekly.used_percent, weekly.detail) == ("weekly", 100, "Limit reached")
    older = {"subscription": {"status": "active", "tier": "pro",
                              "billingPeriodEnd": "2026-10-21T00:00:00Z"},
             "rateLimit": {"weeklyUsed": 2100, "weeklyLimit": 7000,
                           "weeklyResetsAt": "2026-10-06T00:00:00Z"},
             "email": "user@example.com"}
    windows, plan, email = parse_subscription(older)
    assert (plan, email, windows[0].used_percent) == ("Pro", "user@example.com", 30)
    assert parse_subscription({"subscription": {"tier": 2, "displayName": "Pro"}})[1] == "Pro"
    assert parse_subscription({"subscription": {"scheduledTier": 3}})[1] == "3"
    assert parse_subscription(load_json("codebuff", "subscription_none.json")) == ([], "Free",
                                                                                   None)


def test_cli_session_reads_usage_and_subscription(home):
    write(home / CREDENTIALS, load_text("codebuff", "credentials.json"))
    http = FakeHttp({("POST", USAGE_URL): load_json("codebuff", "usage.json"),
                     ("GET", SUBSCRIPTION_URL): load_json("codebuff", "subscription_active.json")})
    snap = CodebuffProvider().fetch(make_ctx(home, http=http))
    usage = http.last(USAGE_URL)
    assert usage["headers"]["Authorization"] == f"Bearer {SESSION}"
    assert usage["json"] == {"fingerprintId": "quotaglance-usage", "authToken": SESSION}
    assert http.last(SUBSCRIPTION_URL)["headers"]["Cookie"] == f"next-auth.session-token={SESSION};"
    assert [w.id for w in snap.windows] == ["session", "weekly", "credits"]
    assert (snap.plan, snap.account, snap.source) == ("Pro", "dev@example.com",
                                                      "Codebuff CLI sign-in")


def test_api_key_reads_credits_only(home):
    http = FakeHttp({("POST", USAGE_URL): load_json("codebuff", "usage.json")})
    snap = CodebuffProvider().fetch(make_ctx(home, http=http, env={"CODEBUFF_API_KEY": "cb_key"}))
    assert [call["url"] for call in http.calls] == [USAGE_URL]  # API keys can't read the plan
    assert [w.id for w in snap.windows] == ["credits"]
    assert (snap.plan, snap.account, snap.source) == (None, None, "$CODEBUFF_API_KEY")


def test_failed_subscription_call_keeps_the_credits(home):
    write(home / CREDENTIALS, load_text("codebuff", "credentials.json"))
    http = FakeHttp({("POST", USAGE_URL): load_json("codebuff", "usage.json"),
                     ("GET", SUBSCRIPTION_URL): http_error(500, b"oops")})
    snap = CodebuffProvider().fetch(make_ctx(home, http=http))
    assert [w.id for w in snap.windows] == ["credits"]
    http.add("GET", SUBSCRIPTION_URL, load_json("codebuff", "subscription_none.json"))
    assert CodebuffProvider().fetch(make_ctx(home, http=http)).plan == "Pay as you go"


def test_keychain_stored_sign_in_asks_for_an_api_key(home):
    write(home / CREDENTIALS, json.dumps({"default": {
        "id": "u1", "email": "dev@example.com", "name": None, "fingerprintId": "fp",
        "tokenStore": "keychain"}}))
    provider = CodebuffProvider()
    ctx = make_ctx(home)
    assert provider.detect(ctx)
    with pytest.raises(NotConfigured) as info:
        provider.fetch(ctx)
    assert "API key" in info.value.hint


def test_revoked_session_is_auth_error(home):
    write(home / CREDENTIALS, load_text("codebuff", "credentials.json"))
    http = FakeHttp({("POST", USAGE_URL): http_error(401, {"error": "Invalid auth token"})})
    with pytest.raises(AuthError) as info:
        CodebuffProvider().fetch(make_ctx(home, http=http))
    assert "codebuff login" in info.value.hint


def test_endpoint_override_must_be_https(home):
    bare = make_ctx(home, env={"CODEBUFF_API_URL": "staging.codebuff.com"})
    assert base_url(bare) == "https://staging.codebuff.com"
    assert base_url(make_ctx(home, env={"CODEBUFF_API_URL": "https://proxy.example/"})) == \
        "https://proxy.example"
    with pytest.raises(ProviderError):
        base_url(make_ctx(home, env={"CODEBUFF_API_URL": "http://insecure.example"}))


def test_not_configured(home):
    provider = CodebuffProvider()
    ctx = make_ctx(home)
    assert provider.detect(ctx) is False
    with pytest.raises(NotConfigured):
        provider.fetch(ctx)
