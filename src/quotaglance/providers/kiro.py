"""Kiro usage."""

from __future__ import annotations

from quotaglance.i18n import _
from quotaglance.models import ProviderSnapshot
from quotaglance.providers.base import FetchContext, NotConfigured, Provider


class KiroProvider(Provider):
    id = "kiro"
    name = "Kiro"
    short = "K"
    color = "#D946EF"
    category = "editors"
    homepage = "https://kiro.dev"

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        raise NotConfigured(_("Not implemented yet"))
