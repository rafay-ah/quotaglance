import sqlite3
from pathlib import Path

import pytest

from conftest import NOW, FakeHttp, http_error, jwt, load_json, make_ctx
from quotaglance.models import Status
from quotaglance.net import HttpError, Response
from quotaglance.providers.base import AuthError, NotConfigured, run_provider
from quotaglance.providers.cursor import (
    API2_USAGE_URL,
    SUMMARY_URL,
    CursorProvider,
    decode_value,
    parse_current_period_usage,
    parse_usage_summary,
    session_cookie,
)

TOKEN = jwt({"sub": "auth0|user_01ABC", "exp": NOW.timestamp() + 3600,
             "email": "dev@example.com"})


def make_state_db(home: Path, token: str = TOKEN, as_blob: bool = False) -> Path:
    db = home / ".config/Cursor/User/globalStorage/state.vscdb"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    value = token.encode("utf-16-le") if as_blob else token
    conn.execute("INSERT INTO ItemTable VALUES (?, ?)", ("cursorAuth/accessToken", value))
    conn.execute("INSERT INTO ItemTable VALUES (?, ?)", ("cursorAuth/cachedEmail",
                                                         "dev@example.com"))
    conn.execute("INSERT INTO ItemTable VALUES (?, ?)", ("cursorAuth/stripeMembershipType",
                                                         "pro"))
    conn.commit()
    conn.close()
    return db


def test_pro_summary_maps_total_auto_api_and_on_demand():
    windows, plan = parse_usage_summary(load_json("cursor", "usage_summary_pro.json"))
    assert plan == "Pro"
    by_id = {w.id: w for w in windows}
    assert by_id["monthly"].used_percent == pytest.approx(40.6)
    assert by_id["monthly"].used == pytest.approx(8.12)
    assert by_id["monthly"].limit == pytest.approx(20.0)
    assert by_id["monthly"].unit == "USD"
    assert by_id["monthly"].resets_at.isoformat() == "2026-10-18T09:14:03+00:00"
    assert by_id["auto"].used_percent == pytest.approx(3.9)
    assert by_id["api"].used_percent == pytest.approx(1.2)
    assert by_id["on_demand"].used_percent == pytest.approx(3.5)
    assert by_id["on_demand"].limit == pytest.approx(100.0)


def test_enterprise_overall_is_the_headline():
    windows, plan = parse_usage_summary(load_json("cursor", "usage_summary_enterprise.json"))
    assert plan == "Enterprise"
    assert windows[0].id == "monthly"
    assert windows[0].used_percent == pytest.approx(61.2)
    assert [w.id for w in windows] == ["monthly"]


def test_team_on_demand_falls_back_to_team_pool():
    windows, plan = parse_usage_summary(load_json("cursor", "usage_summary_team_pool.json"))
    on_demand = next(w for w in windows if w.id == "on_demand")
    assert plan == "Team"
    assert on_demand.limit == pytest.approx(20000.0)
    assert on_demand.used_percent == pytest.approx(65.55625)


def test_hobby_uses_cents_ratio():
    windows, plan = parse_usage_summary(load_json("cursor", "usage_summary_hobby.json"))
    assert plan == "Hobby"
    assert windows[0].used_percent == 0.0
    assert windows[0].resets_at is None


def test_api2_strings_and_epoch_milliseconds():
    windows = parse_current_period_usage(load_json("cursor", "current_period_usage.json"))
    by_id = {w.id: w for w in windows}
    assert by_id["monthly"].used_percent == 90
    assert by_id["monthly"].resets_at.year == 2026
    assert by_id["on_demand"].used == pytest.approx(12.0)
    assert by_id["on_demand"].used_percent == pytest.approx(24.0)


def test_decode_value_handles_utf16_blobs():
    assert decode_value("abc.def.ghi".encode("utf-16-le")) == "abc.def.ghi"
    assert decode_value(b"abc.def") == "abc.def"
    assert decode_value("  token  ") == "token"
    assert decode_value(None) is None


def test_session_cookie_uses_user_id_after_pipe():
    cookie = session_cookie(TOKEN, NOW.timestamp())
    assert cookie == f"WorkosCursorSessionToken=user_01ABC%3A%3A{TOKEN}"


def test_expired_token_is_an_auth_error():
    expired = jwt({"sub": "auth0|u1", "exp": NOW.timestamp() + 30})
    with pytest.raises(AuthError):
        session_cookie(expired, NOW.timestamp())


def test_fetch_reads_state_db_and_sends_cookie(home):
    make_state_db(home, as_blob=True)
    http = FakeHttp({("GET", SUMMARY_URL): load_json("cursor", "usage_summary_pro.json")})
    snap = CursorProvider().fetch(make_ctx(home, http=http))
    assert snap.status is Status.OK
    assert snap.account == "dev@example.com"
    assert snap.plan == "Pro"
    cookie = http.last(SUMMARY_URL)["headers"]["Cookie"]
    assert "WorkosCursorSessionToken=user_01ABC%3A%3A" in cookie


def test_fetch_falls_back_to_api2_on_vercel_challenge(home):
    make_state_db(home)
    challenge = HttpError(403, SUMMARY_URL, b"<html>Vercel Security Checkpoint</html>",
                          {"Content-Type": "text/html"})
    http = FakeHttp({
        ("GET", SUMMARY_URL): challenge,
        ("POST", API2_USAGE_URL): load_json("cursor", "current_period_usage.json"),
    })
    snap = CursorProvider().fetch(make_ctx(home, http=http))
    assert snap.windows[0].used_percent == 90
    assert http.last(API2_USAGE_URL)["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert snap.plan == "Pro"


def test_rejected_session_is_reported_as_auth_error(home):
    make_state_db(home)
    http = FakeHttp({("GET", SUMMARY_URL): http_error(401, {"error": "not_authenticated"})})
    snap, error = run_provider(CursorProvider(), make_ctx(home, http=http))
    assert isinstance(error, AuthError)
    assert snap.status is Status.ERROR


def test_missing_install_is_not_configured(home):
    provider = CursorProvider()
    ctx = make_ctx(home)
    assert provider.detect(ctx) is False
    with pytest.raises(NotConfigured):
        provider.fetch(ctx)


def test_env_token_overrides_db(home):
    http = FakeHttp({("GET", SUMMARY_URL): Response(200, SUMMARY_URL,
                                                     b'{"membershipType":"ultra",'
                                                     b'"individualUsage":{"plan":'
                                                     b'{"totalPercentUsed":12}}}')})
    snap = CursorProvider().fetch(make_ctx(home, http=http, env={"CURSOR_ACCESS_TOKEN": TOKEN}))
    assert snap.plan == "Ultra"
    assert snap.windows[0].used_percent == 12


def test_reading_a_closed_wal_database_leaves_no_sidecar_files(home):
    db = make_state_db(home)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    before = sorted(p.name for p in db.parent.iterdir())
    from quotaglance.providers.cursor import read_state_values

    assert read_state_values(db, ["cursorAuth/accessToken"])
    assert sorted(p.name for p in db.parent.iterdir()) == before
