"""Claude Code usage."""

from __future__ import annotations

from quotaglance.i18n import _
from quotaglance.models import ProviderSnapshot
from quotaglance.providers.base import FetchContext, NotConfigured, Provider


class ClaudeProvider(Provider):
    id = "claude"
    name = "Claude Code"
    short = "Cl"
    color = "#D97757"
    category = "agents"
    homepage = "https://claude.com/claude-code"

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        raise NotConfigured(_("Not implemented yet"))
