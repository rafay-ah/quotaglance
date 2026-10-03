from datetime import datetime, timedelta, timezone

import pytest

from conftest import NOW, FakeHttp, http_error, load_json, make_ctx, write
from quotaglance.providers.base import AuthError, NotConfigured
from quotaglance.providers.factory import (
    FactoryProvider,
    find_key,
    key_from_dotenv,
    parse_billing_limits,
    parse_legacy_usage,
    plan_label,
    token_percent,
)

ME_URL = "https://api.factory.ai/api/app/auth/me"
APP_ME_URL = "https://app.factory.ai/api/app/auth/me"
LIMITS_URL = "https://api.factory.ai/api/billing/limits"
USAGE_URL = "https://api.factory.ai/api/organization/subscription/usage"
FIXTURE_NOW = datetime(2026, 10, 3, 8, 30, tzinfo=timezone.utc)  # the limits fixture's clock


def test_token_rate_limits_standard_core_and_extra_usage():
    windows = parse_billing_limits(load_json("factory", "billing_limits.json"), FIXTURE_NOW)
    by_id = {w.id: w for w in windows}
    assert list(by_id) == ["session", "weekly", "monthly", "core_session", "core_weekly",
                           "core_monthly", "extra_usage"]
    session = by_id["session"]
    assert (session.label, session.used_percent, session.window_seconds) == ("5-hour", 37, 18000)
    assert session.resets_at.isoformat() == "2026-10-03T11:00:00+00:00"
    assert by_id["weekly"].resets_at.isoformat() == "2026-10-06T00:00:00+00:00"
    assert by_id["monthly"].window_seconds is None
    core = by_id["core_session"]
    assert (core.label, core.used_percent) == ("Core 5-hour", 4)
    assert core.resets_at.isoformat() == "2026-10-03T10:30:00+00:00"
    extra = by_id["extra_usage"]
    assert (extra.used, extra.unit, extra.used_percent) == (25.0, "USD", None)


def test_stale_windows_roll_over_and_counter_only_shape():
    by_id = {w.id: w for w in parse_billing_limits(load_json("factory", "billing_limits.json"),
                                                   NOW)}
    assert (by_id["core_session"].used_percent, by_id["core_session"].resets_at) == (0, None)
    counters = parse_billing_limits(load_json("factory", "billing_limits_counters.json"), NOW)
    assert [w.id for w in counters] == ["session", "weekly", "monthly"]  # no core, no balance
    assert counters[0].resets_at == NOW + timedelta(hours=1)
    assert parse_billing_limits(load_json("factory", "billing_limits_legacy.json"), NOW) is None
    assert parse_billing_limits({"usesTokenRateLimitsBilling": True, "limits": {}}, NOW) is None


def test_legacy_standard_and_premium_pools():
    standard, premium = parse_legacy_usage(load_json("factory", "usage_legacy.json"))
    assert (standard.label, standard.used_percent) == ("Standard", 62.5)
    assert (standard.used, standard.limit, standard.unit) == (12_500_000, 20_000_000, "tokens")
    assert standard.resets_at.isoformat() == "2026-11-01T00:00:00+00:00"
    assert standard.window_seconds == 31 * 86400
    assert premium.used_percent == pytest.approx(15.0)
    (unlimited,) = parse_legacy_usage({"usage": {"standard": {
        "userTokens": 50_000_000, "totalAllowance": 2_000_000_000_000}}})
    assert (unlimited.used_percent, unlimited.limit, unlimited.detail) == (None, None,
                                                                           "Unlimited")


def test_token_percent_edge_cases():
    assert token_percent(72_311_737, 0, 0.361558685) == pytest.approx(36.1558685)
    assert token_percent(5_000, 10_000, 0) == 50  # a zero ratio despite real usage is ignored
    assert token_percent(10, 0, 45.0) == 45  # already a percentage when there's no allowance
    assert token_percent(10, 0, None) == 0
    assert token_percent(50_000_000, 2e12, None) is None


def test_plan_label_combines_tier_plan_and_fallback():
    me = load_json("factory", "auth_me.json")
    assert plan_label(me) == "Factory Team"  # plan "Team" would only repeat the tier
    assert plan_label(me, "droidCore") == "Factory Team - Fallback: droidCore"
    enterprise = {"organization": {"subscription": {
        "factoryTier": "enterprise", "orbSubscription": {"plan": {"name": "Pro"}}}}}
    assert plan_label(enterprise) == "Factory Enterprise - Pro"
    assert plan_label({}) is None


