"""Threshold alerts ("Claude session at 80%") with de-duplication.

The tracker remembers, per provider window, the highest threshold already
announced and the reset time it belonged to. A threshold fires once per
window cycle; when the window resets (its reset time moves forward or usage
drops well below the lowest threshold) the tracker re-arms. State is
persisted so restarting the app does not repeat notifications.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from quotaglance import paths
from quotaglance.i18n import _
from quotaglance.models import ProviderSnapshot, Severity, Status
from quotaglance.timefmt import format_percent, format_reset
from quotaglance.util import parse_time

log = logging.getLogger(__name__)

THRESHOLDS = (80, 95)
REARM_MARGIN = 10.0  # usage must fall this far below a threshold to re-arm it


@dataclass(frozen=True)
class Alert:
    provider_id: str
    window_id: str
    kind: str  # "threshold" | "reset"
    threshold: int | None
    title: str
    body: str
    severity: Severity

    @property
    def notification_id(self) -> str:
        return f"{self.provider_id}-{self.window_id}"


class AlertTracker:
    def __init__(self, state_path: Path | None = None, persist: bool = True) -> None:
        self.state_path = state_path or paths.state_dir() / "alerts.json"
        self.persist = persist
        self.state: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        if not self.persist:
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        if not self.persist:
            return
        try:
            paths.atomic_write_text(self.state_path, json.dumps(self.state, sort_keys=True))
        except OSError as exc:
            log.debug("Could not save alert state: %s", exc)

    def evaluate(self, snapshot: ProviderSnapshot, provider_name: str, now: datetime, *,
                 thresholds: tuple[int, ...] = THRESHOLDS, notify_reset: bool = False
                 ) -> list[Alert]:
        """Return the alerts to show for this snapshot (possibly none)."""
        if snapshot.status is not Status.OK:
            return []  # never alert on stale or failed data
        alerts: list[Alert] = []
        changed = False
        active = sorted(thresholds)
        for window in snapshot.effective_windows(now):
            used = window.used_percent
            if used is None:
                continue
            key = f"{snapshot.provider_id}:{window.id}"
            entry = self.state.get(key, {"level": 0, "resets_at": None})
            level = int(entry.get("level") or 0)
            resets_at = window.resets_at.isoformat() if window.resets_at else None

            reset_happened = False
            if level:
                lowest = min(active) if active else THRESHOLDS[0]
                dropped = used < lowest - REARM_MARGIN
                moved_on = False
                previous_reset = entry.get("resets_at")
                if previous_reset and window.resets_at is not None and used < lowest:
                    before = parse_time(previous_reset)
                    moved_on = bool(before and (window.resets_at - before).total_seconds() > 3600)
                reset_happened = dropped or moved_on
            if reset_happened:
                if notify_reset:
                    alerts.append(Alert(
                        snapshot.provider_id, window.id, "reset", None,
                        _("{provider}: {window} limit reset").format(
                            provider=provider_name, window=window.label),
                        _("You're back to {pct} used.").format(pct=format_percent(used)),
                        Severity.NORMAL))
                level = 0
                changed = True

            crossed = [t for t in active if used >= t and t > level]
            if crossed:
                threshold = max(crossed)
                severity = Severity.CRITICAL if threshold >= 95 else Severity.WARNING
                reset_text = format_reset(window.resets_at, now)
                body = _("{pct} of your {window} quota is used.").format(
                    pct=format_percent(used), window=window.label.lower())
                if reset_text:
                    body += " " + reset_text + "."
                alerts.append(Alert(
                    snapshot.provider_id, window.id, "threshold", threshold,
                    _("{provider} {window} at {pct}").format(
                        provider=provider_name, window=window.label.lower(),
                        pct=format_percent(used)),
                    body, severity))
                level = threshold
                changed = True

            new_entry = {"level": level, "resets_at": resets_at or entry.get("resets_at")}
            if new_entry != entry:
                self.state[key] = new_entry
                changed = True
        if changed:
            self._save()
        return alerts
