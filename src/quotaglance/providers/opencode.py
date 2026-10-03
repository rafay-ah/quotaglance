"""OpenCode usage."""

from __future__ import annotations

from quotaglance.i18n import _
from quotaglance.models import ProviderSnapshot
from quotaglance.providers.base import FetchContext, NotConfigured, Provider


class OpenCodeProvider(Provider):
    id = "opencode"
    name = "OpenCode"
    short = "OC"
    color = "#F59E0B"
    category = "agents"
    homepage = "https://opencode.ai"

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        raise NotConfigured(_("Not implemented yet"))
