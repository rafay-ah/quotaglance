"""Demo mode: realistic, slowly changing mock data for every surface.

Run ``quotaglance --demo`` (or set ``QUOTAGLANCE_DEMO=1``) to explore the
app without any accounts. Values drift upward while the app runs and
windows roll over when their reset time passes, so live updates, colour
changes and notifications can all be seen.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from quotaglance.models import ProviderSnapshot, Status, UsageWindow
from quotaglance.util import UTC

_ANCHOR = time.time()


@dataclass(frozen=True)
class DemoWindow:
    id: str
    label: str
    base: float  # percent used at app start
    per_hour: float  # percent added per hour of running
    reset_in: float  # seconds from app start until the first reset
    window: int  # window length in seconds
    limit: float | None = None
    unit: str | None = None


H = 3600
D = 86400

DEMO: dict[str, tuple[str, str, list[DemoWindow]]] = {
    "claude": ("Max 5x", "OAuth usage API", [
        DemoWindow("session", "Session", 42, 9.0, 2 * H + 13 * 60, 5 * H),
        DemoWindow("weekly", "Weekly", 67, 0.6, 3 * D + 4 * H, 7 * D),
        DemoWindow("weekly_opus", "Opus", 31, 0.4, 3 * D + 4 * H, 7 * D),
    ]),
    "codex": ("Plus", "Session logs", [
        DemoWindow("session", "5-hour", 18, 6.0, 3 * H + 47 * 60, 5 * H),
        DemoWindow("weekly", "Weekly", 82, 0.5, 2 * D + 1 * H, 7 * D),
    ]),
    "cursor": ("Pro", "Cursor dashboard API", [
        DemoWindow("monthly", "Included", 54, 0.3, 12 * D + 6 * H, 30 * D, 20.0, "USD"),
        DemoWindow("on_demand", "On-demand", 16, 0.1, 12 * D + 6 * H, 30 * D, 20.0, "USD"),
    ]),
    "copilot": ("Pro", "GitHub API", [
        DemoWindow("premium", "Premium", 96, 0.2, 9 * D + 3 * H, 30 * D, 300, "requests"),
        DemoWindow("chat", "Chat", 0, 0.0, 9 * D + 3 * H, 30 * D),
    ]),
    "gemini": ("Free", "Gemini CLI credentials", [
        DemoWindow("pro", "Pro models", 23, 2.0, 14 * H + 20 * 60, D),
        DemoWindow("flash", "Flash models", 8, 1.0, 14 * H + 20 * 60, D),
    ]),
    "kiro": ("Pro", "kiro-cli", [
        DemoWindow("credits", "Credits", 34, 0.4, 18 * D + 2 * H, 30 * D, 1000, "credits"),
    ]),
    "elevenlabs": ("Creator", "ElevenLabs API", [
        DemoWindow("credits", "Credits", 61, 0.3, 11 * D + 9 * H, 30 * D, 100_000, "credits"),
    ]),
    "opencode": ("Go", "Local history", [
        DemoWindow("session", "5-hour", 34, 4.0, 1 * H + 52 * 60, 5 * H, 12.0, "USD"),
        DemoWindow("weekly", "Weekly", 47, 0.8, 4 * D + 7 * H, 7 * D, 30.0, "USD"),
        DemoWindow("monthly", "Monthly", 37, 0.3, 21 * D, 30 * D, 60.0, "USD"),
    ]),
    "zai": ("GLM Coding Pro", "z.ai API", [
        DemoWindow("session", "5-hour", 12, 5.0, 4 * H + 5 * 60, 5 * H),
        DemoWindow("mcp", "MCP tools", 4, 0.1, 17 * D, 30 * D, 1000, "calls"),
    ]),
    "openrouter": ("Pay as you go", "OpenRouter API", [
        DemoWindow("credits", "Credits", 63, 0.2, 0, 0, 50.0, "USD"),
    ]),
}

DEMO_PROVIDERS = frozenset(DEMO)


def _window_at(spec: DemoWindow, now: datetime) -> UsageWindow:
    elapsed = max(0.0, now.timestamp() - _ANCHOR)
    if spec.window <= 0:  # a balance: no reset, just drifts
        used = min(100.0, spec.base + spec.per_hour * elapsed / H)
        resets_at = None
    elif elapsed < spec.reset_in:
        used = spec.base + spec.per_hour * elapsed / H
        resets_at = datetime.fromtimestamp(_ANCHOR + spec.reset_in, UTC)
    else:
        cycles = math.floor((elapsed - spec.reset_in) / spec.window) + 1
        last_reset = _ANCHOR + spec.reset_in + (cycles - 1) * spec.window
        used = spec.per_hour * (now.timestamp() - last_reset) / H
        resets_at = datetime.fromtimestamp(last_reset + spec.window, UTC)
    used = round(min(100.0, max(0.0, used)), 1)
    absolute = None
    if spec.limit is not None:
        absolute = round(spec.limit * used / 100.0, 2)
    return UsageWindow(
        id=spec.id,
        label=spec.label,
        used_percent=used,
        resets_at=resets_at,
        window_seconds=spec.window or None,
        used=absolute,
        limit=spec.limit,
        unit=spec.unit,
    )


def demo_snapshot(provider, now: datetime) -> ProviderSnapshot:
    entry = DEMO.get(provider.id)
    if entry is None:
        return ProviderSnapshot(provider.id, Status.NOT_CONFIGURED,
                                message="Not part of the demo", fetched_at=now)
    plan, source, windows = entry
    observed = now - timedelta(seconds=40) if provider.id == "codex" else now
    return ProviderSnapshot(
        provider_id=provider.id,
        status=Status.OK,
        windows=[_window_at(spec, now) for spec in windows],
        plan=plan,
        account="alex@example.com",
        source=source,
        fetched_at=now,
        observed_at=observed,
    )
