import json
import urllib.parse

import pytest

from conftest import NOW, FakeHttp, http_error, jwt, load_json, make_ctx, write
from quotaglance.providers import gemini
from quotaglance.providers.base import AuthError, NotConfigured, ProviderError
from quotaglance.providers.gemini import (
    API,
    TOKEN_URL,
    GeminiProvider,
    parse_load_code_assist,
    parse_quota,
    resolve_plan,
)

LOAD = f"{API}:loadCodeAssist"
QUOTA = f"{API}:retrieveUserQuota"
ID_TOKEN = jwt({"email": "dev@example.com", "hd": "example.com"})
PERSONAL_ID_TOKEN = jwt({"email": "someone@gmail.com"})


@pytest.fixture(autouse=True)
def clean_caches():
    gemini.reset_caches()
    yield
    gemini.reset_caches()


def write_creds(home, *, expiry_ms, id_token=ID_TOKEN, refresh="1//0gFIXTURErefresh"):
    write(home / ".gemini/oauth_creds.json", json.dumps({
        "access_token": "ya29.cached", "token_type": "Bearer", "id_token": id_token,
        "expiry_date": expiry_ms, "refresh_token": refresh}))


def install_fake_cli(home):
    root = home / ".npm-global/lib/node_modules/@google/gemini-cli"
    write(root / "package.json", json.dumps({"name": "@google/gemini-cli", "version": "0.62.0"}))
    write(root / "bundle/chunk-ABC123.js",
          'var X = 1;\nvar OAUTH_CLIENT_ID = "681255809395-fixture.apps.googleusercontent.com";\n'
          'var OAUTH_CLIENT_SECRET = "FIXTURE-not-a-real-secret";\n')


def test_quota_families_take_lowest_fraction_with_counts():
    windows = parse_quota(load_json("gemini", "quota_standard.json"))
    by_id = {w.id: w for w in windows}
    assert [w.id for w in windows] == ["pro", "flash", "flash_lite"]
    assert by_id["pro"].used_percent == pytest.approx(20.8)
    assert by_id["pro"].limit == 1500 and by_id["pro"].used == 312
    assert by_id["flash"].used_percent == pytest.approx(6.35)
    assert by_id["flash"].resets_at.isoformat() == "2026-10-04T05:12:44+00:00"
    assert by_id["flash_lite"].used_percent == pytest.approx(0.5)


def test_fraction_only_buckets():
    windows = parse_quota(load_json("gemini", "quota_fractions.json"))
    assert [round(w.used_percent) for w in windows] == [40, 10, 20]
    assert all(w.limit is None for w in windows)


def test_duplicate_model_buckets_keep_the_lowest():
    windows = parse_quota(load_json("gemini", "quota_duplicates.json"))
    assert [(w.id, round(w.used_percent)) for w in windows] == [("flash", 60)]


def test_empty_buckets_is_an_error():
    with pytest.raises(ProviderError):
        parse_quota({"buckets": []})


def test_plan_resolution():
    paid = parse_load_code_assist(load_json("gemini", "load_paid_credits.json"))
    assert resolve_plan(paid, None) == "Gemini Code Assist in Google One AI Pro"
    assert paid["project"] == "cloudaicompanion-fixture-123"
    assert paid["credits"] == 1350
    standard = parse_load_code_assist(load_json("gemini", "load_standard.json"))
    assert resolve_plan(standard, None) == "Paid"
    free = parse_load_code_assist(load_json("gemini", "load_free_workspace.json"))
    assert resolve_plan(free, "example.com") == "Workspace"
    assert resolve_plan(free, None) == "Free"
    assert parse_load_code_assist(load_json("gemini", "load_shutdown.json"))["shutdown"]


def test_fetch_with_valid_token(home):
    write_creds(home, expiry_ms=(NOW.timestamp() + 3600) * 1000)
    http = FakeHttp({("POST", LOAD): load_json("gemini", "load_standard.json"),
                     ("POST", QUOTA): load_json("gemini", "quota_standard.json")})
    snap = GeminiProvider().fetch(make_ctx(home, http=http))
    assert http.last(QUOTA)["json"] == {"project": "acme-gca-prod-4821"}
    assert http.last(QUOTA)["headers"]["Authorization"] == "Bearer ya29.cached"
    assert snap.plan == "Paid"
    assert snap.account == "dev@example.com"
    assert snap.windows[0].id == "pro"


def test_expired_token_is_refreshed_in_memory_once(home):
    write_creds(home, expiry_ms=(NOW.timestamp() - 600) * 1000)
    install_fake_cli(home)
    http = FakeHttp({
        ("POST", TOKEN_URL): {"access_token": "ya29.fresh", "expires_in": 3599,
                              "id_token": ID_TOKEN},
        ("POST", LOAD): load_json("gemini", "load_paid_credits.json"),
        ("POST", QUOTA): load_json("gemini", "quota_fractions.json"),
    })
    provider = GeminiProvider()
    snap = provider.fetch(make_ctx(home, http=http))
    form = urllib.parse.parse_qs(http.last(TOKEN_URL)["data"].decode())
    assert form["client_id"] == ["681255809395-fixture.apps.googleusercontent.com"]
    assert form["refresh_token"] == ["1//0gFIXTURErefresh"]
    assert http.last(QUOTA)["headers"]["Authorization"] == "Bearer ya29.fresh"
    assert any(w.id == "credits" for w in snap.windows)
    provider.fetch(make_ctx(home, http=http))
    assert sum(1 for c in http.calls if c["url"] == TOKEN_URL) == 1
    # The CLI's own credentials file is never rewritten.
    assert json.loads((home / ".gemini/oauth_creds.json").read_text())["access_token"] == \
        "ya29.cached"


def test_revoked_refresh_token(home):
    write_creds(home, expiry_ms=1)
    install_fake_cli(home)
    http = FakeHttp({("POST", TOKEN_URL): http_error(400, {"error": "invalid_grant"})})
    with pytest.raises(AuthError):
        GeminiProvider().fetch(make_ctx(home, http=http))


def test_consumer_shutdown_is_explained(home):
    write_creds(home, expiry_ms=(NOW.timestamp() + 3600) * 1000, id_token=PERSONAL_ID_TOKEN)
    http = FakeHttp({("POST", LOAD): load_json("gemini", "load_shutdown.json")})
    with pytest.raises(ProviderError, match="personal accounts"):
        GeminiProvider().fetch(make_ctx(home, http=http))


def test_api_key_auth_has_no_quota(home):
    write(home / ".gemini/settings.json",
          json.dumps({"security": {"auth": {"selectedType": "gemini-api-key"}}}))
    with pytest.raises(NotConfigured, match="API key"):
        GeminiProvider().fetch(make_ctx(home))


def test_keyring_storage_mode(home):
    secret = json.dumps({"serverName": "main-account", "token": {
        "accessToken": "ya29.keyring", "refreshToken": "1//r",
        "expiresAt": (NOW.timestamp() + 3600) * 1000}})
    http = FakeHttp({("POST", LOAD): load_json("gemini", "load_standard.json"),
                     ("POST", QUOTA): load_json("gemini", "quota_fractions.json")})
    ctx = make_ctx(home, http=http, foreign_secrets=[
        ({"service": "gemini-cli-oauth", "account": "main-account"}, secret)])
    GeminiProvider().fetch(ctx)
    assert http.last(QUOTA)["headers"]["Authorization"] == "Bearer ya29.keyring"


def test_not_signed_in(home):
    provider = GeminiProvider()
    assert not provider.detect(make_ctx(home))
    with pytest.raises(NotConfigured):
        provider.fetch(make_ctx(home))
