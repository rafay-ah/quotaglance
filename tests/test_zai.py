import json

import pytest

from conftest import NOW, FakeHttp, http_error, load_json, make_ctx, write
from quotaglance.providers.base import AuthError, NotConfigured, ProviderError
from quotaglance.providers.zai import ZaiProvider, find_key, parse_quota

GLOBAL_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
CN_URL = "https://open.bigmodel.cn/api/monitor/usage/quota/limit"


def test_pro_plan_session_weekly_and_mcp():
    windows, plan = parse_quota(load_json("zai", "pro.json"), NOW)
    assert plan == "Pro"
    session, weekly, mcp = windows
    assert (session.id, session.label, session.used_percent) == ("session", "5-hour", 31)
    assert session.resets_at.isoformat() == "2026-10-03T14:21:00+00:00"
    assert (weekly.id, weekly.used_percent) == ("weekly", 12)
    assert weekly.resets_at.isoformat() == "2026-10-07T12:00:00+00:00"
    assert mcp.used_percent == pytest.approx(8.7)
    assert (mcp.used, mcp.limit, mcp.unit) == (87, 1000, "calls")
    assert mcp.detail == "search-prime 61 · web-reader 22 · zread 4"


def test_credit_plan_sorted_by_duration_with_counts():
    windows, plan = parse_quota(load_json("zai", "lite_credits.json"), NOW)
    assert plan == "Lite"
    session, weekly = windows
    assert session.used_percent == pytest.approx(17.0)
    assert session.unit == "credits"
    assert weekly.used_percent == pytest.approx(26.5)


def test_single_window_account():
    windows, plan = parse_quota(load_json("zai", "legacy_single.json"), NOW)
    assert plan == "GLM Coding Max"
    assert len(windows) == 1
    assert windows[0].used_percent == pytest.approx(22.8)


def test_impossible_reset_is_dropped():
    windows, _plan = parse_quota(load_json("zai", "bad_reset.json"), NOW)
    assert windows[0].used_percent == 25
    assert windows[0].resets_at is None


def test_error_envelope_with_http_200_is_auth_error():
    with pytest.raises(AuthError):
        parse_quota(load_json("zai", "token_error.json"), NOW)
    with pytest.raises(ProviderError):
        parse_quota({"code": 200, "success": True, "data": {}}, NOW)


def test_key_from_opencode_auth_selects_region(home):
    write(home / ".local/share/opencode/auth.json",
          json.dumps({"zhipuai-coding-plan": {"type": "api", "key": "cn.key"}}))
    key, region, origin = find_key(make_ctx(home))
    assert (key, region, origin) == ("cn.key", "cn", "OpenCode sign-in")


def test_key_from_claude_code_settings(home):
    write(home / ".claude/settings.json", json.dumps({"env": {
        "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
        "ANTHROPIC_AUTH_TOKEN": "zai.secret"}}))
    assert find_key(make_ctx(home))[:2] == ("zai.secret", "global")


def test_fetch_retries_with_raw_key_after_401(home):
    calls = []

    def respond(method, url, headers, body):
        calls.append(headers["Authorization"])
        if headers["Authorization"].startswith("Bearer"):
            return http_error(401, {"code": 401})
        return load_json("zai", "pro.json")

    http = FakeHttp({("GET", GLOBAL_URL): respond})
    ctx = make_ctx(home, http=http, env={"ZAI_API_KEY": "abc.def"})
    snap = ZaiProvider().fetch(ctx)
    assert calls == ["Bearer abc.def", "abc.def"]
    assert snap.plan == "Pro"


def test_cn_env_uses_bigmodel_host(home):
    http = FakeHttp({("GET", CN_URL): load_json("zai", "legacy_single.json")})
    snap = ZaiProvider().fetch(make_ctx(home, http=http, env={"BIGMODEL_API_KEY": "cn"}))
    assert snap.windows[0].id == "session"


def test_not_configured(home):
    with pytest.raises(NotConfigured):
        ZaiProvider().fetch(make_ctx(home))
