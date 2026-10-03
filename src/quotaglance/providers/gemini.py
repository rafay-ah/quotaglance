"""Gemini CLI usage."""

from __future__ import annotations

from quotaglance.i18n import _
from quotaglance.models import ProviderSnapshot
from quotaglance.providers.base import FetchContext, NotConfigured, Provider


class GeminiProvider(Provider):
    id = "gemini"
    name = "Gemini CLI"
    short = "G"
    color = "#4285F4"
    category = "agents"
    homepage = "https://github.com/google-gemini/gemini-cli"

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        raise NotConfigured(_("Not implemented yet"))
