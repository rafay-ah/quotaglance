import os

import pytest

from conftest import load_text, make_ctx, write
from quotaglance.providers.base import NotConfigured, ProviderError
from quotaglance.providers.jetbrains import (
    JetBrainsProvider,
    ide_label,
    parse_quota,
    parse_quota_xml,
)


def test_parses_entity_encoded_json_and_refill():
    quota, refill = parse_quota_xml(load_text("jetbrains", "AIAssistantQuotaManager2.xml"))
    windows = parse_quota(quota, refill)
    monthly, top_up = windows
    assert monthly.used_percent == pytest.approx(74.78305)
    assert monthly.used == pytest.approx(7.48)
    assert monthly.limit == 10
    assert monthly.unit == "credits"
    assert monthly.resets_at.isoformat().startswith("2026-10-16T14:00:54")
    assert top_up.used_percent == 25


def test_reversed_attribute_order_and_missing_refill():
    quota, refill = parse_quota_xml(load_text("jetbrains", "exceeded.xml"))
    windows = parse_quota(quota, refill)
    assert refill == {}
    assert windows[0].used_percent == 100
    assert windows[0].resets_at is None


def test_missing_maximum_is_an_error():
    with pytest.raises(ProviderError):
        parse_quota({"current": "5", "maximum": "0"}, {})


def test_picks_newest_ide_file(home):
    old = write(home / ".config/JetBrains/PyCharm2025.3/options/AIAssistantQuotaManager2.xml",
                load_text("jetbrains", "exceeded.xml"))
    new = write(home / ".config/JetBrains/IntelliJIdea2026.1/options/"
                       "AIAssistantQuotaManager2.xml",
                load_text("jetbrains", "AIAssistantQuotaManager2.xml"))
    os.utime(old, (1_700_000_000, 1_700_000_000))
    os.utime(new, (1_800_000_000, 1_800_000_000))
    provider = JetBrainsProvider()
    ctx = make_ctx(home)
    assert provider.detect(ctx)
    snap = provider.fetch(ctx)
    assert snap.source == "IntelliJ IDEA 2026.1"
    assert snap.windows[0].used_percent == pytest.approx(74.78305)


def test_falls_back_when_newest_file_is_broken(home):
    good = write(home / ".config/JetBrains/GoLand2025.2/options/AIAssistantQuotaManager2.xml",
                 load_text("jetbrains", "exceeded.xml"))
    bad = write(home / ".config/JetBrains/WebStorm2026.1/options/AIAssistantQuotaManager2.xml",
                "<application><component name='Other'/></application>")
    os.utime(good, (1_700_000_000, 1_700_000_000))
    os.utime(bad, (1_800_000_000, 1_800_000_000))
    snap = JetBrainsProvider().fetch(make_ctx(home))
    assert snap.source == "GoLand 2025.2"
    assert snap.plan == "Exceeded"


def test_ide_label():
    from pathlib import Path

    assert ide_label(Path("/x/RustRover2026.2/options/f.xml")) == "RustRover 2026.2"
    assert ide_label(Path("/x/AndroidStudio2025.1/options/f.xml")) == "Android Studio 2025.1"


def test_not_configured(home):
    with pytest.raises(NotConfigured):
        JetBrainsProvider().fetch(make_ctx(home))
