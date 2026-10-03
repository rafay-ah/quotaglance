import os
import subprocess
from pathlib import Path

import pytest

from conftest import load_json, load_text, make_ctx, write
from quotaglance.providers.antigravity import (
    REPORT_ARGS,
    AntigravityProvider,
    parse_quota_summary,
    parse_usage_report,
    parse_version,
)
from quotaglance.providers.base import AuthError, NotConfigured, ProviderError

REPORT = load_text("antigravity", "usage_report.json")


class AgyRunner:
    """Fake ``agy``: answers ``--version`` and the print-mode report separately."""

    def __init__(self, version: str = "1.2.11", report=(0, REPORT, "")) -> None:
        self.version = version
        self.report = report
        self.calls: list[list[str]] = []
        self.workdirs: list[tuple[str | None, bool]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if list(argv[1:]) == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, f"{self.version}\n", "")
        cwd = kwargs.get("cwd")
        self.workdirs.append((cwd, bool(cwd) and os.path.isdir(cwd) and not os.listdir(cwd)))
        if isinstance(self.report, Exception):
            raise self.report
        code, out, err = self.report
        return subprocess.CompletedProcess(argv, code, out, err)


def install_agy(home: Path, where: str = ".local/bin/agy") -> Path:
    agy = write(home / where, "#!/bin/sh\n")
    agy.chmod(0o755)
    return agy


def test_cli_report_maps_both_families_and_cadences():
    windows = parse_usage_report(REPORT)
    assert [w.id for w in windows] == ["gemini_5h", "gemini_weekly", "claude_gpt_5h",
                                       "claude_gpt_weekly"]
    assert [w.label for w in windows] == ["Gemini 5h", "Gemini 7d", "Claude 5h", "Claude 7d"]
    assert [w.used_percent for w in windows] == pytest.approx([5, 14, 27, 36])
    assert [w.window_seconds for w in windows] == [18000, 604800, 18000, 604800]
    assert windows[0].resets_at.isoformat() == "2026-09-13T03:48:04+00:00"
    assert windows[3].resets_at.isoformat() == "2026-09-20T00:39:54+00:00"


def test_quota_summary_camel_case_and_weekly_only_starter():
    windows = parse_quota_summary(load_json("antigravity", "quota_summary.json"))
    assert [(w.id, round(w.used_percent)) for w in windows] == [
        ("gemini_5h", 9), ("gemini_weekly", 18), ("claude_gpt_5h", 27),
        ("claude_gpt_weekly", 36)]
    [weekly] = parse_quota_summary(load_json("antigravity", "quota_summary_starter.json"))
    assert (weekly.id, weekly.label) == ("gemini_weekly", "Gemini 7d")
    assert weekly.used_percent == pytest.approx(60)


def test_remaining_variants_and_unmeasured_buckets():
    windows = parse_quota_summary({"groups": [
        {"displayName": "Claude and GPT models", "buckets": [
            {"bucketId": "3p-5h", "remaining": {"case": "remainingFraction", "value": 0.25}},
            {"bucketId": "3p-weekly", "remaining": 0.5},
            {"bucketId": "3p-extra", "disabled": True, "remainingFraction": 0.1}]},
        {"displayName": "Image Models", "buckets": [
            {"bucketId": "img-daily", "displayName": "Daily Image Limit", "remaining": {}},
            {"bucketId": "img-daily-2", "displayName": "Daily Image Limit",
             "remainingFraction": 1.2}]},
    ]})
    assert [(w.id, w.label, w.used_percent) for w in windows] == [
        ("claude_gpt_5h", "Claude 5h", 75.0),
        ("claude_gpt_weekly", "Claude 7d", 50.0),
        ("image_models_img_daily_2", "Daily Image", 0.0),
    ]
    assert windows[2].window_seconds is None


@pytest.mark.parametrize("text, message", [
    (REPORT.replace('"SUCCESS"', '"ERROR"'), "didn't return a usage report"),
    (REPORT.replace('"name": "usage"', '"name": "models"'), "didn't return a usage report"),
    (REPORT.replace('"remaining_fraction"', '"remaining_fractionx"'), "no usable quota"),
    ("Select login method:\n> Google", "Couldn't read"),
])
def test_only_successful_usage_reports_with_known_quota_parse(text, message):
    with pytest.raises(ProviderError, match=message):
        parse_usage_report(text)


