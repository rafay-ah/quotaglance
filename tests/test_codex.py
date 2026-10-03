import json
import os
import sys
from datetime import timedelta

import pytest

from conftest import NOW, FakeHttp, http_error, jwt, load_json, load_text, make_ctx, write
from quotaglance.providers import codex
from quotaglance.providers.base import AuthError, NotConfigured, ProviderError
from quotaglance.providers.codex import (
    CodexProvider,
    base_url,
    kind_of,
    newest_snapshot,
    parse_app_server,
    parse_rollout_line,
    parse_usage,
    plan_label,
    reached_note,
    rollout_windows,
    token_info,
    usage_url,
)
from quotaglance.util import parse_time

USAGE = "https://chatgpt.com/backend-api/wham/usage"
ACCOUNT = "8f3c2a9e-5b1d-4e7f-a6c0-2d9b7e4f1a35"


@pytest.fixture(autouse=True)
def clean_caches():
    codex.reset_caches()
    yield
    codex.reset_caches()


def by_id(windows):
    return {w.id: w for w in windows}


def edge(name):
    return load_json("codex", "usage_edges.json")[name]


def install_rollout(home, text, name="rollout-2026-10-02T16-02-58-01a10095.jsonl",
                    day="2026/10/02", age=None):
    path = write(home / ".codex/sessions" / day / name, text)
    stamp = (NOW - (age or timedelta(hours=20))).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def install_auth(home, name="auth.json"):
    write(home / ".codex/auth.json", load_text("codex", name))


FAKE_CODEX = """#!{python}
import json, os, sys
with open(os.path.join(os.path.dirname(__file__), "argv.json"), "w") as f:
    json.dump({{"argv": sys.argv[1:], "home": os.environ.get("CODEX_HOME")}}, f)
answer = json.loads(open(os.path.join(os.path.dirname(__file__), "answer.json")).read())
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        print(json.dumps({{"id": msg["id"], "result": {{"userAgent": "quotaglance/0.160.0"}}}}),
              flush=True)
    elif msg.get("method") == "account/rateLimits/read":
        print(json.dumps({{"method": "account/updated", "params": {{"planType": "plus"}}}}),
              flush=True)
        print(json.dumps(dict(answer, id=msg["id"])), flush=True)
"""


def install_codex_cli(home, answer):
    bin_dir = home / ".local/bin"
    script = write(bin_dir / "codex", FAKE_CODEX.format(python=sys.executable))
    script.chmod(0o755)
    write(bin_dir / "answer.json", json.dumps(answer))
    return bin_dir


# -- wham/usage ------------------------------------------------------------------------


def test_plus_with_spark_limit():
    windows, plan = parse_usage(load_json("codex", "usage_plus_spark.json"), NOW)
    found = by_id(windows)
    assert plan == "Plus"
    assert list(found) == ["session", "weekly", "spark_session", "spark_weekly"]
    assert found["session"].used_percent == 37
    assert found["session"].window_seconds == 18000
    assert found["session"].resets_at.isoformat() == "2026-10-02T16:24:51+00:00"
    assert found["weekly"].used_percent == 58
    assert (found["spark_session"].label, found["spark_session"].used_percent) == ("Spark 5h", 12)
    assert found["spark_weekly"].label == "Spark week"
    assert "credits" not in found  # a zero balance ("0E-10") is not worth a row


def test_pro_lite_is_called_pro():
    windows, plan = parse_usage(load_json("codex", "usage_prolite.json"), NOW)
    assert plan == "Pro"
    assert [(w.id, w.used_percent) for w in windows] == [("session", 11), ("weekly", 23)]


def test_free_plan_weekly_only_window_is_weekly():
    windows, plan = parse_usage(load_json("codex", "usage_free.json"), NOW)
    assert plan == "Free"
    assert [(w.id, w.label, w.used_percent) for w in windows] == [("weekly", "Weekly", 6)]


