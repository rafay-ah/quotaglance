"""Codex usage."""

from __future__ import annotations

from quotaglance.i18n import _
from quotaglance.models import ProviderSnapshot
from quotaglance.providers.base import FetchContext, NotConfigured, Provider


class CodexProvider(Provider):
    id = "codex"
    name = "Codex"
    short = "Cx"
    color = "#10A37F"
    category = "agents"
    homepage = "https://openai.com/codex"

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        raise NotConfigured(_("Not implemented yet"))
