import json

import pytest

from conftest import FakeHttp, http_error, load_json, load_text, make_ctx, write
from quotaglance.models import Status
from quotaglance.providers.base import AuthError, NotConfigured, ProviderError, run_provider
from quotaglance.providers.zed import (
    LOGIN_HINT,
    ZedProvider,
    find_credentials,
    load_client_settings,
    parse_users_me,
    resolve_endpoints,
    strip_jsonc,
)

API_URL = "https://cloud.zed.dev/client/users/me"
ACCOUNT = ({"url": "https://zed.dev", "username": "583231",
            "xdg:schema": "org.freedesktop.Secret.Generic"}, "zed-access-token")


def test_pro_plan_edit_predictions_reset_with_the_billing_cycle():
    windows, plan, account = parse_users_me(load_json("zed", "users_me_pro.json"))
    assert (plan, account) == ("Pro", "octocat")
    [preds] = windows
    assert (preds.id, preds.label) == ("edit_predictions", "Edit preds")
    assert preds.used_percent == pytest.approx(20.6)
    assert (preds.used, preds.limit, preds.unit) == (412, 2000, "predictions")
    assert preds.resets_at.isoformat() == "2026-06-13T00:00:00+00:00"
    assert preds.window_seconds == 31 * 86400


def test_limited_object_form_on_free_plan_is_capped():
    windows, plan, account = parse_users_me(load_json("zed", "users_me_free.json"))
    assert (plan, account) == ("Free", "free-dev")
    [preds] = windows
    assert preds.used_percent == 100
    assert (preds.used, preds.limit) == (2000, 2000)
    assert preds.resets_at is None


def test_unlimited_trial_and_overdue_invoice():
    windows, plan, account = parse_users_me(load_json("zed", "users_me_trial_unlimited.json"))
    assert (plan, account) == ("Pro Trial", "dev")
    preds, billing = windows
    assert preds.used_percent is None
    assert (preds.detail, preds.used) == ("Unlimited", 10)
    assert preds.resets_at.isoformat() == "2026-06-13T00:00:00+00:00"
    assert (billing.id, billing.label, billing.detail) == ("billing", "Billing",
                                                           "Invoice overdue")
    assert billing.used_percent is None


@pytest.mark.parametrize("payload", [
    {"user": {"id": 1}, "plan": {"plan_v3": "zed_pro"}},
    {"user": {"id": 1}, "plan": {"usage": {"edit_predictions": {"used": 3, "limit": "lots"}}}},
    {"user": {"id": 1}, "plan": {"usage": {"edit_predictions": {"used": -1, "limit": 10}}}},
    {"plan": {"usage": {"edit_predictions": {"used": 1, "limit": 10}}}},
    [],
])
def test_drifted_responses_fail_closed(payload):
    with pytest.raises(ProviderError, match="Unexpected response"):
        parse_users_me(payload)


def test_jsonc_settings_select_the_staging_credentials(home):
    write(home / ".config/zed/settings.json", load_text("zed", "settings.json"))
    settings = load_client_settings(make_ctx(home))
    assert settings == {"server_url": "https://staging.zed.dev",
                        "credentials_url": "https://staging.zed.dev"}
    assert resolve_endpoints(settings) == ("https://staging.zed.dev", API_URL)
    cleaned = json.loads(strip_jsonc(load_text("zed", "settings.json")))
    assert cleaned["file_scan_exclusions"][-1] == "https://example.com//not-a-comment"


def test_endpoint_guard_never_forwards_credentials_cross_origin():
    assert resolve_endpoints({}) == ("https://zed.dev", API_URL)
    assert resolve_endpoints({"server_url": "https://zed.dev", "credentials_url": "https://x.io"}
                             ) == ("https://x.io", API_URL)
    assert resolve_endpoints({"server_url": "https://zed.corp.example/"}) == (
        "https://zed.corp.example/", "https://zed.corp.example/client/users/me")
    with pytest.raises(ProviderError, match="HTTPS"):
        resolve_endpoints({"server_url": "http://localhost:3000"})
    with pytest.raises(ProviderError, match="credentials_url"):
        resolve_endpoints({"server_url": "https://evil.example",
                           "credentials_url": "https://zed.dev"})


def test_keyring_item_needs_a_numeric_user_then_dev_credentials(home):
    other = ({"url": "https://zed.dev", "username": "Bearer"}, "sk-unrelated")
    ctx = make_ctx(home, foreign_secrets=[other, ACCOUNT])
    assert find_credentials(ctx, "https://zed.dev") == ("583231", "zed-access-token",
                                                        "Zed sign-in")
    write(home / ".config/zed/development_credentials", json.dumps(
        {"https://zed.dev": ["42", list(b"dev-token")]}))
    ctx = make_ctx(home, foreign_secrets=[other])
    assert find_credentials(ctx, "https://zed.dev") == ("42", "dev-token",
                                                        "Zed dev credentials")
    assert find_credentials(ctx, "https://staging.zed.dev") is None


def test_fetch_sends_user_id_and_token_to_the_cloud_api(home):
    (home / ".config/zed").mkdir(parents=True)
    http = FakeHttp({("GET", API_URL): load_json("zed", "users_me_pro.json")})
    ctx = make_ctx(home, http=http, foreign_secrets=[ACCOUNT])
    provider = ZedProvider()
    assert provider.detect(ctx) is True
    snap = provider.fetch(ctx)
    assert http.last(API_URL)["headers"]["Authorization"] == "583231 zed-access-token"
    assert snap.status is Status.OK
    assert (snap.plan, snap.account, snap.source) == ("Pro", "octocat", "Zed sign-in")


def test_custom_server_uses_its_own_credentials_and_host(home):
    write(home / ".config/zed/settings.json", '{"server_url": "https://zed.corp.example"}')
    url = "https://zed.corp.example/client/users/me"
    http = FakeHttp({("GET", url): load_json("zed", "users_me_free.json")})
    corp = ({"url": "https://zed.corp.example", "username": "9"}, "corp-token")
    snap = ZedProvider().fetch(make_ctx(home, http=http, foreign_secrets=[ACCOUNT, corp]))
    assert http.last(url)["headers"]["Authorization"] == "9 corp-token"
    assert snap.plan == "Free"


def test_untrusted_settings_are_refused_before_any_request(home):
    write(home / ".config/zed/settings.json", json.dumps(
        {"server_url": "https://evil.example", "credentials_url": "https://zed.dev"}))
    http = FakeHttp()
    with pytest.raises(ProviderError, match="credentials_url"):
        ZedProvider().fetch(make_ctx(home, http=http, foreign_secrets=[ACCOUNT]))
    assert http.calls == []


def test_rejected_token_is_an_auth_error(home):
    http = FakeHttp({("GET", API_URL): http_error(401, {"error": "unauthorized"})})
    snap, error = run_provider(ZedProvider(), make_ctx(home, http=http,
                                                       foreign_secrets=[ACCOUNT]))
    assert isinstance(error, AuthError)
    assert error.hint == LOGIN_HINT
    assert snap.status is Status.ERROR


def test_not_signed_in(home):
    provider = ZedProvider()
    assert provider.detect(make_ctx(home, foreign_secrets=[ACCOUNT])) is False  # no Zed config
    (home / ".config/zed").mkdir(parents=True)
    ctx = make_ctx(home)
    assert provider.detect(ctx) is False
    with pytest.raises(NotConfigured, match="Not signed in to Zed"):
        provider.fetch(ctx)