def test_team_cap_reached_with_monthly_credit_pool():
    data = load_json("codex", "usage_team.json")
    found = by_id(parse_usage(data, NOW)[0])
    assert found["weekly"].used_percent == 100
    monthly = found["monthly_credits"]
    assert monthly.used_percent == 24
    assert (monthly.used, monthly.limit, monthly.unit) == (pytest.approx(612.40377), 2500,
                                                           "credits")
    assert monthly.resets_at.isoformat() == "2026-11-01T00:00:00+00:00"
    assert found["credits"].detail == "Workspace credits"
    assert reached_note(data["rate_limit_reached_type"]) == "Workspace usage limit reached"


def test_unknown_window_length_is_kept_with_its_length():
    windows, _plan = parse_usage(edge("unknown_window"), NOW)
    assert [(w.id, w.label, w.used_percent) for w in windows] == [("session", "9h window", 17)]


def test_windows_are_classified_by_length_not_position():
    found = by_id(parse_usage(edge("reversed"), NOW)[0])
    assert (found["session"].used_percent, found["weekly"].used_percent) == (37, 58)


def test_credits_only_plan():
    windows, plan = parse_usage(edge("credits_only"), NOW)
    assert plan == "Pro (More)"
    assert [(w.id, w.used, w.detail) for w in windows] == [("credits", 14.5,
                                                            "14.5 credits left")]


def test_malformed_extra_limits_are_skipped():
    windows, _plan = parse_usage(edge("malformed_extra"), NOW)
    assert [w.id for w in windows] == ["session"]


def test_code_review_and_other_model_limits():
    review = parse_usage(edge("code_review"), NOW)[0]
    assert [(w.id, w.label, w.used_percent) for w in review] == [
        ("review_weekly", "Code review", 0)]
    found = by_id(parse_usage(edge("other_model"), NOW)[0])
    assert found["codex-otter_weekly"].label == "Max week"
    assert found["codex-otter_weekly"].used_percent == 44


@pytest.mark.parametrize(("raw", "expected"), [
    ("prolite", "Pro"), ("pro", "Pro (More)"), ("promax", "Pro (Max)"), ("hc", "Enterprise"),
    ("education", "Edu"), ("self_serve_business_usage_based", "Self Serve Business Usage Based"),
    ("unknown", None), (None, None),
])
def test_plan_names_match_codex(raw, expected):
    assert plan_label(raw) == expected


@pytest.mark.parametrize(("minutes", "kind"), [
    (300, "session"), (299, "session"), (10079, "weekly"), (1440, "daily"),
    (43200, "monthly"), (540, "unknown"), (None, "unknown"),
])
def test_window_kinds_tolerate_codex_rounding(minutes, kind):
    assert kind_of(minutes) == kind


# -- app-server ------------------------------------------------------------------------------


def test_app_server_result_with_spark_bucket():
    windows, plan = parse_app_server(load_json("codex", "app_server_result.json"), NOW)
    assert plan == "Plus"
    assert [(w.id, w.used_percent) for w in windows] == [
        ("session", 37), ("weekly", 58), ("spark_session", 12), ("spark_weekly", 4)]


def test_old_app_server_snake_case():
    result = {"rateLimits": {"primary": {"used_percent": 40, "window_minutes": 300,
                                         "resets_at": 1790958291}}}
    windows, _plan = parse_app_server(result, NOW)
    assert [(w.id, w.used_percent) for w in windows] == [("session", 40)]


# -- rollout logs -------------------------------------------------------------------------


def variant(index):
    return load_text("codex", "rollout_variants.jsonl").splitlines()[index]


@pytest.mark.parametrize(("index", "session", "weekly", "plan"), [
    (0, 64, 81, "Pro (More)"),  # 0.122+: limit_id, plan, reached type
    (1, 9, 33, "Plus"),         # 0.100+: limit_id
    (2, 21, 47, "Plus"),        # 0.66+: plan_type and credits
    (3, 3, 14, None),           # 0.48+: integer resets_at, 299/10079-minute windows
    (4, 5, 11, None),           # pre-release: RFC 3339 resets_at
    (5, 2, 19, None),           # 0.41+: resets_in_seconds
    (6, 14, 21, None),          # 0.40: flat fields
    (7, 8, 30, None),           # 0.40 pre-release: weekly_* fields
])
def test_every_rollout_format(index, session, weekly, plan):
    observed, limits = parse_rollout_line(variant(index))
    windows, _seen, found_plan, _note = rollout_windows({"main": (observed, limits)})
    found = by_id(windows)
    assert (found["session"].used_percent, found["weekly"].used_percent) == (session, weekly)
    assert found_plan == plan


