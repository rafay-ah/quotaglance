"""JetBrains AI Assistant: monthly AI credits, read from the IDE's own file.

Every JetBrains IDE with AI Assistant writes its quota state to
``~/.config/JetBrains/<IDE><version>/options/AIAssistantQuotaManager2.xml``.
QuotaGlance reads the most recently updated one. Fully local: no network,
no credentials.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from quotaglance.i18n import _
from quotaglance.models import ProviderSnapshot, UsageWindow
from quotaglance.providers.base import FetchContext, NotConfigured, Provider, ProviderError
from quotaglance.util import clamp, parse_time, to_float

FILE_NAME = "AIAssistantQuotaManager2.xml"
UNITS_PER_CREDIT = 100_000.0

IDE_NAMES = {
    "intellijidea": "IntelliJ IDEA", "ideaic": "IntelliJ IDEA CE", "pycharm": "PyCharm",
    "pycharmce": "PyCharm CE", "webstorm": "WebStorm", "goland": "GoLand", "clion": "CLion",
    "rustrover": "RustRover", "datagrip": "DataGrip", "rubymine": "RubyMine", "rider": "Rider",
    "phpstorm": "PhpStorm", "androidstudio": "Android Studio", "dataspell": "DataSpell",
    "aqua": "Aqua", "fleet": "Fleet", "writerside": "Writerside",
}


def quota_files(ctx: FetchContext) -> list[Path]:
    bases = [ctx.config_home / "JetBrains", ctx.data_home / "JetBrains",
             ctx.config_home / "Google"]
    found = []
    for base in bases:
        if not base.is_dir():
            continue
        for candidate in base.glob(f"*/options/{FILE_NAME}"):
            if candidate.is_file():
                found.append(candidate)
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def ide_label(path: Path) -> str:
    folder = path.parent.parent.name  # e.g. "PyCharm2026.1"
    match = re.match(r"([A-Za-z]+)(\d[\d.]*)?$", folder)
    if not match:
        return folder
    name = IDE_NAMES.get(match.group(1).lower(), match.group(1))
    return f"{name} {match.group(2)}" if match.group(2) else name


def parse_quota_xml(text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return decoded (quotaInfo, nextRefill) JSON objects."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ProviderError(_("JetBrains quota file is unreadable")) from exc
    component = None
    for node in root.iter("component"):
        if node.get("name") == "AIAssistantQuotaManager2":
            component = node
            break
    if component is None:
        raise ProviderError(_("No AI Assistant quota recorded yet"))
    options = {opt.get("name"): opt.get("value") for opt in component.iter("option")}

    def decode(value: str | None) -> dict[str, Any]:
        if not value:
            return {}
        try:
            data = json.loads(value)  # ElementTree already decoded the entities
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    quota = decode(options.get("quotaInfo"))
    if not quota:
        raise ProviderError(_("No AI Assistant quota recorded yet"))
    return quota, decode(options.get("nextRefill"))


def parse_quota(quota: dict[str, Any], refill: dict[str, Any]) -> list[UsageWindow]:
    current = to_float(quota.get("current"))
    maximum = to_float(quota.get("maximum"))
    tariff = quota.get("tariffQuota") if isinstance(quota.get("tariffQuota"), dict) else {}
    if tariff:
        current = to_float(tariff.get("current")) if current is None else current
        maximum = to_float(tariff.get("maximum")) or maximum
    if not maximum or maximum <= 0 or current is None or current < 0:
        raise ProviderError(_("JetBrains didn't record a quota limit"))
    resets_at = parse_time(refill.get("next")) if refill else None
    windows = [UsageWindow(
        id="monthly", label=_("Credits"), used_percent=clamp(current / maximum * 100, 0, 100),
        resets_at=resets_at, window_seconds=30 * 86400,
        used=round(current / UNITS_PER_CREDIT, 2), limit=round(maximum / UNITS_PER_CREDIT, 2),
        unit="credits")]
    top_up = quota.get("topUpQuota")
    if isinstance(top_up, dict):
        top_current = to_float(top_up.get("current"))
        top_max = to_float(top_up.get("maximum"))
        if top_current is not None and top_max:
            windows.append(UsageWindow(
                id="top_up", label=_("Top-up"),
                used_percent=clamp(top_current / top_max * 100, 0, 100),
                used=round(top_current / UNITS_PER_CREDIT, 2),
                limit=round(top_max / UNITS_PER_CREDIT, 2), unit="credits"))
    return windows


class JetBrainsProvider(Provider):
    id = "jetbrains"
    name = "JetBrains AI"
    short = "JB"
    color = "#F43F5E"
    category = "editors"
    homepage = "https://www.jetbrains.com/ai/"
    source_summary = _("Quota file written by your JetBrains IDE (local)")
    setup_hint = _("Use AI Assistant once in any JetBrains IDE; it then records its quota "
                   "locally.")
    local_only = True

    def detect(self, ctx: FetchContext) -> bool:
        return bool(quota_files(ctx))

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        files = quota_files(ctx)
        if not files:
            raise NotConfigured(_("No JetBrains IDE with AI Assistant found"), self.setup_hint)
        last_error: ProviderError | None = None
        for path in files:
            try:
                quota, refill = parse_quota_xml(path.read_text(encoding="utf-8"))
                windows = parse_quota(quota, refill)
            except (OSError, ProviderError) as exc:
                last_error = exc if isinstance(exc, ProviderError) else ProviderError(str(exc))
                continue
            observed = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            status = str(quota.get("type") or "")
            return self.snapshot(ctx, windows, plan=None if status.lower() in (
                "available", "known", "") else status.title(), account=ide_label(path),
                source=ide_label(path), observed_at=observed)
        raise last_error or ProviderError(_("No AI Assistant quota recorded yet"))
