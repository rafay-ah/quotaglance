"""Data model shared by providers, the engine, the UI and the D-Bus API."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from quotaglance.util import parse_time


class Status(str, enum.Enum):
    OK = "ok"
    STALE = "stale"  # showing older data (e.g. last good values after an error)
    ERROR = "error"
    NOT_CONFIGURED = "not_configured"
    LOADING = "loading"


class Severity(enum.IntEnum):
    NORMAL = 0
    WARNING = 1
    CRITICAL = 2


DEFAULT_WARNING = 80.0
DEFAULT_CRITICAL = 95.0


def severity_for(percent: float | None, warning: float = DEFAULT_WARNING,
                 critical: float = DEFAULT_CRITICAL) -> Severity:
    if percent is None:
        return Severity.NORMAL
    if percent >= critical:
        return Severity.CRITICAL
    if percent >= warning:
        return Severity.WARNING
    return Severity.NORMAL


@dataclass
class UsageWindow:
    """One quota meter, e.g. "Session (5h)" at 42% resetting in 2h 13m.

    ``used_percent`` is 0..100 (it may exceed 100 when a provider allows
    overage). Windows without a percentage (balances, plain counters) set
    ``used_percent`` to None and describe themselves through ``detail``.
    """

    id: str
    label: str
    used_percent: float | None = None
    resets_at: datetime | None = None
    window_seconds: int | None = None
    used: float | None = None
    limit: float | None = None
    unit: str | None = None
    detail: str | None = None

    @property
    def has_meter(self) -> bool:
        return self.used_percent is not None

    @property
    def remaining_percent(self) -> float | None:
        if self.used_percent is None:
            return None
        return max(0.0, 100.0 - self.used_percent)

    def is_expired(self, now: datetime) -> bool:
        return self.resets_at is not None and self.resets_at <= now

    def effective(self, now: datetime) -> UsageWindow:
        """Return this window as it applies *now*.

        Local sources (log files) can report a window that has since reset.
        Once the reset moment has passed the quota is fresh again, so the
        meter drops to zero until new data arrives.
        """
        if self.is_expired(now) and self.used_percent is not None:
            return replace(self, used_percent=0.0, used=0.0 if self.used is not None else None,
                           resets_at=None, detail=self.detail)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "used_percent": self.used_percent,
            "resets_at": self.resets_at.isoformat() if self.resets_at else None,
            "window_seconds": self.window_seconds,
            "used": self.used,
            "limit": self.limit,
            "unit": self.unit,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UsageWindow:
        return cls(
            id=str(data.get("id", "")),
            label=str(data.get("label", "")),
            used_percent=data.get("used_percent"),
            resets_at=parse_time(data.get("resets_at")),
            window_seconds=data.get("window_seconds"),
            used=data.get("used"),
            limit=data.get("limit"),
            unit=data.get("unit"),
            detail=data.get("detail"),
        )


@dataclass
class ProviderSnapshot:
    provider_id: str
    status: Status
    windows: list[UsageWindow] = field(default_factory=list)
    plan: str | None = None
    account: str | None = None
    source: str | None = None
    message: str | None = None
    hint: str | None = None
    fetched_at: datetime | None = None
    observed_at: datetime | None = None

    @property
    def has_data(self) -> bool:
        return bool(self.windows)

    def effective_windows(self, now: datetime) -> list[UsageWindow]:
        return [window.effective(now) for window in self.windows]

    def peak(self, now: datetime) -> UsageWindow | None:
        """The most constrained metered window (highest used percent)."""
        metered = [w for w in self.effective_windows(now) if w.used_percent is not None]
        if not metered:
            return None
        return max(metered, key=lambda w: w.used_percent or 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "status": self.status.value,
            "windows": [w.to_dict() for w in self.windows],
            "plan": self.plan,
            "account": self.account,
            "source": self.source,
            "message": self.message,
            "hint": self.hint,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProviderSnapshot:
        try:
            status = Status(data.get("status", "error"))
        except ValueError:
            status = Status.ERROR
        return cls(
            provider_id=str(data.get("provider_id", "")),
            status=status,
            windows=[UsageWindow.from_dict(w) for w in data.get("windows") or []
                     if isinstance(w, dict)],
            plan=data.get("plan"),
            account=data.get("account"),
            source=data.get("source"),
            message=data.get("message"),
            hint=data.get("hint"),
            fetched_at=parse_time(data.get("fetched_at")),
            observed_at=parse_time(data.get("observed_at")),
        )