def test_rollout_reset_times():
    observed, limits = parse_rollout_line(variant(5))
    windows = by_id(rollout_windows({"main": (observed, limits)})[0])
    assert windows["session"].resets_at == parse_time("2025-09-30T12:05:41.415Z") + \
        timedelta(seconds=16810)
    observed, limits = parse_rollout_line(variant(4))
    windows = by_id(rollout_windows({"main": (observed, limits)})[0])
    assert windows["session"].resets_at.isoformat() == "2025-10-18T02:11:09+00:00"
    observed, limits = parse_rollout_line(variant(6))
    assert by_id(rollout_windows({"main": (observed, limits)})[0])["session"].resets_at is None


def test_rollout_credits_carry_through():
    observed, limits = parse_rollout_line(variant(2))
    found = by_id(rollout_windows({"main": (observed, limits)})[0])
    assert found["credits"].detail == "112.5 credits left"


@pytest.mark.parametrize("index", [8, 9])
def test_null_and_metadata_only_rate_limits_are_skipped(index):
    assert parse_rollout_line(variant(index)) is None


def test_main_limits_survive_the_spark_bucket_pitfall(home):
    install_rollout(home, load_text("codex", "rollout_current.jsonl"))
    snapshot = newest_snapshot(home / ".codex", NOW)
    windows, observed, _plan, _note = rollout_windows(snapshot)
    found = by_id(windows)
    assert (found["session"].used_percent, found["weekly"].used_percent) == (37.5, 58)
    assert found["spark_session"].used_percent == 12
    assert "spark_weekly" not in found
    assert observed.isoformat() == "2026-10-02T14:02:58.785000+00:00"


def test_newest_file_wins_and_old_or_compressed_files_are_ignored(home):
    older = variant(1).replace("2026-03-04T16:20:00.402Z", "2026-10-01T08:00:00.000Z")
    install_rollout(home, older + "\n", name="rollout-a.jsonl", day="2026/10/01",
                    age=timedelta(days=2))
    install_rollout(home, load_text("codex", "rollout_current.jsonl"))
    stale = variant(0).replace("2026-05-14T09:41:22.118Z", "2026-10-03T11:00:00.000Z")
    install_rollout(home, stale + "\n", name="rollout-old.jsonl", day="2026/09/20",
                    age=timedelta(days=8))
    write(home / ".codex/sessions/2026/10/03/rollout-z.jsonl.zst", b"\x28\xb5\x2f\xfd")
    windows, *_rest = rollout_windows(newest_snapshot(home / ".codex", NOW))
    assert by_id(windows)["session"].used_percent == 37.5


def test_sessions_from_another_workspace_are_ignored(home):
    install_rollout(home, load_text("codex", "rollout_current.jsonl"))
    other = load_text("codex", "rollout_current.jsonl").replace(ACCOUNT, "another-workspace")
    other = other.replace('"used_percent":37.5', '"used_percent":91.0')
    install_rollout(home, other, name="rollout-b.jsonl", age=timedelta(hours=1))
    mine = rollout_windows(newest_snapshot(home / ".codex", NOW, account=ACCOUNT))[0]
    assert by_id(mine)["session"].used_percent == 37.5
    codex.reset_caches()
    anyone = rollout_windows(newest_snapshot(home / ".codex", NOW))[0]
    assert by_id(anyone)["session"].used_percent == 91


# -- auth and endpoints -------------------------------------------------------------------


def test_token_info_from_codex_auth_file():
    info = token_info(load_json("codex", "auth.json"), NOW)
    assert info["fresh"] and info["account"] == ACCOUNT
    assert (info["email"], info["plan"]) == ("dev.fixture@example.com", "Plus")
    assert token_info(load_json("codex", "auth.json"), NOW + timedelta(days=10))["fresh"] is False


