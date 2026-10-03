"""``quotaglance --status`` / ``--json``: usage in the terminal, no GUI needed."""

from __future__ import annotations

import json
import os
import sys

from quotaglance import __version__
from quotaglance.config import Config
from quotaglance.engine import Engine
from quotaglance.models import Severity, Status, severity_for
from quotaglance.secretstore import KeyringSecretStore
from quotaglance.timefmt import format_ago, format_countdown, format_percent, format_used_of
from quotaglance.util import utcnow

BAR_WIDTH = 16


def _bar(percent: float | None, color: bool) -> str:
    if percent is None:
        return " " * BAR_WIDTH
    filled = round(min(100.0, max(0.0, percent)) / 100 * BAR_WIDTH)
    bar = "█" * filled + "░" * (BAR_WIDTH - filled)
    if not color:
        return bar
    sev = severity_for(percent)
    code = {Severity.NORMAL: "32", Severity.WARNING: "33", Severity.CRITICAL: "31"}[sev]
    return f"\x1b[{code}m{bar}\x1b[0m"


def snapshot_payload(engine: Engine) -> dict:
    now = utcnow()
    providers = []
    for provider, snap in engine.visible_snapshots():
        entry = {"id": provider.id, "name": provider.name}
        if snap is not None:
            entry.update(snap.to_dict())
            entry["windows"] = [w.to_dict() for w in snap.effective_windows(now)]
        providers.append(entry)
    return {"version": __version__, "generated_at": now.isoformat(), "providers": providers}


def render_table(engine: Engine, color: bool) -> str:
    now = utcnow()
    bold = (lambda s: f"\x1b[1m{s}\x1b[0m") if color else (lambda s: s)
    dim = (lambda s: f"\x1b[2m{s}\x1b[0m") if color else (lambda s: s)
    lines: list[str] = []
    rows = engine.visible_snapshots()
    if not rows:
        return ("No providers are enabled. Open QuotaGlance → Preferences → Providers,\n"
                "or set one up (e.g. sign in to Claude Code or Codex) and run again.")
    for provider, snap in rows:
        header = bold(provider.name)
        if snap and snap.plan:
            header += "  " + dim(snap.plan)
        lines.append(header)
        if snap is None:
            lines.append("  " + dim("no data"))
            continue
        if snap.status in (Status.ERROR, Status.NOT_CONFIGURED, Status.STALE) and snap.message:
            note = snap.message + (f" — {snap.hint}" if snap.hint else "")
            lines.append("  " + ("\x1b[33m" + note + "\x1b[0m" if color else note))
        for window in snap.effective_windows(now):
            countdown = format_countdown(window.resets_at, now)
            amount = format_used_of(window.used, window.limit, window.unit)
            parts = [f"  {window.label:<18.18}"]
            if window.used_percent is not None:
                parts.append(_bar(window.used_percent, color))
                parts.append(f"{format_percent(window.used_percent):>5}")
            else:
                parts.append(f"{(window.detail or amount or ''):<22}")
            if countdown:
                parts.append(dim(f"resets in {countdown}"))
            if window.used_percent is not None and amount:
                parts.append(dim(f"({amount})"))
            lines.append("  ".join(parts))
        if snap.source or snap.fetched_at:
            meta = " · ".join(x for x in (snap.source, format_ago(snap.observed_at or
                                                                   snap.fetched_at, now)) if x)
            lines.append("  " + dim(meta))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def run_status(json_output: bool = False, demo: bool = False) -> int:
    config = Config()
    engine = Engine(config, KeyringSecretStore(), demo=demo)
    if not demo and not config.get("first_run_complete"):
        engine.apply_first_run(engine.detect_all())
    try:
        engine.refresh_blocking()
    finally:
        engine.shutdown()
    if json_output:
        json.dump(snapshot_payload(engine), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        sys.stdout.write(render_table(engine, color))
    return 0
