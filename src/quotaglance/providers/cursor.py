"""Cursor usage."""

from __future__ import annotations

from quotaglance.i18n import _
from quotaglance.models import ProviderSnapshot
from quotaglance.providers.base import FetchContext, NotConfigured, Provider


class CursorProvider(Provider):
    id = "cursor"
    name = "Cursor"
    short = "Cu"
    color = "#64748B"
    category = "editors"
    homepage = "https://cursor.com"

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        raise NotConfigured(_("Not implemented yet"))