def test_version_parsing_is_strict():
    assert parse_version("1.2.11\n") == (1, 2, 11)
    assert parse_version("agy 1.1.11") == (1, 1, 11)
    assert parse_version("v2.0.0") == (2, 0, 0)
    for text in ("1.2.2-preview", "1.2.2.3", "+1.2.2", "running", "", None):
        assert parse_version(text) is None


def test_fetch_checks_version_then_runs_the_report_in_a_private_dir(home):
    agy = install_agy(home)
    runner = AgyRunner()
    provider = AntigravityProvider()
    ctx = make_ctx(home, runner=runner)
    assert provider.detect(ctx) is True
    snap = provider.fetch(ctx)
    assert runner.calls == [[str(agy), "--version"], [str(agy), *REPORT_ARGS]]
    workdir, was_empty = runner.workdirs[0]
    assert was_empty and workdir != str(home)
    assert not os.path.exists(workdir)  # removed again afterwards
    assert snap.source == "agy CLI"
    assert snap.plan is None and snap.account is None
    assert snap.windows[0].id == "gemini_5h"


@pytest.mark.parametrize("version, message", [("1.1.10", "too old"),
                                              ("1.2.2-preview", "Couldn't determine")])
def test_old_or_unknown_versions_never_run_the_report(home, version, message):
    install_agy(home)
    runner = AgyRunner(version=version)
    with pytest.raises(ProviderError, match=message):
        AntigravityProvider().fetch(make_ctx(home, runner=runner))
    assert [call[1:] for call in runner.calls] == [["--version"]]


@pytest.mark.parametrize("report", [
    (1, "", "Error: You are not logged into Antigravity (https://lh3.googleusercontent.com/a/x)"),
    (0, "Select login method:\n  > Sign in with Google (https://lh3.googleusercontent.com)", ""),
])
def test_signed_out_cli_is_an_auth_error_without_leaking_output(home, report):
    install_agy(home)
    runner = AgyRunner(report=report)
    with pytest.raises(AuthError) as info:
        AntigravityProvider().fetch(make_ctx(home, runner=runner))
    assert "googleusercontent" not in info.value.message
    assert "agy" in (info.value.hint or "")


@pytest.mark.parametrize("stderr, message, transient", [
    ('Post "https://usage.invalid/v1": dial tcp: no such host', "couldn't reach Google", True),
    ("Eligibility check failed: account does not support Google ToS", "isn't eligible", False),
    ('Eligibility check failed: failed to get profile picture: Get "https://lh3.example/a": EOF',
     "eligibility", True),
    ("panic: runtime error", "exited with code 2", False),
])
def test_failed_runs_map_to_safe_messages(home, stderr, message, transient):
    install_agy(home)
    runner = AgyRunner(report=(2, "", stderr))
    with pytest.raises(ProviderError, match=message) as info:
        AntigravityProvider().fetch(make_ctx(home, runner=runner))
    assert info.value.transient is transient
    assert "https://" not in info.value.message


def test_report_timeout_is_transient(home):
    install_agy(home)
    runner = AgyRunner(report=subprocess.TimeoutExpired(["agy"], 45))
    with pytest.raises(ProviderError, match="timed out") as info:
        AntigravityProvider().fetch(make_ctx(home, runner=runner))
    assert info.value.transient


def test_binary_discovery_and_not_configured(home):
    provider = AntigravityProvider()
    assert provider.detect(make_ctx(home)) is False
    with pytest.raises(NotConfigured):
        provider.fetch(make_ctx(home))
    custom = install_agy(home, "opt/antigravity/agy")
    runner = AgyRunner()
    provider.fetch(make_ctx(home, runner=runner, env={"ANTIGRAVITY_CLI_PATH": str(custom)}))
    assert runner.calls[0][0] == str(custom)
    install_agy(home)  # an override that points nowhere is not silently replaced by PATH
    ctx = make_ctx(home, env={"ANTIGRAVITY_CLI_PATH": str(home / "missing/agy")})
    assert provider.detect(ctx) is False