def test_legacy_camel_case_tokens_and_account_from_jwt():
    claims = {"exp": NOW.timestamp() + 3600,
              "https://api.openai.com/auth": {"chatgpt_account_id": "acct-from-jwt"}}
    auth = {"tokens": {"accessToken": jwt(claims), "idToken": jwt({"email": "a@b.c"}),
                       "refreshToken": "rt_x"}, "last_refresh": "2025-12-20T12:34:56Z"}
    info = token_info(auth, NOW)
    assert (info["account"], info["email"], info["fresh"]) == ("acct-from-jwt", "a@b.c", True)


def test_opaque_token_uses_the_eight_day_rule():
    auth = load_json("codex", "auth_opaque.json")
    assert token_info(auth, NOW)["fresh"] is False  # refreshed 13 days ago
    assert token_info(auth, parse_time("2026-09-25T08:00:00Z"))["fresh"] is True


def test_base_url_from_config(home):
    codex_dir = home / ".codex"
    assert base_url(codex_dir) == "https://chatgpt.com/backend-api"
    write(codex_dir / "config.toml", 'model = "gpt-5.5"\nchatgpt_base_url = "https://chatgpt.com/"\n')
    assert usage_url(base_url(codex_dir)) == USAGE
    write(codex_dir / "config.toml", 'chatgpt_base_url = "https://codex.example.com/api"\n')
    assert usage_url(base_url(codex_dir)) == "https://codex.example.com/api/api/codex/usage"


# -- fetch ------------------------------------------------------------------------------------


def test_logs_only_mode_never_touches_the_network(home):
    install_auth(home)
    install_rollout(home, load_text("codex", "rollout_current.jsonl"))
    snap = CodexProvider().fetch(make_ctx(home, settings={"source": "logs"}))
    assert snap.source == "Session logs"
    assert snap.plan == "Plus"  # from the id token
    assert snap.observed_at.isoformat() == "2026-10-02T14:02:58.785000+00:00"
    assert by_id(snap.windows)["session"].used_percent == 37.5


def test_fresh_token_reads_chatgpt_usage(home):
    install_auth(home)
    install_rollout(home, load_text("codex", "rollout_current.jsonl"))
    http = FakeHttp({("GET", USAGE): load_json("codex", "usage_plus_spark.json")})
    snap = CodexProvider().fetch(make_ctx(home, http=http))
    headers = http.last(USAGE)["headers"]
    assert headers["Authorization"].startswith("Bearer eyJ")
    assert headers["ChatGPT-Account-Id"] == ACCOUNT
    assert snap.source == "Session logs + ChatGPT usage API"
    assert snap.account == "dev.fixture@example.com"
    found = by_id(snap.windows)
    assert found["session"].used_percent == 37  # the live reading is newer than the log
    assert found["spark_weekly"].used_percent == 4


def test_usage_is_read_at_most_every_five_minutes(home):
    install_auth(home)
    http = FakeHttp({("GET", USAGE): load_json("codex", "usage_plus_spark.json")})
    provider = CodexProvider()
    provider.fetch(make_ctx(home, http=http))
    provider.fetch(make_ctx(home, http=http))
    assert len(http.calls) == 1
    forced = make_ctx(home, http=http)
    forced.force = True
    provider.fetch(forced)
    assert len(http.calls) == 2


def test_rate_limit_keeps_last_reading_and_backs_off(home):
    install_auth(home)
    responses = [load_json("codex", "usage_plus_spark.json"),
                 http_error(429, {"detail": "Too many requests"})]
    http = FakeHttp({("GET", USAGE): lambda *a: responses.pop(0)})
    provider = CodexProvider()
    provider.fetch(make_ctx(home, http=http))
    for _ in range(2):
        forced = make_ctx(home, http=http)
        forced.force = True
        snap = provider.fetch(forced)
        assert by_id(snap.windows)["spark_session"].used_percent == 12
    assert len(http.calls) == 2  # the second forced refresh waited out the back-off


