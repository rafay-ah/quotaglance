import json

import pytest

from conftest import NOW, FakeHttp, http_error, load_json, make_ctx, write
from quotaglance.providers.base import AuthError, NotConfigured, ProviderError
from quotaglance.providers.poe import (
    BALANCE_URL,
    HISTORY_URL,
    PoeProvider,
    find_key,
    history_rows,
    parse_balance,
    summarize_history,
)


def _history(method, url, headers, body):
    if "starting_after=q_0c4d9e8f1a" in url:
        return load_json("poe", "history_page2.json")
    return load_json("poe", "history_page1.json")


def test_point_balance():
    window = parse_balance(load_json("poe", "balance.json"))
    assert (window.id, window.label, window.used_percent) == ("balance", "Points", None)
    assert (window.used, window.unit, window.detail) == (742300, "points", "742,300 points left")
    assert parse_balance({"current_point_balance": "1500"}).used == 1500
    assert parse_balance({}) is None
    with pytest.raises(ProviderError):
        parse_balance({"current_point_balance": "lots"})


def test_history_summary_today_and_thirty_days():
    rows = history_rows(load_json("poe", "history_page1.json")) + \
        history_rows(load_json("poe", "history_page2.json"))
    today, month = summarize_history(rows, NOW)
    assert (today.label, today.used, today.detail) == ("Today", 4520, "4,520 points · 2 requests")
    assert (month.label, month.used) == ("Last 30 days", 22780)
    assert month.detail == "22,780 points · 4 requests · top Veo-3.2"


def test_history_skips_unreadable_and_old_rows():
    rows = [{"creation_time": "soon", "cost_points": 5},
            {"creation_time": 1780000000000000, "cost_points": 900},  # older than 30 days
            {"timestamp": 1791027000000, "points": -40, "bot_name": "Bot"}]
    today, month = summarize_history(rows, NOW)
    assert (today.used, month.used) == (0, 0)
    assert month.detail == "0 points · 1 request · top Bot"


def test_fetch_pages_through_history(home):
    http = FakeHttp({("GET", BALANCE_URL): load_json("poe", "balance.json"),
                     ("GET", HISTORY_URL): _history})
    snap = PoeProvider().fetch(make_ctx(home, http=http, secrets={("poe", "api_key"): "pk"}))
    pages = [call["url"] for call in http.calls if call["url"].startswith(HISTORY_URL)]
    assert pages == [HISTORY_URL + "?limit=100",
                     HISTORY_URL + "?limit=100&starting_after=q_0c4d9e8f1a"]
    assert http.last(BALANCE_URL)["headers"]["Authorization"] == "Bearer pk"
    assert [w.id for w in snap.windows] == ["balance", "spend_today", "spend_30d"]
    assert (snap.plan, snap.source) == ("Points balance", "Keyring")


def test_history_failure_never_hides_the_balance(home):
    http = FakeHttp({("GET", BALANCE_URL): {"current_point_balance": 1500},
                     ("GET", HISTORY_URL): http_error(500)})
    snap = PoeProvider().fetch(make_ctx(home, http=http, env={"POE_API_KEY": "pk"}))
    assert [(w.id, w.detail) for w in snap.windows] == [("balance", "1,500 points left")]


def test_rejected_key_is_auth_error(home):
    http = FakeHttp({("GET", BALANCE_URL): http_error(401)})
    with pytest.raises(AuthError):
        PoeProvider().fetch(make_ctx(home, http=http, env={"POE_API_KEY": "pk"}))


def test_key_sources_and_not_configured(home):
    assert not PoeProvider().detect(make_ctx(home))
    with pytest.raises(NotConfigured):
        PoeProvider().fetch(make_ctx(home))
    write(home / ".local/share/opencode/auth.json",
          json.dumps({"poe": {"type": "api", "key": "pk_oc"}}))
    assert find_key(make_ctx(home)) == ("pk_oc", "OpenCode sign-in")
    assert find_key(make_ctx(home, env={"POE_API_KEY": "pk_env"})) == ("pk_env", "$POE_API_KEY")
