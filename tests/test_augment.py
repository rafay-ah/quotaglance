import json
import subprocess

import pytest

from conftest import FakeHttp, FakeRunner, http_error, load_json, load_text, make_ctx, write
from quotaglance.providers.augment import (
    AugmentProvider,
    billing_url,
    build_windows,
    find_session,
    parse_billing_summary,
    parse_status_output,
    parse_status_text,
)
from quotaglance.providers.base import AuthError, NotConfigured, ProviderError

BILLING_URL = "https://d16.api.augmentcode.com/get-billing-summary"
TOKEN = "aug_fixture_0123456789abcdef0123456789abcdef0123456789abcdef"


class ArgvRunner(FakeRunner):
    """Answers by arguments, so `--json` and plain `account status` can differ."""

    def __init__(self, answers):
        super().__init__()
        self.answers = answers
        self.kwargs = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.kwargs.append(kwargs)
        code, out, err = self.answers[tuple(argv[1:])]
        return subprocess.CompletedProcess(argv, code, out, err)


def make_cli(home):
    auggie = write(home / ".npm-global/bin/auggie", "#!/usr/bin/env node\n")
    auggie.chmod(0o755)
    return auggie


def test_billing_summary_credits_plan():
    windows, plan = parse_billing_summary(load_json("augment", "billing_credits.json"))
    assert plan == "Max Plan"
    (credits,) = windows
    assert (credits.id, credits.label, credits.unit) == ("credits", "Credits", "credits")
    assert credits.used_percent == pytest.approx(36.1311, abs=1e-4)
    assert (credits.used, credits.limit) == (162590, 450000)
    assert credits.resets_at.isoformat() == "2026-10-12T00:00:00+00:00"
    assert credits.detail == "287,410 credits left"


def test_billing_summary_usd_plan_and_balance_above_allowance():
    windows, plan = parse_billing_summary(load_json("augment", "billing_usd_banner.json"))
    assert plan == "Indie Plan"
    (included,) = windows
    assert (included.label, included.unit, included.detail) == ("Included", "USD", "$12.48 left")
    assert included.used_percent == pytest.approx(37.6)
    assert included.resets_at.isoformat() == "2026-10-04T00:00:00+00:00"
    (topped_up,) = build_windows(500_000, 450_000, "credits", None)  # top-ups or rollover
    assert topped_up.used_percent == 0
    assert "above the monthly allowance" in topped_up.detail


@pytest.mark.parametrize("name", ["status.json", "status_credits.txt"])
def test_cli_json_and_text_box_match_the_api(name):
    output = load_text("augment", name).replace("287,410", "\x1b[1m287,410\x1b[22m")
    windows, plan = parse_status_output(output)
    assert plan == "Max Plan"
    assert (windows[0].used, windows[0].limit, windows[0].unit) == (162590, 450000, "credits")
    assert windows[0].resets_at.isoformat() == "2026-10-12T00:00:00+00:00"


def test_text_box_usd_plan_with_banner():
    windows, plan = parse_status_output(load_text("augment", "status_usd.txt"))
    assert plan == "Indie Plan"
    assert (windows[0].label, windows[0].unit) == ("Included", "USD")
    assert windows[0].used_percent == pytest.approx(37.6)
    assert windows[0].resets_at.isoformat() == "2026-10-04T00:00:00+00:00"
    no_plan = load_text("augment", "status_usd.txt").replace("Indie Plan", "          ")
    assert parse_status_text(no_plan)[1] is None  # the box border is not a plan name


def test_legacy_text_and_locale_grouping():
    windows, plan = parse_status_text(load_text("augment", "status_legacy.txt"))
    assert plan == "Max Plan"
    assert windows[0].used_percent == pytest.approx(98.7918, abs=1e-4)  # used of total
    assert (windows[0].used, windows[0].limit) == (953170, 964827)
    assert windows[0].resets_at.isoformat() == "2026-01-08T00:00:00+00:00"
    german = load_text("augment", "status_credits.txt").replace("287,410", "287.410") \
        .replace("450,000", "450.000")
    assert parse_status_text(german)[0][0].used == 162590


