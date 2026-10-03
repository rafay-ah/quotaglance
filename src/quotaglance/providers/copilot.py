"""GitHub Copilot usage."""

from __future__ import annotations

from quotaglance.i18n import _
from quotaglance.models import ProviderSnapshot
from quotaglance.providers.base import FetchContext, NotConfigured, Provider


class CopilotProvider(Provider):
    id = "copilot"
    name = "GitHub Copilot"
    short = "Co"
    color = "#8957E5"
    category = "editors"
    homepage = "https://github.com/features/copilot"

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        raise NotConfigured(_("Not implemented yet"))
