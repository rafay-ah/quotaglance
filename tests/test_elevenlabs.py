import pytest

from conftest import FakeHttp, http_error, load_json, make_ctx
from quotaglance.providers.base import AuthError, NotConfigured, ProviderError
from quotaglance.providers.elevenlabs import (
    ElevenLabsProvider,
    parse_subscription,
    subscription_url,
)

URL = "https://api.elevenlabs.io/v1/user/subscription"


def test_creator_credits_and_voice_slots():
    windows, plan = parse_subscription(load_json("elevenlabs", "creator.json"))
    credits, voices = windows
    assert plan == "Creator"
    assert credits.used_percent == pytest.approx(61.234)
    assert credits.used == 61234 and credits.limit == 100000
    assert credits.resets_at.isoformat() == "2026-10-14T17:30:00+00:00"
    assert voices.used_percent == pytest.approx(23.333, rel=1e-3)


def test_free_tier_exhausted_and_no_pro_voice_window():
    windows, plan = parse_subscription(load_json("elevenlabs", "free_exhausted.json"))
    assert plan == "Free"
    assert windows[0].used_percent == 100
    assert {w.id for w in windows} == {"credits", "voice_slots"}


def test_overage_can_exceed_one_hundred_percent():
    windows, plan = parse_subscription(load_json("elevenlabs", "pro_overage.json"))
    assert windows[0].used_percent == pytest.approx(104.68)
    assert windows[0].detail == "Overage $4.68"
    assert plan == "Pro"


def test_status_suffix_and_missing_reset():
    windows, plan = parse_subscription(load_json("elevenlabs", "business_past_due.json"))
    assert plan == "Growing Business · past due"
    assert windows[0].used_percent == 60
    assert windows[0].resets_at is None


def test_rejects_non_integer_counts():
    with pytest.raises(ProviderError):
        parse_subscription({"character_count": "12", "character_limit": 100})
    with pytest.raises(ProviderError):
        parse_subscription([])


def test_fetch_uses_keyring_key_and_header(home):
    http = FakeHttp({("GET", URL): load_json("elevenlabs", "creator.json")})
    ctx = make_ctx(home, http=http, secrets={("elevenlabs", "api_key"): "sk_test"})
    snap = ElevenLabsProvider().fetch(ctx)
    assert http.last(URL)["headers"]["xi-api-key"] == "sk_test"
    assert snap.windows[0].id == "credits"


def test_env_key_and_region(home):
    ctx = make_ctx(home, env={"XI_API_KEY": "sk_env"}, settings={"region": "eu"})
    assert ElevenLabsProvider().api_key(ctx) == "sk_env"
    assert subscription_url(ctx) == "https://api.eu.residency.elevenlabs.io/v1/user/subscription"
    override = make_ctx(home, env={"ELEVENLABS_API_URL": "https://proxy.example/v1/"})
    assert subscription_url(override) == "https://proxy.example/v1/user/subscription"


def test_permission_error_is_auth_error(home):
    body = {"detail": {"status": "missing_permissions", "message": "missing user_read"}}
    http = FakeHttp({("GET", URL): http_error(401, body)})
    ctx = make_ctx(home, http=http, env={"ELEVENLABS_API_KEY": "sk"})
    with pytest.raises(AuthError, match="Read permission"):
        ElevenLabsProvider().fetch(ctx)


def test_no_key(home):
    with pytest.raises(NotConfigured):
        ElevenLabsProvider().fetch(make_ctx(home))
