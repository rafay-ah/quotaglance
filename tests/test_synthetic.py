import json

import pytest

from conftest import FakeHttp, http_error, load_json, make_ctx, write
from quotaglance.providers.base import AuthError, NotConfigured, ProviderError
from quotaglance.providers.synthetic import (
    QUOTAS_URL,
    SyntheticProvider,
    find_key,
    parse_quotas,
)


def test_rolling_five_hour_weekly_credits_and_search():
    windows, plan = parse_quotas(load_json("synthetic", "quotas.json"))
    assert plan == "Subscription"
    session, weekly, search = windows  # the legacy subscription pool is ignored
    assert session.used_percent == pytest.approx(45.07, abs=0.01)
    assert (session.used, session.limit, session.unit) == (338, 750, "requests")
    # Pools refill in steps, so the reset is when they are full again.
    assert session.resets_at.isoformat() == "2026-10-03T14:26:42+00:00"
    assert weekly.used_percent == pytest.approx(28.75)
    assert (weekly.used, weekly.limit, weekly.unit) == (pytest.approx(10.35), 36, "USD")
    assert weekly.resets_at.isoformat() == "2026-10-05T12:23:54+00:00"
    assert (search.id, search.used_percent) == ("search", pytest.approx(6.8))
    assert search.resets_at.isoformat() == "2026-10-03T12:30:01.494000+00:00"


def test_exhausted_five_hour_pool_is_full():
    windows, _plan = parse_quotas(load_json("synthetic", "exhausted.json"))
    assert [(w.id, w.used_percent) for w in windows] == [("session", 100), ("weekly", 87.5)]


def test_legacy_subscription_only():
    windows, _plan = parse_quotas(load_json("synthetic", "legacy_subscription.json"))
    assert len(windows) == 1
    assert windows[0].used_percent == pytest.approx(31.11, abs=0.01)
    assert windows[0].resets_at.isoformat() == "2026-10-03T15:12:40.511000+00:00"


def test_wrapped_and_generic_shapes():
    wrapped, _plan = parse_quotas({"data": load_json("synthetic", "quotas.json")})
    assert [w.id for w in wrapped] == ["session", "weekly", "search"]
    windows, plan = parse_quotas(load_json("synthetic", "generic_quotas.json"))
    assert plan == "Starter"
    assert [(w.id, w.used_percent) for w in windows] == [("monthly", 25), ("daily", 75)]
    assert windows[1].window_seconds == 86400


def test_missing_quota_data_is_an_error():
    for payload in ({}, {"data": {}}, "nope", []):
        with pytest.raises(ProviderError):
            parse_quotas(payload)


def test_fetch_sends_bearer_key(home):
    http = FakeHttp({("GET", QUOTAS_URL): load_json("synthetic", "quotas.json")})
    ctx = make_ctx(home, http=http, secrets={("synthetic", "api_key"): "syn_9c2e"})
    snap = SyntheticProvider().fetch(ctx)
    assert http.last(QUOTAS_URL)["headers"]["Authorization"] == "Bearer syn_9c2e"
    assert snap.source == "Keyring"


def test_key_sources(home):
    ctx = make_ctx(home, env={"SYNTHETIC_API_KEY": "'syn_quoted'"})
    assert find_key(ctx) == ("syn_quoted", "$SYNTHETIC_API_KEY")
    write(home / ".local/share/opencode/auth.json",
          json.dumps({"synthetic": {"type": "api", "key": "syn_oc"}}))
    assert find_key(make_ctx(home)) == ("syn_oc", "OpenCode sign-in")


def test_rejected_key_and_not_configured(home):
    http = FakeHttp({("GET", QUOTAS_URL): http_error(401)})
    with pytest.raises(AuthError):
        SyntheticProvider().fetch(make_ctx(home, http=http, env={"SYNTHETIC_API_KEY": "syn"}))
    assert not SyntheticProvider().detect(make_ctx(home))
    with pytest.raises(NotConfigured):
        SyntheticProvider().fetch(make_ctx(home))
