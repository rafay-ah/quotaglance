import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import FakeHttp, http_error, load_json, load_text, make_ctx, write
from quotaglance.providers.base import AuthError, NotConfigured, ProviderError
from quotaglance.providers.clinepass import (
    URL,
    ClinePassProvider,
    parse_providers,
    parse_usage_limits,
    providers_path,
)

SESSION_NOW = datetime(2026, 10, 3, 7, 12, tzinfo=timezone.utc)  # before the 07:15 expiry
SESSION_FILE = ".cline/data/settings/providers.json"


def test_five_hour_weekly_and_monthly_windows():
    session, weekly, monthly = parse_usage_limits(load_json("clinepass", "usage_limits.json"))
    assert (session.id, session.label, session.used_percent) == ("session", "5-hour", 12.5)
    assert session.resets_at.isoformat() == "2026-10-03T12:20:30+00:00"
    assert (session.window_seconds, weekly.window_seconds, monthly.window_seconds) == (
        5 * 3600, 7 * 86400, 30 * 86400)
    assert (weekly.label, weekly.used_percent) == ("Weekly", 34)
    assert (monthly.label, monthly.used_percent) == ("Monthly", 56.75)


def test_unknown_pools_null_resets_partial_lists_and_clamping():
    windows = parse_usage_limits(load_json("clinepass", "usage_unknown_pool.json"))
    assert [w.id for w in windows] == ["session", "weekly", "monthly"]
    assert windows[2].resets_at is None
    (weekly,) = parse_usage_limits({"success": True, "data": {"limits": [
        {"type": "weekly", "percentUsed": 25}]}})
    assert weekly.id == "weekly"
    (over,) = parse_usage_limits({"success": True, "data": {"limits": [
        {"type": "five_hour", "percentUsed": 104.2, "resetsAt": "2026-10-03T09:00:00Z"}]}})
    assert over.used_percent == 100


@pytest.mark.parametrize("payload", [
    {"data": {"limits": [{"type": "weekly", "percentUsed": "forty"}]}, "success": True},
    {"success": False, "error": "Plan not found"},
    {"success": "yes", "data": {}},
    {"success": True, "data": {"limits": {}}},
    {"success": True, "data": {"limits": [{"type": 5, "percentUsed": 1}]}},
    {"success": True, "data": {"limits": [{"type": "weekly", "percentUsed": 1,
                                           "resetsAt": 12345}]}},
    ["not", "an", "object"],
])
def test_invalid_payloads_are_rejected(payload):
    with pytest.raises(ProviderError):
        parse_usage_limits(payload)


def test_credential_extraction_mirrors_cline():
    data = load_json("clinepass", "providers.json")
    credential = parse_providers(data)
    assert credential.oauth and credential.expires_ms == 1791011700000
    auth = data["providers"]["cline"]["settings"]["auth"]
    auth["accessToken"] = auth["accessToken"].removeprefix("workos:")
    del auth["expiresAt"]
    credential = parse_providers(data)
    assert credential.token.startswith("workos:eyJ")  # Cline sends OAuth tokens prefixed
    assert credential.expires_ms == 1791011700000  # taken from the JWT's exp instead
    data["providers"]["cline"]["settings"] = {"provider": "cline", "apiKey": "sk-cline"}
    credential = parse_providers(data)
    assert (credential.token, credential.oauth) == ("sk-cline", False)
    assert parse_providers({"providers": {"cline-pass": {"settings": {"apiKey": "x"}}}}) is None


def test_providers_file_lookup_order(home):
    assert providers_path(make_ctx(home)) == home / SESSION_FILE
    assert providers_path(make_ctx(home, env={"CLINE_DIR": "~/alt"})) == \
        home / "alt/data/settings/providers.json"
    assert providers_path(make_ctx(home, env={"CLINE_DATA_DIR": "/srv/cline",
                                              "CLINE_DIR": "~/alt"})) == \
        Path("/srv/cline/settings/providers.json")
    assert providers_path(make_ctx(home, env={"CLINE_PROVIDER_SETTINGS_PATH": "~/p.json",
                                              "CLINE_DATA_DIR": "/srv"})) == home / "p.json"


def test_fresh_sign_in_is_used_read_only(home):
    session = write(home / SESSION_FILE, load_text("clinepass", "providers.json"))
    http = FakeHttp({("GET", URL): load_json("clinepass", "usage_limits.json")})
    snap = ClinePassProvider().fetch(make_ctx(home, http=http, now=SESSION_NOW))
    token = load_json("clinepass", "providers.json")["providers"]["cline"]["settings"]["auth"][
        "accessToken"]
    assert http.last(URL)["headers"]["Authorization"] == f"Bearer {token}"
    assert (snap.plan, snap.source) == ("ClinePass", "Cline sign-in")
    assert session.read_text(encoding="utf-8") == load_text("clinepass", "providers.json")


def test_expired_sign_in_is_never_sent(home):
    write(home / SESSION_FILE, load_text("clinepass", "providers.json"))
    http = FakeHttp()
    with pytest.raises(AuthError, match="expired") as info:
        ClinePassProvider().fetch(make_ctx(home, http=http))
    assert "cline auth" in info.value.hint
    assert http.calls == []


def test_expired_sign_in_falls_back_to_the_api_key(home):
    write(home / SESSION_FILE, load_text("clinepass", "providers.json"))
    http = FakeHttp({("GET", URL): load_json("clinepass", "usage_limits.json")})
    snap = ClinePassProvider().fetch(make_ctx(home, http=http,
                                              env={"CLINEPASS_API_KEY": "cline_key"}))
    assert http.last(URL)["headers"]["Authorization"] == "Bearer cline_key"  # no prefix
    assert snap.source == "$CLINEPASS_API_KEY"


def test_rejected_key_is_auth_error(home):
    http = FakeHttp({("GET", URL): http_error(403, {"success": False, "error": "forbidden"})})
    ctx = make_ctx(home, http=http, secrets={("clinepass", "api_key"): "cline_key"})
    with pytest.raises(AuthError) as info:
        ClinePassProvider().fetch(ctx)
    assert "API key" in info.value.hint


def test_not_configured(home):
    provider = ClinePassProvider()
    write(home / SESSION_FILE, json.dumps({"version": 1, "providers": {}}))
    ctx = make_ctx(home)
    assert provider.detect(ctx) is False
    with pytest.raises(NotConfigured):
        provider.fetch(ctx)
