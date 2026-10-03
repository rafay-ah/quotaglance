import json

import pytest

from conftest import NOW, FakeHttp, http_error, load_json, make_ctx, write
from quotaglance.providers.base import AuthError, NotConfigured
from quotaglance.providers.chutes import (
    ChutesProvider,
    api_url,
    find_key,
    parse_quota_usage,
    parse_subscription,
    quota_rows,
)

BASE = "https://api.chutes.ai/users/me"
USAGE_URL = BASE + "/subscription_usage"


def test_subscription_four_hour_and_monthly_caps():
    windows, plan = parse_subscription(load_json("chutes", "subscription.json"))
    assert plan == "$20/mo"
    session, monthly = windows
    assert (session.id, session.label) == ("session", "4-hour")
    assert session.used_percent == pytest.approx(41.01, abs=0.01)
    assert (session.used, session.limit, session.unit) == (
        pytest.approx(3.4172), pytest.approx(8.3333, abs=1e-4), "USD")
    assert session.resets_at.isoformat() == "2026-10-03T16:00:00+00:00"
    assert monthly.used_percent == pytest.approx(41.86)
    assert monthly.resets_at.isoformat() == "2026-10-14T17:42:10.552318+00:00"


def test_custom_subscription_has_an_uncapped_month():
    windows, plan = parse_subscription(load_json("chutes", "subscription_custom.json"))
    assert plan == "$10/mo (custom)"
    assert windows[0].used_percent == 0
    assert (windows[1].used_percent, windows[1].detail) == (None, "Uncapped")
    assert parse_subscription(load_json("chutes", "no_subscription.json")) == ([], None)


def test_quota_rows_and_daily_requests():
    assert quota_rows(load_json("chutes", "quotas.json")) == [("*", 300)]
    assert quota_rows({"*": 0}) == [("*", 0)]
    assert quota_rows({}) == []
    daily = parse_quota_usage(load_json("chutes", "quota_usage.json"), NOW)
    assert daily.used_percent == pytest.approx(95.67, abs=0.01)
    assert daily.resets_at.isoformat() == "2026-10-04T00:00:00+00:00"
    unlimited = parse_quota_usage({"quota": "unlimited", "used": 0}, NOW)
    assert (unlimited.used_percent, unlimited.detail) == (None, "Unlimited")


def test_fetch_subscription_with_balance(home):
    http = FakeHttp({("GET", USAGE_URL): load_json("chutes", "subscription.json"),
                     ("GET", BASE): load_json("chutes", "user.json")})
    ctx = make_ctx(home, http=http, secrets={("chutes", "api_key"): "cpk_test"})
    snap = ChutesProvider().fetch(ctx)
    assert http.last(USAGE_URL)["headers"]["Authorization"] == "Bearer cpk_test"
    assert [w.id for w in snap.windows] == ["session", "monthly", "balance"]
    assert snap.windows[-1].detail == "$12.48 left"
    assert (snap.plan, snap.source) == ("$20/mo", "Keyring")


def test_fetch_without_subscription_uses_the_daily_quota(home):
    http = FakeHttp({("GET", USAGE_URL): load_json("chutes", "no_subscription.json"),
                     ("GET", BASE + "/quotas"): load_json("chutes", "quotas.json"),
                     ("GET", BASE + "/quota_usage/"): load_json("chutes", "quota_usage.json"),
                     ("GET", BASE): http_error(500)})
    snap = ChutesProvider().fetch(make_ctx(home, http=http, env={"CHUTES_API_KEY": "cpk"}))
    assert http.last(BASE + "/quota_usage/")["url"].endswith("/quota_usage/%2A")
    assert [w.id for w in snap.windows] == ["daily_requests"]  # the balance call failed
    assert snap.plan == "Pay as you go"


def test_rejected_key_is_auth_error(home):
    body = {"detail": "Invalid token or user not found"}
    http = FakeHttp({("GET", USAGE_URL): http_error(401, body)})
    with pytest.raises(AuthError, match="rejected"):
        ChutesProvider().fetch(make_ctx(home, http=http, env={"CHUTES_API_KEY": "cpk"}))


def test_key_sources_base_override_and_not_configured(home):
    assert not ChutesProvider().detect(make_ctx(home))
    with pytest.raises(NotConfigured):
        ChutesProvider().fetch(make_ctx(home))
    write(home / ".local/share/opencode/auth.json",
          json.dumps({"chutes": {"type": "api", "key": "cpk_oc"}}))
    assert find_key(make_ctx(home)) == ("cpk_oc", "OpenCode sign-in")
    assert api_url(make_ctx(home, env={"CHUTES_API_URL": "https://proxy.example/"})) == \
        "https://proxy.example"
    assert api_url(make_ctx(home, env={"CHUTES_API_URL": "http://insecure"})) == \
        "https://api.chutes.ai"
