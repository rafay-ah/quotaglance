import json
from datetime import datetime, timezone

import pytest

from conftest import NOW, FakeHttp, http_error, load_json, make_ctx, write
from quotaglance.providers.base import AuthError, NotConfigured, ProviderError
from quotaglance.providers.minimax import MiniMaxProvider, find_key, parse_remains

EARLY = datetime(2026, 10, 3, 4, 0, tzinfo=timezone.utc)  # 12:00 in UTC+8, inside the slot
TOKEN_PLAN = "https://api.minimax.io/v1/token_plan/remains"
LEGACY = "https://api.minimax.io/v1/api/openplatform/coding_plan/remains"
CN_TOKEN_PLAN = "https://api.minimaxi.com/v1/token_plan/remains"


def test_token_plan_percent_lanes_and_hidden_video_lane():
    windows, plan = parse_remains(load_json("minimax", "token_plan.json"), EARLY)
    assert plan == "Token Plan Plus"
    session, weekly, points = windows
    assert (session.id, session.label, session.used_percent) == ("session", "5-hour", 28)
    assert session.resets_at.isoformat() == "2026-10-03T07:00:00+00:00"
    assert (session.window_seconds, session.used) == (18000, None)
    assert weekly.used_percent == 45
    assert weekly.resets_at.isoformat() == "2026-10-04T16:00:00+00:00"
    assert (points.used, points.detail) == (23000, "23,000 points")


def test_legacy_counts_are_what_is_left():
    windows, plan = parse_remains(load_json("minimax", "coding_plan_legacy.json"), EARLY)
    session, weekly = windows
    assert plan == "Coding Plan Max"
    assert session.used_percent == pytest.approx(25.13, abs=0.01)
    assert (session.used, session.limit, session.unit) == (377, 1500, "prompts")
    assert weekly.used_percent == pytest.approx(20.13, abs=0.01)  # counts sent as strings
    assert weekly.used == 3020


def test_boosted_and_unlimited_weekly():
    windows, _plan = parse_remains(load_json("minimax", "boosted_weekly.json"), EARLY)
    assert [w.used_percent for w in windows] == [0, 30]
    windows, plan = parse_remains(load_json("minimax", "unlimited_weekly.json"), EARLY)
    assert plan == "Token Plan Max"
    assert (windows[1].id, windows[1].used_percent, windows[1].detail) == (
        "weekly", None, "Unlimited")


def test_past_slot_end_falls_back_to_remains_time():
    windows, _plan = parse_remains(load_json("minimax", "token_plan.json"), NOW)
    assert windows[0].resets_at.isoformat() == "2026-10-03T15:00:00+00:00"


def test_error_envelopes_with_http_200():
    with pytest.raises(AuthError):
        parse_remains(load_json("minimax", "invalid_key.json"), NOW)
    with pytest.raises(ProviderError) as exc:
        parse_remains({"base_resp": {"status_code": 1002, "status_msg": "rate limit"}}, NOW)
    assert exc.value.transient
    with pytest.raises(ProviderError, match="No coding plan"):
        parse_remains({"model_remains": [], "base_resp": {"status_code": 0}}, NOW)


def test_fetch_falls_back_to_legacy_endpoint(home):
    http = FakeHttp({("GET", TOKEN_PLAN): http_error(404),
                     ("GET", LEGACY): load_json("minimax", "coding_plan_legacy.json")})
    ctx = make_ctx(home, http=http, now=EARLY, secrets={("minimax", "api_key"): "sk-cp-abc"})
    snap = MiniMaxProvider().fetch(ctx)
    headers = http.last(LEGACY)["headers"]
    assert headers["Authorization"] == "Bearer sk-cp-abc"
    assert headers["MM-API-Source"] == "QuotaGlance"
    assert (snap.plan, snap.source) == ("Coding Plan Max", "Keyring")


def test_key_rejected_by_global_host_retries_china(home):
    http = FakeHttp({("GET", TOKEN_PLAN): http_error(401), ("GET", LEGACY): http_error(401),
                     ("GET", CN_TOKEN_PLAN): load_json("minimax", "token_plan.json")})
    ctx = make_ctx(home, http=http, now=EARLY, env={"MINIMAX_API_KEY": "sk-cp-cn"})
    snap = MiniMaxProvider().fetch(ctx)
    assert [call["url"] for call in http.calls] == [TOKEN_PLAN, LEGACY, CN_TOKEN_PLAN]
    assert snap.plan == "Token Plan Plus"


def test_key_rejected_everywhere_is_auth_error(home):
    http = FakeHttp({("GET", "https://api.minimax"): http_error(403)})
    with pytest.raises(AuthError):
        MiniMaxProvider().fetch(make_ctx(home, http=http, env={"MINIMAX_API_KEY": "sk-cp-x"}))
    assert len(http.calls) == 4


def test_key_sources_skip_pay_as_you_go_keys(home):
    env = {"MINIMAX_API_KEY": "sk-api-payg", "MINIMAX_CODING_API_KEY": "sk-cp-env"}
    assert find_key(make_ctx(home, env=env)) == ("sk-cp-env", "global", "$MINIMAX_CODING_API_KEY")
    write(home / ".claude/settings.json", json.dumps({"env": {
        "ANTHROPIC_BASE_URL": "https://api.minimaxi.com/anthropic",
        "ANTHROPIC_AUTH_TOKEN": "sk-cp-claude"}}))
    payg = {"MINIMAX_API_KEY": "sk-api-payg"}
    assert find_key(make_ctx(home, env=payg)) == ("sk-cp-claude", "cn", "Claude Code settings")
    write(home / ".local/share/opencode/auth.json",
          json.dumps({"minimax-coding-plan": {"type": "api", "key": "sk-cp-oc"}}))
    assert find_key(make_ctx(home, env=payg)) == ("sk-cp-oc", "global", "OpenCode sign-in")


def test_pay_as_you_go_key_alone_and_no_key(home):
    payg = make_ctx(home, env={"MINIMAX_API_KEY": "sk-api-payg"})
    assert not MiniMaxProvider().detect(payg)
    with pytest.raises(NotConfigured, match="Pay-as-you-go"):
        MiniMaxProvider().fetch(payg)
    with pytest.raises(NotConfigured, match="No MiniMax"):
        MiniMaxProvider().fetch(make_ctx(home))