@pytest.mark.parametrize(("text", "expected"), [
    ("FACTORY_API_KEY=fk-plain", "fk-plain"),
    ("export FACTORY_API_KEY='fk-single'", "fk-single"),
    ('# comment\nFACTORY_API_KEY="fk-double"', "fk-double"),
    ("OTHER=1", None),
])
def test_dotenv_vectors(text, expected):
    assert key_from_dotenv(text) == expected


def test_key_sources_in_order(home):
    write(home / ".factory/.env", "FACTORY_API_KEY=fk-file\n")
    assert find_key(make_ctx(home)) == ("fk-file", "~/.factory/.env")
    assert find_key(make_ctx(home, env={"FACTORY_API_KEY": "fk-env"}))[0] == "fk-env"
    ctx = make_ctx(home, env={"FACTORY_API_KEY": "fk-env"},
                   secrets={("factory", "api_key"): "fk-keyring"})
    assert find_key(ctx) == ("fk-keyring", "Keyring")


def test_fetch_with_token_rate_limits(home):
    http = FakeHttp({
        ("GET", ME_URL): load_json("factory", "auth_me.json"),
        ("GET", LIMITS_URL): load_json("factory", "billing_limits.json"),
    })
    ctx = make_ctx(home, http=http, env={"FACTORY_API_KEY": "fk-test"}, now=FIXTURE_NOW)
    snap = FactoryProvider().fetch(ctx)
    headers = http.last(LIMITS_URL)["headers"]
    assert headers["Authorization"] == "Bearer fk-test"
    assert (headers["x-factory-client"], headers["Origin"]) == ("web-app",
                                                                "https://app.factory.ai")
    assert (snap.account, snap.plan) == ("dev@example.com", "Factory Team - Fallback: droidCore")
    assert (snap.source, snap.windows[0].id) == ("$FACTORY_API_KEY", "session")


def test_failed_limits_call_falls_back_to_legacy_usage(home):
    http = FakeHttp({
        ("GET", ME_URL): load_json("factory", "auth_me.json"),
        ("GET", LIMITS_URL): http_error(500, b"oops"),
        ("GET", USAGE_URL): load_json("factory", "usage_legacy.json"),
    })
    snap = FactoryProvider().fetch(make_ctx(home, http=http, env={"FACTORY_API_KEY": "fk"}))
    assert http.calls[-1]["url"] == \
        f"{USAGE_URL}?useCache=true&userId=user_01JFIXTURE0000000000000000"
    assert [w.id for w in snap.windows] == ["standard", "premium"]
    assert snap.plan == "Factory Team"


def test_rejected_key_is_reported_over_a_later_404(home):
    http = FakeHttp({
        ("GET", ME_URL): http_error(401, {"detail": "invalid api key"}),
        ("GET", APP_ME_URL): http_error(404, b"not found"),
    })
    with pytest.raises(AuthError) as info:
        FactoryProvider().fetch(make_ctx(home, http=http, env={"FACTORY_API_KEY": "fk-bad"}))
    assert "api-keys" in info.value.hint
    assert [call["url"] for call in http.calls] == [ME_URL, APP_ME_URL]


def test_eu_region_uses_the_eu_api(home):
    eu = "https://api.eu.factory.ai"
    http = FakeHttp({
        ("GET", f"{eu}/api/app/auth/me"): load_json("factory", "auth_me.json"),
        ("GET", f"{eu}/api/billing/limits"): load_json("factory", "billing_limits_counters.json"),
    })
    ctx = make_ctx(home, http=http, env={"FACTORY_API_KEY": "fk"}, settings={"region": "eu"})
    snap = FactoryProvider().fetch(ctx)
    assert {call["url"].split("/api/")[0] for call in http.calls} == {eu}
    assert snap.windows[1].used_percent == 34


def test_not_configured(home):
    provider = FactoryProvider()
    ctx = make_ctx(home)
    assert provider.detect(ctx) is False
    with pytest.raises(NotConfigured):
        provider.fetch(ctx)
