import json

import pytest

from conftest import NOW, FakeHttp, http_error, load_json, load_text, make_ctx, write
from quotaglance.providers.base import AuthError, NotConfigured, ProviderError
from quotaglance.providers.kimi import (
    KimiProvider,
    find_key,
    parse_usages,
    plan_label,
    usages_url,
)

CN_URL = "https://api.kimi.com/coding/v1/usages"
INTL_URL = "https://api.kimi.ai/coding/v1/usages"


def test_ratio_pools_win_with_monthly_split_and_extra_usage():
    windows, plan = parse_usages(load_json("kimi", "full.json"))
    assert plan == "Allegretto"
    by_id = {w.id: w for w in windows}
    assert list(by_id) == ["session", "weekly", "monthly", "extra_usage"]
    assert by_id["session"].used_percent == pytest.approx(41.5)
    assert by_id["session"].resets_at.isoformat() == "2026-10-03T15:20:00+00:00"
    assert by_id["weekly"].used_percent == pytest.approx(18)
    assert by_id["monthly"].used_percent == pytest.approx(7.31)
    assert by_id["monthly"].detail == "Code 5.1% · other 2.2%"
    extra = by_id["extra_usage"]
    assert (extra.used, extra.limit, extra.detail) == (pytest.approx(7.66), 20, "$12.34 left")


def test_legacy_request_counts():
    windows, plan = parse_usages(load_json("kimi", "legacy_counts.json"))
    session, weekly = windows
    assert plan is None
    assert (session.used, session.limit) == (57, 200)
    assert session.used_percent == pytest.approx(28.5)
    assert session.resets_at.isoformat() == "2026-10-03T13:05:44.902117+00:00"
    assert weekly.used_percent == pytest.approx(29.83, abs=0.01)
    assert weekly.unit == "requests"


def test_exhausted_monthly_is_clamped_and_weekly_never_invented():
    windows, _plan = parse_usages(load_json("kimi", "monthly_exhausted.json"))
    assert [(w.id, w.used_percent) for w in windows] == [("session", 0), ("monthly", 100)]


def test_zero_ratio_placeholder_defers_to_counters():
    data = load_json("kimi", "legacy_counts.json")
    data["usages"] = {"limit_5h": {"used_ratio": 0, "reset_time": "2026-10-03T13:05:44Z"}}
    session = parse_usages(data)[0][0]
    assert (session.used_percent, session.used) == (pytest.approx(28.5), 57)


def test_no_windows_is_an_error_and_plan_levels():
    for payload in ({}, {"usages": {}}, {"usages": {"limit_5h": {}}}):
        with pytest.raises(ProviderError):
            parse_usages(payload)
    assert plan_label({"user": {"membership": {"level": "LEVEL_BASIC"}}}) == "Moderato"
    assert plan_label({"user": {"membership": {"level": "LEVEL_UNSPECIFIED"}}}) is None
    assert plan_label({"version": "GOODS_VERSION_V2",
                       "user": {"membership": {"level": "LEVEL_BASIC"}}}) == "Basic"
    assert plan_label({"user": "malformed"}) is None


def test_fetch_uses_region_host_and_bearer(home):
    http = FakeHttp({("GET", INTL_URL): load_json("kimi", "full.json")})
    ctx = make_ctx(home, http=http, secrets={("kimi", "api_key"): "sk-kimi-test"},
                   settings={"region": "international"})
    snap = KimiProvider().fetch(ctx)
    headers = http.last(INTL_URL)["headers"]
    assert headers["Authorization"] == "Bearer sk-kimi-test"
    assert not any(name.startswith("X-Msh") for name in headers)
    assert (snap.plan, snap.source) == ("Allegretto", "Keyring")


def test_fresh_cli_token_is_borrowed_read_only(home):
    write(home / ".kimi-code/credentials/kimi-code.json", load_text("kimi", "cli_credentials.json"))
    write(home / ".kimi-code/device_id", "3f2b8c1e-0d4a-4e5b-9c6d-7e8f9a0b1c2d\n")
    before = sorted(home.rglob("*"))
    http = FakeHttp({("GET", CN_URL): load_json("kimi", "legacy_counts.json")})
    snap = KimiProvider().fetch(make_ctx(home, http=http))
    headers = http.last(CN_URL)["headers"]
    assert headers["Authorization"].startswith("Bearer eyJhbGci")
    assert headers["X-Msh-Platform"] == "kimi_code_cli"
    assert headers["X-Msh-Device-Id"] == "3f2b8c1e-0d4a-4e5b-9c6d-7e8f9a0b1c2d"
    assert snap.source == "Kimi Code CLI"
    assert sorted(home.rglob("*")) == before


def test_expired_cli_token_asks_to_renew(home):
    data = json.loads(load_text("kimi", "cli_credentials.json"))
    data["expires_at"] = NOW.timestamp() + 30  # inside the one-minute safety margin
    write(home / ".kimi-code/credentials/kimi-code.json", json.dumps(data))
    assert KimiProvider().detect(make_ctx(home))
    with pytest.raises(AuthError, match="expired"):
        KimiProvider().fetch(make_ctx(home))


def test_local_keys_override_url_and_rejected_key(home):
    write(home / ".local/share/opencode/auth.json",
          json.dumps({"kimi-code-plan-global": {"type": "api", "key": "sk-kimi-oc"}}))
    assert find_key(make_ctx(home)) == ("sk-kimi-oc", "international", "OpenCode sign-in")
    ctx = make_ctx(home, env={"KIMI_CODE_BASE_URL": "https://proxy.example/coding"})
    assert usages_url(ctx, "china") == "https://proxy.example/coding/v1/usages"
    body = {"error": {"message": "Invalid Authentication", "type": "invalid_authentication_error"}}
    http = FakeHttp({("GET", CN_URL): http_error(401, body)})
    with pytest.raises(AuthError, match="rejected"):
        KimiProvider().fetch(make_ctx(home, http=http, env={"KIMI_API_KEY": "sk-kimi-bad"}))


def test_not_configured(home):
    assert not KimiProvider().detect(make_ctx(home))
    with pytest.raises(NotConfigured):
        KimiProvider().fetch(make_ctx(home))