def test_stale_token_without_cli_shows_logs_and_asks_for_login(home):
    install_auth(home)
    later = NOW + timedelta(days=10)
    install_rollout(home, load_text("codex", "rollout_current.jsonl"), age=-timedelta(days=9))
    snap = CodexProvider().fetch(make_ctx(home, now=later))  # FakeHttp fails on any request
    assert snap.source == "Session logs"
    assert "login needs a refresh" in snap.message and "codex login" in snap.message
    codex.reset_caches()
    (home / ".codex/sessions").rename(home / ".codex/elsewhere")
    with pytest.raises(AuthError):
        CodexProvider().fetch(make_ctx(home, now=later))


def test_stale_token_asks_codex_app_server(home):
    install_auth(home)
    bin_dir = install_codex_cli(home, {"result": load_json("codex", "app_server_result.json")})
    snap = CodexProvider().fetch(make_ctx(home, now=NOW + timedelta(days=10)))
    assert snap.source == "Codex app-server"
    assert [w.id for w in snap.windows] == ["session", "weekly", "spark_session", "spark_weekly"]
    launched = json.loads((bin_dir / "argv.json").read_text())
    assert launched["argv"] == ["-s", "read-only", "-a", "never", "app-server"]
    assert launched["home"] == str(home / ".codex")


def test_rejected_token_falls_back_to_app_server(home):
    install_auth(home)
    install_codex_cli(home, {"result": load_json("codex", "app_server_result.json")})
    http = FakeHttp({("GET", USAGE): http_error(401, {"detail": {"code": "token_expired"}})})
    snap = CodexProvider().fetch(make_ctx(home, http=http))
    assert snap.source == "Codex app-server"
    assert len(http.calls) == 1


def test_app_server_auth_failure(home):
    install_auth(home)
    message = ("failed to fetch codex rate limits: GET https://chatgpt.com/backend-api/wham/usage"
               " failed: 401 Unauthorized; body={\"detail\": {\"code\": \"token_expired\"}}")
    install_codex_cli(home, {"error": {"code": -32603, "message": message}})
    with pytest.raises(AuthError, match="isn't signed in"):
        CodexProvider().fetch(make_ctx(home, now=NOW + timedelta(days=10)))


def test_keyring_login_without_auth_file_uses_app_server(home):
    install_codex_cli(home, {"result": load_json("codex", "app_server_result.json")})
    install_rollout(home, load_text("codex", "rollout_current.jsonl"))
    snap = CodexProvider().fetch(make_ctx(home))
    assert snap.source == "Session logs + Codex app-server"
    assert snap.plan == "Plus"


def test_api_key_login_has_no_plan_limits(home):
    install_auth(home, "auth_apikey.json")
    with pytest.raises(NotConfigured, match="API key"):
        CodexProvider().fetch(make_ctx(home))


def test_limit_reached_note_from_live_reading(home):
    install_auth(home)
    http = FakeHttp({("GET", USAGE): load_json("codex", "usage_team.json")})
    snap = CodexProvider().fetch(make_ctx(home, http=http))
    assert snap.message == "Workspace usage limit reached"
    assert snap.plan == "Team"


def test_unreadable_auth_file(home, monkeypatch):
    monkeypatch.setattr(codex.time, "sleep", lambda _s: None)
    write(home / ".codex/auth.json", "{oops")
    with pytest.raises(ProviderError, match="could not be read"):
        CodexProvider().fetch(make_ctx(home))


def test_codex_home_from_setting_and_environment(home):
    custom = home / "work-codex"
    install_rollout(home, "")  # default home exists but is empty
    write(custom / "sessions/2026/10/02/rollout-x.jsonl",
          load_text("codex", "rollout_current.jsonl"))
    provider = CodexProvider()
    assert provider.detect(make_ctx(home, env={"CODEX_HOME": str(custom)}))
    snap = provider.fetch(make_ctx(home, settings={"source": "logs", "home": str(custom)}))
    assert by_id(snap.windows)["session"].used_percent == 37.5


def test_not_configured(home):
    provider = CodexProvider()
    assert not provider.detect(make_ctx(home))
    with pytest.raises(NotConfigured):
        provider.fetch(make_ctx(home))
