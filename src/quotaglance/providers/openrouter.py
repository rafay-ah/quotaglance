"""OpenRouter usage."""

from __future__ import annotations

from quotaglance.i18n import _
from quotaglance.models import ProviderSnapshot
from quotaglance.providers.base import FetchContext, NotConfigured, Provider


class OpenRouterProvider(Provider):
    id = "openrouter"
    name = "OpenRouter"
    short = "OR"
    color = "#6366F1"
    category = "api"
    homepage = "https://openrouter.ai"

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        raise NotConfigured(_("Not implemented yet"))
