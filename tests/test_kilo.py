import json

import pytest

from conftest import FakeHttp, http_error, load_json, load_text, make_ctx, write
from quotaglance.providers.base import AuthError, NotConfigured, ProviderError
from quotaglance.providers.kilo import BATCH_URL, KiloProvider, find_credentials, parse_batch

CLI_TOKEN = "kilo_fixture_eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyXzEyMyJ9.sig"
ORG_ID = "4f6c1e2a-8b3d-4c7e-9a10-2b3c4d5e6f70"
BALANCE_URL = "https://api.kilo.ai/api/profile/balance"


def test_credit_blocks_and_kilo_pass():
    (credits, kilo_pass), plan = parse_batch(load_json("kilo", "batch_pass.json"))
    assert plan == "Starter · Auto top-up: off"
    assert credits.used_percent == pytest.approx(32.2917, abs=1e-4)  # micro-USD blocks
    assert (credits.used, credits.limit, credits.unit) == (pytest.approx(7.75), 24, "USD")
    assert credits.detail == "$16.25 left"
    assert (kilo_pass.id, kilo_pass.label) == ("pass", "Kilo Pass")
    assert kilo_pass.used_percent == pytest.approx(26.6667, abs=1e-4)  # bonus counts too
    assert kilo_pass.resets_at.isoformat() == "2026-10-28T04:00:00+00:00"
    assert kilo_pass.detail == "$7.60 / $19.00 (+ $9.50 bonus)"


def test_superjson_wrapped_generic_keys():
    windows, plan = parse_batch(load_json("kilo", "batch_superjson.json"))
    assert plan == "Kilo Pass Pro · Auto top-up: visa"
    assert [w.id for w in windows] == ["credits"]
    assert (windows[0].used_percent, windows[0].unit) == (25, "credits")


def test_zero_balance_shows_as_used_up():
    (credits,), plan = parse_batch(load_json("kilo", "batch_zero.json"))
    assert plan == "Auto top-up: off"
    assert (credits.used_percent, credits.used, credits.limit) == (100, 0, 0)


def test_auto_top_up_amount_and_sparse_indexed_batch():
    root = {"0": {"result": {"data": {"creditBlocks": [], "totalBalance_mUsd": 0,
                                      "autoTopUpEnabled": True}}},
            "2": {"result": {"data": {"enabled": True, "amountCents": 5000,
                                      "paymentMethod": None}}}}
    windows, plan = parse_batch(root)
    assert plan == "Auto top-up: $50"
    assert [w.id for w in windows] == ["credits"]


def test_only_required_procedure_errors_fail():
    windows, plan = parse_batch(load_json("kilo", "batch_optional_error.json"))
    assert (plan, windows[0].used_percent) == ("Starter", 10)
    with pytest.raises(AuthError):
        parse_batch(load_json("kilo", "batch_unauthorized.json"))
    with pytest.raises(ProviderError):
        parse_batch("<html>")


def test_fetch_uses_the_cli_sign_in_with_its_organization(home):
    write(home / ".local/share/kilo/auth.json", load_text("kilo", "auth.json"))
    http = FakeHttp({("GET", BATCH_URL): load_json("kilo", "batch_pass.json")})
    snap = KiloProvider().fetch(make_ctx(home, http=http))
    headers = http.last(BATCH_URL)["headers"]
    assert headers["Authorization"] == f"Bearer {CLI_TOKEN}"
    assert headers["X-KILOCODE-ORGANIZATIONID"] == ORG_ID
    assert (snap.source, snap.plan) == ("Kilo CLI sign-in", "Starter · Auto top-up: off")
    assert BATCH_URL.endswith("?batch=1&input=%7B%220%22%3A%7B%22json%22%3Anull%7D%2C%221%22%3A"
                              "%7B%22json%22%3Anull%7D%2C%222%22%3A%7B%22json%22%3Anull%7D%7D")


def test_rejected_cli_token_falls_back_to_the_pasted_token(home):
    write(home / ".local/share/kilo/auth.json", load_text("kilo", "auth.json"))

    def respond(method, url, headers, body):
        if headers["Authorization"] == f"Bearer {CLI_TOKEN}":
            return http_error(401, {"error": "unauthorized"})
        return load_json("kilo", "batch_pass.json")

    http = FakeHttp({("GET", BATCH_URL): respond})
    snap = KiloProvider().fetch(make_ctx(home, http=http,
                                         secrets={("kilo", "api_key"): "kilo_pasted"}))
    headers = http.last(BATCH_URL)["headers"]
    assert headers["Authorization"] == "Bearer kilo_pasted"
    assert "X-KILOCODE-ORGANIZATIONID" not in headers
    assert snap.source == "Keyring"


def test_expired_cli_sign_in_is_never_sent(home):
    auth = load_json("kilo", "auth.json")
    auth["kilo"]["expires"] = 1_700_000_000_000
    write(home / ".local/share/kilo/auth.json", json.dumps(auth))
    http = FakeHttp()
    with pytest.raises(AuthError, match="expired") as info:
        KiloProvider().fetch(make_ctx(home, http=http))
    assert "kilo auth login" in info.value.hint
    assert http.calls == []


def test_moved_batch_endpoint_falls_back_to_the_balance(home):
    http = FakeHttp({("GET", BATCH_URL): http_error(404, b"not found"),
                     ("GET", BALANCE_URL): {"balance": 16.25}})
    snap = KiloProvider().fetch(make_ctx(home, http=http, env={"KILO_API_KEY": "kilo_env"}))
    (credits,) = snap.windows
    assert (credits.used, credits.unit, credits.used_percent) == (16.25, "USD", None)
    assert (snap.source, snap.plan) == ("$KILO_API_KEY", None)


def test_credential_sources(home):
    write(home / ".kilocode/cli/config.json", json.dumps({"providers": [
        {"provider": "kilocode", "kilocodeToken": "legacy", "kilocodeOrganizationId": "org_9"}]}))
    (legacy,) = find_credentials(make_ctx(home))
    assert (legacy.token, legacy.organization) == ("legacy", "org_9")
    write(home / ".local/share/kilo/auth.json",
          json.dumps({"kilo": {"type": "api", "key": "kilo_key"}}))
    found = find_credentials(make_ctx(home, env={"KILO_API_KEY": "kilo_env"}))
    assert [(c.token, c.origin) for c in found] == [("kilo_key", "Kilo CLI sign-in"),
                                                    ("kilo_env", "$KILO_API_KEY")]
    content = json.dumps({"kilo": {"type": "api", "key": "kilo_content"}})
    assert find_credentials(make_ctx(home, env={"KILO_AUTH_CONTENT": content}))[0].token == \
        "kilo_content"


def test_not_configured(home):
    provider = KiloProvider()
    ctx = make_ctx(home)
    assert provider.detect(ctx) is False
    with pytest.raises(NotConfigured):
        provider.fetch(ctx)
