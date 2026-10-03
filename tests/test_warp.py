import pytest

from conftest import FakeHttp, http_error, load_json, make_ctx
from quotaglance.providers.base import AuthError, NotConfigured, ProviderError
from quotaglance.providers.warp import URL, WarpProvider, parse_request_limits


def test_monthly_credits_and_combined_addon_grants():
    credits, addons = parse_request_limits(load_json("warp", "normal.json"))
    assert (credits.id, credits.label) == ("credits", "Credits")
    assert credits.used_percent == pytest.approx(42.8)
    assert (credits.used, credits.limit, credits.unit) == (642, 1500, "credits")
    assert credits.resets_at.isoformat() == "2026-10-28T19:16:33.462988+00:00"
    assert (addons.id, addons.label) == ("addon_credits", "Add-ons")
    assert addons.used_percent == pytest.approx(29.4118, abs=1e-4)  # user + workspace grants
    assert (addons.used, addons.limit) == (500, 1700)
    assert addons.detail == "1,000 credits expire Oct 15"  # 120 + 880 share that expiry


def test_unlimited_plan_has_no_reset():
    (credits,) = parse_request_limits(load_json("warp", "unlimited.json"))
    assert (credits.used_percent, credits.resets_at, credits.detail) == (0, None, "Unlimited")


def test_string_and_null_scalars():
    (credits,) = parse_request_limits(load_json("warp", "string_scalars.json"))
    assert (credits.used_percent, credits.limit) == (100, 1500)
    assert credits.resets_at.isoformat() == "2026-10-28T19:16:33+00:00"


def test_exhausted_month_with_addons_left():
    credits, addons = parse_request_limits(load_json("warp", "exhausted_addons.json"))
    assert credits.used_percent == 100
    assert addons.used_percent == pytest.approx(30.8)
    assert addons.detail == "1,730 credits expire Apr 1"


def test_graphql_errors_and_unexpected_shapes():
    with pytest.raises(AuthError):
        parse_request_limits(load_json("warp", "graphql_error.json"))
    with pytest.raises(ProviderError, match=r"^Timeout \| second$"):
        parse_request_limits({"errors": [{"message": "Timeout"}, "second"]})
    with pytest.raises(AuthError):
        parse_request_limits({"data": {"user": {"__typename": "AuthError"}}})
    with pytest.raises(ProviderError, match="SomethingElse"):
        parse_request_limits({"data": {"user": {"__typename": "SomethingElse"}}})
    with pytest.raises(ProviderError, match="credit usage"):
        parse_request_limits({"data": {"user": {"__typename": "UserOutput", "user": {}}}})
    with pytest.raises(ProviderError):
        parse_request_limits([{"data": {}}])


def test_fetch_sends_the_official_client_identity(home):
    http = FakeHttp({("POST", URL): load_json("warp", "normal.json")})
    ctx = make_ctx(home, http=http, secrets={("warp", "api_key"): "wk-keyring"},
                   env={"WARP_API_KEY": "wk-env"})
    snap = WarpProvider().fetch(ctx)
    call = http.last(URL)
    assert call["headers"]["Authorization"] == "Bearer wk-keyring"
    assert call["headers"]["User-Agent"] == "Warp/1.0"  # anything else hits the edge limiter
    assert call["headers"]["x-warp-client-id"] == "warp-app"
    assert call["json"]["operationName"] == "GetRequestLimitInfo"
    assert call["json"]["variables"]["requestContext"]["osContext"] == {
        "category": "macOS", "name": "macOS", "version": "15.6.1"}
    assert "requestsUsedSinceLastRefresh" in call["json"]["query"]
    assert (snap.plan, snap.source, len(snap.windows)) == (None, "Warp API", 2)


def test_env_keys_in_order(home):
    provider = WarpProvider()
    assert provider.api_key(make_ctx(home, env={"WARP_TOKEN": "wk-token"})) == "wk-token"
    both = make_ctx(home, env={"WARP_TOKEN": "wk-token", "WARP_API_KEY": "wk-api"})
    assert provider.api_key(both) == "wk-api"


def test_edge_rate_limit_backs_off_for_a_minute(home):
    http = FakeHttp({("POST", URL): http_error(429, b"Rate exceeded.",
                                               {"Content-Type": "text/plain"})})
    with pytest.raises(ProviderError) as info:
        WarpProvider().fetch(make_ctx(home, http=http, env={"WARP_API_KEY": "wk"}))
    assert info.value.transient
    assert info.value.retry_after >= 60


def test_expired_key_is_auth_error(home):
    http = FakeHttp({("POST", URL): http_error(401, {"error": "invalid"})})
    with pytest.raises(AuthError) as info:
        WarpProvider().fetch(make_ctx(home, http=http, env={"WARP_API_KEY": "wk"}))
    assert "API keys" in info.value.hint


def test_not_configured(home):
    provider = WarpProvider()
    ctx = make_ctx(home)
    assert provider.detect(ctx) is False
    with pytest.raises(NotConfigured):
        provider.fetch(ctx)
