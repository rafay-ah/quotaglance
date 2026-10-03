import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import FakeHttp, http_error, load_json, load_text, make_ctx
from quotaglance.models import Status
from quotaglance.providers.base import AuthError, NotConfigured, ProviderError, run_provider
from quotaglance.providers.windsurf import (
    LOGIN_HINT,
    STATUS_URLS,
    WindsurfProvider,
    decode_json,
    find_field,
    parse_cached_plan_info,
    parse_user_status,
)

AUTH = load_text("windsurf", "auth_status.json")
CACHE = load_text("windsurf", "cached_plan_full.json")
KEY = "sk-ws-01-EXAMPLEapiKEY0123456789"
AUTH_KEY = "windsurfAuthStatus"
PLAN_KEY = "windsurf.settings.cachedPlanInfo"


def make_state_db(home: Path, values: dict, folder: str = "Windsurf") -> Path:
    db = home / ".config" / folder / "User/globalStorage/state.vscdb"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")  # like VS Code-based editors
    conn.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    for key, value in values.items():
        conn.execute("INSERT INTO ItemTable VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()
    return db


def test_cached_quota_usage_maps_daily_and_weekly():
    windows, plan = parse_cached_plan_info(load_json("windsurf", "cached_plan_full.json"))
    assert plan == "Pro"
    assert [w.id for w in windows] == ["daily", "weekly"]  # counters ignored when quota exists
    daily, weekly = windows
    assert (daily.label, daily.used_percent) == ("Daily", 91)
    assert daily.resets_at == datetime.fromtimestamp(1774080000, timezone.utc)
    assert weekly.used_percent == 46
    assert weekly.window_seconds == 7 * 86400


def test_cached_counters_are_hundredths_of_credits():
    windows, plan = parse_cached_plan_info(load_json("windsurf", "cached_plan_counters.json"))
    assert plan == "Pro"
    assert [w.id for w in windows] == ["prompts", "flow_actions", "flex"]
    prompts, flow, flex = windows
    assert prompts.used_percent == pytest.approx(71.3)
    assert (prompts.used, prompts.limit, prompts.unit) == (356.5, 500, "credits")
    assert flow.used_percent == 0
    assert flex.used_percent == pytest.approx(25.0)
    assert flex.limit == 1237
    assert all(len(w.label) <= 12 for w in windows)


def test_cached_counters_infer_used_and_minimal_caches_have_no_windows():
    windows, _plan = parse_cached_plan_info(
        {"planName": "Pro", "usage": {"messages": 100, "remainingMessages": 25}})
    assert [(w.id, w.used_percent) for w in windows] == [("prompts", 75.0)]
    assert parse_cached_plan_info({"planName": "Free"}) == ([], "Free")
    assert parse_cached_plan_info({"planName": "team"})[1] == "Teams"
    assert parse_cached_plan_info(None) == ([], None)


def test_user_status_nested_quota_resets_and_overage():
    windows, plan, email = parse_user_status(load_json("windsurf", "user_status.json"))
    assert (plan, email) == ("Pro", "dev@example.com")
    by_id = {w.id: w for w in windows}
    assert by_id["daily"].used_percent == 20
    assert by_id["daily"].resets_at.isoformat() == "2026-10-04T00:00:00+00:00"
    assert by_id["weekly"].used_percent == 45
    assert by_id["weekly"].resets_at.isoformat() == "2026-10-08T00:00:00+00:00"
    assert by_id["overage"].used_percent is None
    assert by_id["overage"].detail == "$964.22 left"


def test_user_status_flat_snake_case_with_string_percents():
    windows, plan, email = parse_user_status(load_json("windsurf", "user_status_flat.json"))
    assert (plan, email) == ("Teams", "lead@example.com")
    daily, weekly = windows
    assert daily.used_percent == 100
    assert daily.resets_at.isoformat() == "2026-10-04T00:00:00+00:00"
    assert weekly.used_percent == pytest.approx(62.5)
    assert weekly.resets_at is None


def test_values_decode_from_utf16_blobs_and_nested_keys_are_found():
    assert decode_json('{"planName":"Pro é"}'.encode("utf-16-le")) == {"planName": "Pro é"}
    assert decode_json(b'{"a": 1}') == {"a": 1}
    assert decode_json("not json") is None
    nested = {"email": "a@example.com", "session": {"account": {"api_key": "sk-nested"}}}
    assert find_field(nested, ("apiKey", "api_key")) == "sk-nested"
    assert find_field({"apiKey": "  "}, ("apiKey",)) is None


def test_fetch_reads_key_read_only_and_posts_get_user_status(home):
    db = make_state_db(home, {AUTH_KEY: AUTH.encode("utf-16-le"), PLAN_KEY: CACHE})
    before = db.read_bytes()
    http = FakeHttp({("POST", STATUS_URLS[0]): load_json("windsurf", "user_status.json")})
    snap = WindsurfProvider().fetch(make_ctx(home, http=http))
    call = http.last(STATUS_URLS[0])
    assert call["headers"]["Authorization"] == f"Bearer {KEY}"
    assert call["headers"]["Connect-Protocol-Version"] == "1"
    assert call["json"] == {"metadata": {"apiKey": KEY, "ideName": "windsurf"}}
    assert snap.status is Status.OK
    assert (snap.plan, snap.account, snap.source) == ("Pro", "dev@example.com", "Windsurf API")
    assert [w.id for w in snap.windows] == ["daily", "weekly", "overage"]
    assert db.read_bytes() == before
    assert os.listdir(db.parent) == ["state.vscdb"]  # no -wal/-shm sidecars left behind


def test_falls_back_to_codeium_host_on_404(home):
    make_state_db(home, {AUTH_KEY: AUTH})
    http = FakeHttp({
        ("POST", STATUS_URLS[0]): http_error(404, {"code": "not_found"}),
        ("POST", STATUS_URLS[1]): load_json("windsurf", "user_status_flat.json"),
    })
    snap = WindsurfProvider().fetch(make_ctx(home, http=http))
    assert [c["url"] for c in http.calls] == list(STATUS_URLS)
    assert snap.windows[0].used_percent == 100


def test_rejected_key_never_tries_codeium_and_shows_cache_as_stale(home):
    make_state_db(home, {AUTH_KEY: AUTH, PLAN_KEY: CACHE})
    http = FakeHttp({("POST", STATUS_URLS[0]): http_error(401, {"code": "unauthenticated"})})
    snap = WindsurfProvider().fetch(make_ctx(home, http=http))
    assert len(http.calls) == 1
    assert snap.status is Status.STALE
    assert snap.source == "Windsurf app cache"
    assert "401" in snap.message and snap.hint == LOGIN_HINT
    assert [w.used_percent for w in snap.windows] == [91, 46]


def test_rejected_key_without_cache_is_an_auth_error(home):
    make_state_db(home, {AUTH_KEY: AUTH})
    http = FakeHttp({("POST", STATUS_URLS[0]): http_error(403, {"code": "permission_denied"})})
    snap, error = run_provider(WindsurfProvider(), make_ctx(home, http=http))
    assert isinstance(error, AuthError)
    assert snap.status is Status.ERROR


def test_server_trouble_stays_transient_even_with_a_cache(home):
    make_state_db(home, {AUTH_KEY: AUTH, PLAN_KEY: CACHE})
    http = FakeHttp({("POST", STATUS_URLS[0]): http_error(503),
                     ("POST", STATUS_URLS[1]): http_error(502)})
    with pytest.raises(ProviderError) as info:
        WindsurfProvider().fetch(make_ctx(home, http=http))
    assert info.value.transient


def test_signed_out_app_uses_the_offline_cache(home):
    db = make_state_db(home, {PLAN_KEY: CACHE.encode()}, folder="Windsurf - Next")
    os.utime(db, (1_790_000_000, 1_790_000_000))
    http = FakeHttp()
    snap = WindsurfProvider().fetch(make_ctx(home, http=http))
    assert http.calls == []
    assert snap.status is Status.OK
    assert snap.source == "Windsurf app cache"
    assert snap.observed_at == datetime.fromtimestamp(1_790_000_000, timezone.utc)
    assert snap.plan == "Pro"


def test_sees_wal_writes_while_windsurf_is_running(home):
    db = make_state_db(home, {AUTH_KEY: "{}"})
    writer = sqlite3.connect(db)  # Windsurf still open: its writes sit in the -wal file
    writer.execute("INSERT INTO ItemTable VALUES (?, ?)", (PLAN_KEY, CACHE))
    writer.commit()
    try:
        assert (db.parent / "state.vscdb-wal").exists()
        snap = WindsurfProvider().fetch(make_ctx(home))
        assert [w.used_percent for w in snap.windows] == [91, 46]
    finally:
        writer.close()


def test_missing_install_or_sign_in_is_not_configured(home):
    provider = WindsurfProvider()
    ctx = make_ctx(home)
    assert provider.detect(ctx) is False
    with pytest.raises(NotConfigured):
        provider.fetch(ctx)
    make_state_db(home, {"workbench.panel": "{}"})
    assert provider.detect(ctx) is True
    with pytest.raises(NotConfigured, match="Not signed in"):
        provider.fetch(ctx)
