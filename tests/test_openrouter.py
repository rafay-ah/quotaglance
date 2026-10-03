import json

import pytest

from conftest import NOW, FakeHttp, http_error, load_json, make_ctx, write
from quotaglance.providers.base import AuthError, NotConfigured, ProviderError
from quotaglance.providers.openrouter import (
    OpenRouterProvider,
    find_key,
    parse_credits,
    parse_key,
)

KEY_URL = "https://openrouter.ai/api/v1/key"
CREDITS_URL = "https://openrouter.ai/api/v1/credits"


def test_monthly_capped_key_and_free_model_requests():
    windows, plan = parse_key(load_json("openrouter", "key_capped_monthly.json"), NOW)
    cap, free = windows
    assert plan == "Pay as you go"
    assert (cap.id, cap.label) == ("key_limit", "Monthly cap")
    assert cap.used_percent == pytest.approx(38.73)
    assert (cap.used, cap.limit, cap.unit) == (pytest.approx(38.73), 100, "USD")
    assert cap.resets_at.isoformat() == "2026-11-01T00:00:00+00:00"
    assert cap.detail == "$61.27 left"
    assert (free.id, free.used_percent) == ("free_requests", pytest.approx(3.8))
    assert free.resets_at.isoformat() == "2026-10-04T00:00:00+00:00"


def test_uncapped_key_shows_this_months_spend():
    windows, _plan = parse_key(load_json("openrouter", "key_uncapped.json"), NOW)
    spend = windows[0]
    assert (spend.id, spend.label, spend.used_percent) == ("spend", "This month", None)
    assert spend.used == pytest.approx(11.06)
    assert spend.detail == "$11.06 · no limit set"
    assert spend.resets_at.isoformat() == "2026-11-01T00:00:00+00:00"


def test_free_tier_daily_cap_clamps_negative_remaining():
    windows, plan = parse_key(load_json("openrouter", "key_free_tier.json"), NOW)
    assert plan == "Free tier"
    assert [(w.label, w.used_percent) for w in windows] == [("Daily cap", 100),
                                                            ("Free models", 100)]
    assert windows[0].resets_at.isoformat() == "2026-10-04T00:00:00+00:00"


def test_weekly_cap_without_remaining_uses_the_weeks_spend():
    payload = {"data": {"limit": 20, "limit_remaining": None, "limit_reset": "weekly",
                        "usage": 80, "usage_weekly": 5}}
    cap = parse_key(payload, NOW)[0][0]
    assert cap.used_percent == pytest.approx(25)
    assert cap.resets_at.isoformat() == "2026-10-05T00:00:00+00:00"  # next Monday


def test_management_key_and_malformed_numbers():
    windows, plan = parse_key(load_json("openrouter", "key_management.json"), NOW)
    assert windows == [] and plan == "Pay as you go · management key"
    with pytest.raises(ProviderError):
        parse_key({"data": {"limit": "100"}}, NOW)
    with pytest.raises(ProviderError):
        parse_credits({"data": {"total_credits": "150", "total_usage": 1}})


def test_credits_meter_and_balance():
    credits = parse_credits(load_json("openrouter", "credits.json"))
    assert credits.used_percent == pytest.approx(65.09, abs=0.01)
    assert (credits.used, credits.limit) == (pytest.approx(97.6421), 150)
    assert credits.detail == "$52.36 left"


def test_fetch_sends_bearer_and_attribution_headers(home):
    http = FakeHttp({("GET", KEY_URL): load_json("openrouter", "key_capped_monthly.json"),
                     ("GET", CREDITS_URL): load_json("openrouter", "credits.json")})
    ctx = make_ctx(home, http=http, secrets={("openrouter", "api_key"): "sk-or-v1-test"})
    snap = OpenRouterProvider().fetch(ctx)
    for url in (KEY_URL, CREDITS_URL):
        headers = http.last(url)["headers"]
        assert headers["Authorization"] == "Bearer sk-or-v1-test"
        assert headers["X-Title"] == "QuotaGlance"
    assert [w.id for w in snap.windows] == ["key_limit", "credits", "free_requests"]
    assert (snap.plan, snap.source) == ("Pay as you go", "Keyring")


def test_forbidden_credits_keep_the_key_numbers(home):
    body = {"error": {"code": 403, "message": "Only management keys can access this endpoint"}}
    http = FakeHttp({("GET", KEY_URL): load_json("openrouter", "key_uncapped.json"),
                     ("GET", CREDITS_URL): http_error(403, body)})
    snap = OpenRouterProvider().fetch(make_ctx(home, http=http,
                                               env={"OPENROUTER_API_KEY": "sk-or"}))
    assert [w.id for w in snap.windows] == ["spend", "free_requests"]
    assert "management key" in snap.message
    assert snap.source == "$OPENROUTER_API_KEY"


def test_rejected_key_is_auth_error(home):
    body = {"error": {"code": 401, "message": "User not found."}}
    http = FakeHttp({("GET", KEY_URL): http_error(401, body)})
    with pytest.raises(AuthError):
        OpenRouterProvider().fetch(make_ctx(home, http=http, env={"OPENROUTER_API_KEY": "sk"}))


def test_key_from_opencode_auth_and_not_configured(home):
    assert not OpenRouterProvider().detect(make_ctx(home))
    with pytest.raises(NotConfigured):
        OpenRouterProvider().fetch(make_ctx(home))
    write(home / ".local/share/opencode/auth.json",
          json.dumps({"openrouter": {"type": "api", "key": "sk-or-v1-oc"}}))
    assert find_key(make_ctx(home)) == ("sk-or-v1-oc", "OpenCode sign-in")
    assert OpenRouterProvider().detect(make_ctx(home))
