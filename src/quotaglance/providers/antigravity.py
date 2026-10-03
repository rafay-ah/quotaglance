"""Antigravity: Gemini and Claude/GPT model quotas (5-hour and weekly limits).

Source: the usage report Google's ``agy`` CLI prints with
``agy -p /usage --output-format json`` (agy 1.1.11 or later), made with the
CLI's own Google sign-in. QuotaGlance never handles that sign-in: it only
runs the command, from a private empty directory, and parses its JSON.
``agy --version`` is checked first because older builds would send
``/usage`` to the model as a prompt. The report names no account or plan.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import replace
from functools import partial
from typing import Any

from quotaglance.i18n import _
from quotaglance.models import ProviderSnapshot, UsageWindow
from quotaglance.providers.base import (
    AuthError,
    FetchContext,
    NotConfigured,
    Provider,
    ProviderError,
    strip_ansi,
)
from quotaglance.util import clamp, first, parse_time, to_float

MIN_VERSION = (1, 1, 11)
REPORT_ARGS = ("-p", "/usage", "--output-format", "json", "--print-timeout", "40s")
REPORT_TIMEOUT = 45.0
MAX_OUTPUT = 1024 * 1024

LOGIN_HINT = _("Run `agy` in a terminal and sign in with Google.")
UPDATE_HINT = _("Update the Antigravity CLI to version 1.1.11 or later.")
NETWORK_HINT = _("Check your network or proxy settings.")

SESSION_RE = re.compile(r"(?:^|[^0-9])5\s*-?\s*h(?:ours?|rs?)?\b|five[\s-]*hours?|\bsession\b")
WEEKLY_RE = re.compile(r"week|(?:^|[^0-9])7\s*-?\s*d(?:ays?)?\b|seven[\s-]*days?")
SIGN_IN_RE = re.compile(
    r"not (?:logged|signed) in|unauthenticated|authentication required|login required"
    r"|please (?:log|sign) in|select\s+login\s+method|keyring\s*auth\s*:\s*timed\s+out")
ELIGIBILITY_RE = re.compile(
    r"eligibility check failed|not eligible|does not support google tos"
    r"|unsupported (?:country|region)")
NETWORK_RE = re.compile(
    r"\beof\b|\btimed out\b|\btimeout\b|deadline exceeded|no such host|\bdns\b"
    r"|connection (?:refused|reset)|network is unreachable|\bunreachable\b|\bproxy\b"
    r"|\bcertificate\b|\btls\b|\bssl\b|(?:get|post) \"https?:")

CADENCES = {"5h": (0, 5 * 3600), "weekly": (1, 7 * 86400), None: (2, None)}


# -- the agy CLI -------------------------------------------------------------------


def find_binary(ctx: FetchContext) -> str | None:
    """``$ANTIGRAVITY_CLI_PATH`` when set (it is authoritative), else ``agy`` on PATH."""
    override = (ctx.env.get("ANTIGRAVITY_CLI_PATH") or "").strip()
    if override:
        path = ctx.path(override)
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return ctx.which("agy")


def parse_version(text: str | None) -> tuple[int, ...] | None:
    """``agy --version`` → (major, minor, patch); anything else is unknown."""
    match = re.fullmatch(r"(?:agy\s+)?v?(\d+)\.(\d+)\.(\d+)", strip_ansi(text or "").strip(),
                         re.IGNORECASE)
    return tuple(int(part) for part in match.groups()) if match else None


def run_in_private_dir(ctx: FetchContext, argv: Sequence[str],
                       timeout: float) -> subprocess.CompletedProcess:
    """``ctx.run`` from a fresh, empty working directory.

    ``FetchContext.run`` takes no ``cwd``, and agy treats its working
    directory as a workspace, so the report runs from a throwaway directory
    instead of wherever QuotaGlance was started (usually $HOME).
    """
    with tempfile.TemporaryDirectory(prefix="quotaglance-agy-",
                                     ignore_cleanup_errors=True) as workdir:
        scoped = replace(ctx, runner=partial(ctx.runner, cwd=workdir))
        return scoped.run(argv, timeout=timeout)


def classify_failure(code: int, output: str) -> ProviderError:
    """A fixed, safe message for a failed run (agy's output can hold private URLs)."""
    text = strip_ansi(output).lower()
    if SIGN_IN_RE.search(text):
        return AuthError(_("agy isn't signed in"), LOGIN_HINT)
    network = bool(NETWORK_RE.search(text))
    if ELIGIBILITY_RE.search(text):
        if network:
            return ProviderError(_("agy couldn't check your Antigravity eligibility"),
                                 NETWORK_HINT, transient=True)
        return ProviderError(_("This Google account isn't eligible for Antigravity"))
    if network:
        return ProviderError(_("agy couldn't reach Google"), NETWORK_HINT, transient=True)
    return ProviderError(_("agy exited with code {code}").format(code=code))


# -- parsing ----------------------------------------------------------------------


def _text(obj: Any, *keys: str) -> str | None:
    value = first(obj, *keys)
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _slug(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (value or "").lower()).strip("_")


def _family(name: str | None) -> tuple[str, str, int]:
    """(id, short label, sort rank) of a quota group."""
    lower = (name or "").lower()
    if "gemini" in lower:
        return "gemini", _("Gemini"), 0
    if "claude" in lower or "gpt" in lower:
        return "claude_gpt", _("Claude"), 1
    words = (name or "").split()
    return _slug(name) or "quota", words[0][:9] if words else _("Quota"), 2


def _fraction(bucket: dict[str, Any]) -> float | None:
    """``remaining`` may be a number, ``{remainingFraction}`` or a protobuf oneof."""
    value = first(bucket, "remainingFraction", "remaining_fraction")
    remaining = bucket.get("remaining")
    if value is None and isinstance(remaining, dict):
        value = first(remaining, "remainingFraction", "remaining_fraction")
        if value is None and remaining.get("case") in ("remainingFraction", "remaining_fraction"):
            value = remaining.get("value")
    elif value is None:
        value = remaining
    number = to_float(value)
    return None if number is None else clamp(number, 0.0, 1.0)


def bucket_cadence(window: str | None, bucket_id: str | None, name: str | None) -> str | None:
    """``"5h"``, ``"weekly"`` or None: the explicit window, else the bucket's id/name."""
    text = (window or "").strip() or f"{bucket_id or ''} {name or ''}"
    text = text.lower().replace("_", "-")
    if SESSION_RE.search(text):
        return "5h"
    if WEEKLY_RE.search(text):
        return "weekly"
    return None


def parse_quota_summary(payload: Any) -> list[UsageWindow]:
    """Grouped quota buckets (the CLI report's data, or RetrieveUserQuotaSummary).

    Each group (Gemini, Claude/GPT) shares a 5-hour and a weekly limit.
    Buckets that are disabled or carry no fraction are not measurements.
    """
    if isinstance(payload, dict):
        for key in ("response", "summary"):
            if isinstance(payload.get(key), dict):
                payload = payload[key]
                break
    groups = payload.get("groups") if isinstance(payload, dict) else None
    entries = []
    for group_index, group in enumerate(groups if isinstance(groups, list) else []):
        if not isinstance(group, dict) or not isinstance(group.get("buckets"), list):
            continue
        family, family_label, family_rank = _family(
            _text(group, "displayName", "display_name", "name"))
        for bucket_index, bucket in enumerate(group["buckets"]):
            if not isinstance(bucket, dict) or bucket.get("disabled") is True:
                continue
            fraction = _fraction(bucket)
            if fraction is None:
                continue
            bucket_id = _text(bucket, "bucketId", "bucket_id", "id")
            name = _text(bucket, "displayName", "display_name", "name")
            cadence = bucket_cadence(_text(bucket, "window"), bucket_id, name)
            order = (family_rank, group_index, CADENCES[cadence][0], bucket_index)
            entries.append((order, family, family_label, cadence, bucket_id, name, fraction,
                            bucket))
    windows: list[UsageWindow] = []
    seen: set[str] = set()
    for _order, family, family_label, cadence, bucket_id, name, fraction, bucket in sorted(
            entries, key=lambda entry: entry[0]):
        if cadence == "5h":
            wid, label = f"{family}_5h", _("{family} 5h").format(family=family_label)
        elif cadence == "weekly":
            wid, label = f"{family}_weekly", _("{family} 7d").format(family=family_label)
        else:
            wid = f"{family}_{_slug(bucket_id or name) or 'quota'}"
            label = (name or bucket_id or family_label)[:12].strip()
        unique, n = wid, 2
        while unique in seen:
            unique, n = f"{wid}_{n}", n + 1
        seen.add(unique)
        windows.append(UsageWindow(
            id=unique, label=label, used_percent=(1.0 - fraction) * 100.0,
            resets_at=parse_time(first(bucket, "resetTime", "reset_time")),
            window_seconds=CADENCES[cadence][1]))
    if not windows:
        raise ProviderError(_("Antigravity reported no usable quota"))
    return windows


def _decode(text: str) -> Any:
    text = strip_ansi(text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            return json.loads(text[start:end + 1])  # tolerate log lines around the JSON
        except ValueError:
            return None


def parse_usage_report(text: str) -> list[UsageWindow]:
    """``agy -p /usage --output-format json`` → windows (only a successful usage report)."""
    data = _decode(text)
    command = data.get("command") if isinstance(data, dict) else None
    if not isinstance(command, dict):
        raise ProviderError(_("Couldn't read agy's usage report"))
    if data.get("status") != "SUCCESS" or str(command.get("name") or "").lower() not in (
            "usage", "quota"):
        raise ProviderError(_("agy didn't return a usage report"))
    return parse_quota_summary(command.get("data"))


class AntigravityProvider(Provider):
    id = "antigravity"
    name = "Antigravity"
    short = "Ag"
    color = "#4F46E5"
    category = "editors"
    homepage = "https://antigravity.google"
    source_summary = _("Antigravity CLI (`agy`) usage report")
    setup_hint = _("Install the Antigravity CLI (1.1.11 or later) and run `agy` once to sign "
                   "in with Google.")
    # Not local_only: agy calls Google for every report, so it keeps the normal
    # refresh interval instead of the one-minute cadence of local files.

    def detect(self, ctx: FetchContext) -> bool:
        return find_binary(ctx) is not None

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        binary = find_binary(ctx)
        if not binary:
            raise NotConfigured(_("Antigravity CLI (agy) not found"), self.setup_hint)
        probe = ctx.run([binary, "--version"], timeout=10)
        version = parse_version(probe.stdout) if probe.returncode == 0 else None
        if version is None:
            raise ProviderError(_("Couldn't determine the agy version"), UPDATE_HINT)
        if version < MIN_VERSION:
            raise ProviderError(_("agy {version} is too old for usage reports").format(
                version=".".join(map(str, version))), UPDATE_HINT)
        result = run_in_private_dir(ctx, [binary, *REPORT_ARGS], timeout=REPORT_TIMEOUT)
        stdout, stderr = result.stdout or "", result.stderr or ""
        if len(stdout) > MAX_OUTPUT:
            raise ProviderError(_("agy's usage report was unexpectedly large"))
        if result.returncode != 0:
            raise classify_failure(result.returncode, f"{stderr}\n{stdout}")
        try:
            windows = parse_usage_report(stdout)
        except ProviderError:
            if SIGN_IN_RE.search(strip_ansi(f"{stdout}\n{stderr}").lower()):
                raise AuthError(_("agy isn't signed in"), LOGIN_HINT) from None
            raise
        return self.snapshot(ctx, windows, source=_("agy CLI"))