def test_fetch_prefers_auggie_json(home):
    auggie = make_cli(home)
    write(home / ".augment/session.json", load_text("augment", "session.json"))
    runner = ArgvRunner({("account", "status", "--json"): (0, load_text("augment", "status.json"),
                                                           "")})
    http = FakeHttp()
    snap = AugmentProvider().fetch(make_ctx(home, http=http, runner=runner))
    assert runner.calls == [[str(auggie), "account", "status", "--json"]]
    env = runner.kwargs[0]["env"]
    assert env["LC_ALL"] == "en_US.UTF-8"
    assert env["PATH"].split(":")[0] == str(auggie.parent)  # node usually lives beside it
    assert (snap.plan, snap.source) == ("Max Plan", "auggie CLI")
    assert http.calls == []


def test_old_auggie_without_json_falls_back_to_text(home):
    make_cli(home)
    runner = ArgvRunner({
        ("account", "status", "--json"): (1, "", "error: unknown option '--json'\n"),
        ("account", "status"): (0, load_text("augment", "status_credits.txt"), ""),
    })
    snap = AugmentProvider().fetch(make_ctx(home, runner=runner))
    assert [call[1:] for call in runner.calls] == [["account", "status", "--json"],
                                                   ["account", "status"]]
    assert snap.windows[0].limit == 450000


def test_broken_cli_falls_back_to_the_session_api(home):
    make_cli(home)
    write(home / ".augment/session.json", load_text("augment", "session.json"))
    runner = FakeRunner({"auggie": (127, "", "/usr/bin/env: 'node': No such file or directory")})
    http = FakeHttp({("POST", BILLING_URL): load_json("augment", "billing_credits.json")})
    snap = AugmentProvider().fetch(make_ctx(home, http=http, runner=runner))
    call = http.last(BILLING_URL)
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert call["headers"]["x-request-id"] != call["headers"]["x-request-session-id"]
    assert call["json"] == {}
    assert (snap.source, snap.windows[0].used) == ("Augment API", 162590)


def test_rejected_session_is_auth_error_and_the_file_is_kept(home):
    session = write(home / ".augment/session.json", load_text("augment", "session.json"))
    http = FakeHttp({("POST", BILLING_URL): http_error(401, {"error": "unauthenticated"})})
    with pytest.raises(AuthError) as info:
        AugmentProvider().fetch(make_ctx(home, http=http))
    assert "auggie login" in info.value.hint
    assert session.read_text(encoding="utf-8") == load_text("augment", "session.json")


def test_signed_out_cli_is_auth_error(home):
    make_cli(home)
    runner = FakeRunner({"auggie": (1, load_text("augment", "not_logged_in.txt"), "")})
    with pytest.raises(AuthError):
        AugmentProvider().fetch(make_ctx(home, runner=runner))


def test_session_sources_and_tenant_checks(home):
    write(home / ".augment/session.json", load_text("augment", "session.json"))
    override = json.dumps({"accessToken": "aug_env", "tenantURL": "https://e1.api.augmentcode.com",
                           "scopes": ["email"]})
    assert find_session(make_ctx(home)) == (TOKEN, "https://d16.api.augmentcode.com/")
    assert find_session(make_ctx(home, env={"AUGMENT_SESSION_AUTH": override}))[0] == "aug_env"
    write(home / ".augment/session.json", json.dumps({"accessToken": "x", "tenantURL": "y"}))
    assert find_session(make_ctx(home)) is None  # no scopes: auggie treats it as signed out
    assert billing_url("https://e1.api.augmentcode.com") == \
        "https://e1.api.augmentcode.com/get-billing-summary"
    for foreign in ("https://augmentcode.com.evil.example/", "http://d16.api.augmentcode.com/"):
        with pytest.raises(ProviderError):
            billing_url(foreign)


def test_not_configured(home):
    provider = AugmentProvider()
    ctx = make_ctx(home)
    assert provider.detect(ctx) is False
    with pytest.raises(NotConfigured):
        provider.fetch(ctx)
