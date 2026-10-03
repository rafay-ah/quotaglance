"""z.ai usage."""

from __future__ import annotations

from quotaglance.i18n import _
from quotaglance.models import ProviderSnapshot
from quotaglance.providers.base import FetchContext, NotConfigured, Provider


class ZaiProvider(Provider):
    id = "zai"
    name = "z.ai"
    short = "Z"
    color = "#3B5BDB"
    category = "api"
    homepage = "https://z.ai"

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        raise NotConfigured(_("Not implemented yet"))
