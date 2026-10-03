import json
import sqlite3
from datetime import timezone

import pytest

from conftest import NOW, FakeHttp, http_error, load_json, load_text, make_ctx, write
from quotaglance.net import NetworkError
from quotaglance.providers.base import AuthError, NotConfigured
from quotaglance.providers.opencode import (
    GO_USAGE_URL,
    OpenCodeProvider,
    estimate_go_windows,
    parse_go_usage,
    read_rows,
    zen_spend_windows,
)


def make_db(home):
    path = home / ".local/share/opencode/opencode.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(load_text("opencode", "history.sql"))
    conn.commit()
    conn.close()
    return path


def write_auth(home, entries):
    write(home / ".local/share/opencode/auth.json", json.dumps(entries))


def test_go_usage_windows():
    windows = parse_go_usage(load_json("opencode", "go_usage.json"), NOW)
    assert [(w.id, w.used_percent) for w in windows] == [
        ("session", 37), ("weekly", 58), ("monthly", 21)]
    assert windows[0].resets_at.isoformat() == "2026-10-03T14:12:09+00:00"


def test_rate_limited_is_full_and_missing_monthly_is_omitted():
    windows = parse_go_usage(load_json("opencode", "go_usage_limited.json"), NOW)
    assert [(w.id, w.used_percent) for w in windows] == [("session", 100), ("weekly", 83)]


def test_idle_window_and_reset_in_seconds_without_fraction_scaling():
    windows = parse_go_usage(load_json("opencode", "go_usage_idle_seconds.json"), NOW)
    session, weekly, _monthly = windows
    assert session.resets_at is None and session.detail == "Not started"
    assert weekly.used_percent == 0.5
    assert weekly.resets_at.isoformat() == "2026-10-06T14:01:40+00:00"


def test_local_rows_prefer_step_parts_and_skip_bad_rows(home):
    db = make_db(home)
    zen = read_rows(db, "opencode", 0)
    assert sorted(round(cost, 4) for _, cost, _ in zen) == [0.042, 0.0421, 1.25, 9.99]


def test_zen_spend_windows(home):
    db = make_db(home)
    rows = read_rows(db, "opencode", int((NOW.timestamp() - 62 * 86400) * 1000))
    today, week, month = zen_spend_windows(rows, NOW, tz=timezone.utc)
    assert today.used == pytest.approx(0.0841)
    assert week.used == pytest.approx(1.3341)
    assert month.used == pytest.approx(1.3341)
    assert today.used_percent is None and today.unit == "USD"


def test_go_estimate_matches_reference_numbers(home):
    db = make_db(home)
    windows = estimate_go_windows(read_rows(db, "opencode-go", 0), NOW)
    session, weekly, monthly = windows
    assert session.used == pytest.approx(2.321)
    assert session.used_percent == pytest.approx(19.3)
    assert session.resets_at.isoformat() == "2026-10-03T13:05:00+00:00"
    assert weekly.used == pytest.approx(11.491)
    assert weekly.resets_at.isoformat() == "2026-10-05T00:00:00+00:00"
    assert monthly.used == pytest.approx(15.95)
    assert monthly.resets_at.isoformat() == "2026-10-21T15:00:00+00:00"


def test_fetch_prefers_api_with_key_from_auth_json(home):
    write_auth(home, {"opencode-go": {"type": "api", "key": "oc_sk_go"}})
    http = FakeHttp({("GET", GO_USAGE_URL): load_json("opencode", "go_usage.json")})
    snap = OpenCodeProvider().fetch(make_ctx(home, http=http))
    assert http.last(GO_USAGE_URL)["headers"]["Authorization"] == "Bearer oc_sk_go"
    assert snap.plan == "Go" and snap.source == "OpenCode Go API"
    assert [w.id for w in snap.windows] == ["session", "weekly", "monthly"]


def test_network_failure_falls_back_to_local_estimate(home):
    make_db(home)
    write_auth(home, {"opencode-go": {"type": "api", "key": "oc_sk_go"},
                      "opencode": {"type": "api", "key": "sk-zen"}})
    http = FakeHttp({("GET", GO_USAGE_URL): NetworkError("offline")})
    snap = OpenCodeProvider().fetch(make_ctx(home, http=http))
    assert snap.source == "Local history (estimate)"
    ids = [w.id for w in snap.windows]
    assert ids[:3] == ["session", "weekly", "monthly"] and "spend_30d" in ids


def test_entitlement_error_shows_zen_spend(home):
    make_db(home)
    write_auth(home, {"opencode": {"type": "api", "key": "sk-zen"}})
    http = FakeHttp({("GET", GO_USAGE_URL): http_error(403, {"type": "error", "error": {
        "type": "EntitlementError", "message": "OpenCode Go subscription required."}})})
    # Without Go rows in this DB copy the estimate is skipped; keep only Zen rows.
    db = home / ".local/share/opencode/opencode.db"
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM message WHERE json_valid(data) AND "
                 "json_extract(data,'$.providerID')='opencode-go'")
    conn.commit()
    conn.close()
    snap = OpenCodeProvider().fetch(make_ctx(home, http=http))
    assert snap.plan == "Zen"
    assert [w.id for w in snap.windows] == ["today", "spend_7d", "spend_30d"]
    assert snap.message == "No OpenCode Go subscription on this account"


def test_explicit_key_rejected_is_auth_error(home):
    make_db(home)
    http = FakeHttp({("GET", GO_USAGE_URL): http_error(401, {"error": {"type": "AuthError"}})})
    ctx = make_ctx(home, http=http, env={"OPENCODE_API_KEY": "oc_sk_bad"})
    with pytest.raises(AuthError):
        OpenCodeProvider().fetch(ctx)


def test_not_configured(home):
    provider = OpenCodeProvider()
    assert not provider.detect(make_ctx(home))
    with pytest.raises(NotConfigured):
        provider.fetch(make_ctx(home))
