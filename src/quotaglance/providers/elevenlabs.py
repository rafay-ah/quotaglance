"""ElevenLabs usage."""

from __future__ import annotations

from quotaglance.i18n import _
from quotaglance.models import ProviderSnapshot
from quotaglance.providers.base import FetchContext, NotConfigured, Provider


class ElevenLabsProvider(Provider):
    id = "elevenlabs"
    name = "ElevenLabs"
    short = "11"
    color = "#4B5563"
    category = "media"
    homepage = "https://elevenlabs.io"

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        raise NotConfigured(_("Not implemented yet"))
