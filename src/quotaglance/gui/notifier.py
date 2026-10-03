"""Desktop notifications for usage alerts (GNotification → GNOME Shell)."""

from __future__ import annotations

import logging

import gi

gi.require_version("Gio", "2.0")

from gi.repository import Gio  # noqa: E402

from quotaglance import APP_ID  # noqa: E402
from quotaglance.alerts import THRESHOLDS, Alert, AlertTracker  # noqa: E402
from quotaglance.i18n import _  # noqa: E402
from quotaglance.models import Severity  # noqa: E402
from quotaglance.util import utcnow  # noqa: E402

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, app) -> None:
        self.app = app
        self.tracker = AlertTracker(persist=not app.engine.demo)

    def reset_for_demo(self, demo: bool) -> None:
        self.tracker = AlertTracker(persist=not demo)

    def evaluate(self, provider_id: str) -> None:
        config = self.app.config
        if not config.get("notifications.enabled", True):
            return
        snapshot = self.app.engine.snapshots.get(provider_id)
        provider = self.app.engine.providers.get(provider_id)
        if snapshot is None or provider is None:
            return
        thresholds = tuple(t for t, key in zip(THRESHOLDS, ("warn_80", "warn_95"), strict=True)
                           if config.get(f"notifications.{key}", True))
        alerts = self.tracker.evaluate(snapshot, provider.name, utcnow(), thresholds=thresholds,
                                       notify_reset=bool(config.get("notifications.on_reset")))
        for alert in alerts:
            self.show(alert)

    def show(self, alert: Alert) -> None:
        notification = Gio.Notification.new(alert.title)
        notification.set_body(alert.body)
        if alert.severity is Severity.CRITICAL:
            notification.set_priority(Gio.NotificationPriority.HIGH)
        notification.set_icon(Gio.ThemedIcon.new(APP_ID))
        notification.set_default_action("app.show-window")
        notification.add_button(_("Open QuotaGlance"), "app.show-window")
        try:
            self.app.send_notification(alert.notification_id, notification)
        except Exception:  # never let a notification failure break updates
            log.exception("Could not send notification")
