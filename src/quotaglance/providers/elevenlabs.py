"""ElevenLabs: monthly credits (the API still calls them characters).

Source: the official ``GET /v1/user/subscription`` endpoint with your API
key (``xi-api-key`` header). The key needs the "User → Read" permission.
"""

from __future__ import annotations

from typing import Any

from quotaglance.i18n import _
from quotaglance.models import ProviderSnapshot, UsageWindow
from quotaglance.net import HttpError
from quotaglance.providers.base import (
    AuthError,
    FetchContext,
    Provider,
    ProviderError,
    SettingSpec,
    api_key_setting,
    from_http_error,
)
from quotaglance.timefmt import format_amount
from quotaglance.util import parse_time, to_float

REGIONS = {
    "global": "https://api.elevenlabs.io",
    "us": "https://api.us.elevenlabs.io",
    "eu": "https://api.eu.residency.elevenlabs.io",
    "in": "https://api.in.residency.elevenlabs.io",
    "sg": "https://api.sg.residency.elevenlabs.io",
}


def subscription_url(ctx: FetchContext) -> str:
    override = (ctx.env.get("ELEVENLABS_API_URL") or "").strip().rstrip("/")
    if override.startswith("https://"):
        return override + ("/user/subscription" if override.endswith("/v1")
                           else "/v1/user/subscription")
    base = REGIONS.get(str(ctx.settings.get("region") or "global"), REGIONS["global"])
    return f"{base}/v1/user/subscription"


def plan_label(data: dict[str, Any]) -> str | None:
    tier = str(data.get("tier") or "").replace("_", " ").strip().lower()
    status = str(data.get("status") or "").strip().lower()
    label = tier.title() if tier else status.title() or None
    if label and status and status not in ("active", tier) and not (tier == "free" and
                                                                    status == "free"):
        label = f"{label} · {status.replace('_', ' ')}"
    return label


def parse_subscription(data: Any) -> tuple[list[UsageWindow], str | None]:
    if not isinstance(data, dict):
        raise ProviderError(_("Unexpected response from ElevenLabs"))
    used = data.get("character_count")
    limit = data.get("character_limit")
    if not isinstance(used, int) or not isinstance(limit, int) or isinstance(used, bool):
        raise ProviderError(_("ElevenLabs didn't report credit usage"))
    used = max(0, used)
    resets_at = parse_time(data.get("next_character_count_reset_unix"))
    detail = None
    overage = data.get("current_overage") or {}
    overage_amount = to_float(overage.get("amount")) if isinstance(overage, dict) else None
    if overage_amount:
        currency = str(overage.get("currency") or "usd").upper()
        detail = _("Overage {amount}").format(amount=format_amount(overage_amount, currency))
    windows = [UsageWindow(
        id="credits", label=_("Credits"),
        used_percent=used / limit * 100 if limit > 0 else None,
        resets_at=resets_at, window_seconds=30 * 86400 if resets_at else None,
        used=float(used), limit=float(limit) if limit > 0 else None, unit="credits",
        detail=detail if limit > 0 else _("No credit limit"))]
    voices_used = to_float(data.get("voice_slots_used"))
    voices_limit = to_float(data.get("voice_limit"))
    if voices_used is not None and voices_limit and voices_limit > 0:
        windows.append(UsageWindow(
            id="voice_slots", label=_("Voice slots"),
            used_percent=min(100.0, voices_used / voices_limit * 100),
            used=voices_used, limit=voices_limit, unit="voices"))
    return windows, plan_label(data)


def _error_code(exc: HttpError) -> str:
    body = exc.json()
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return str(detail.get("code") or detail.get("status") or "").strip().lower()
    return ""


class ElevenLabsProvider(Provider):
    id = "elevenlabs"
    name = "ElevenLabs"
    short = "11"
    color = "#4B5563"
    category = "media"
    homepage = "https://elevenlabs.io/app/subscription"
    source_summary = _("ElevenLabs subscription API (API key)")
    setup_hint = _("Create an API key with the User → Read permission at "
                   "elevenlabs.io → Developers → API keys, and paste it below.")
    settings = (
        api_key_setting(("ELEVENLABS_API_KEY", "XI_API_KEY")),
        SettingSpec("region", "choice", _("Data residency"),
                    _("Where your ElevenLabs workspace lives"),
                    choices=(("global", _("Global (default)")), ("us", "US"), ("eu", "EU"),
                             ("in", _("India")), ("sg", _("Singapore"))),
                    default="global"),
    )

    def detect(self, ctx: FetchContext) -> bool:
        return bool(self.api_key(ctx))

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        key = self.require_api_key(ctx)
        try:
            data = ctx.http.get_json(subscription_url(ctx), headers={"xi-api-key": key})
        except HttpError as exc:
            code = _error_code(exc)
            if code in ("missing_permissions", "insufficient_permissions"):
                raise AuthError(_("The API key lacks the User → Read permission"),
                                self.setup_hint) from None
            if code == "invalid_api_key":
                raise AuthError(_("ElevenLabs rejected the API key"),
                                _("Check the key and the data-residency region.")) from None
            raise from_http_error(exc, login_hint=self.setup_hint, service=self.name) from None
        windows, plan = parse_subscription(data)
        return self.snapshot(ctx, windows, plan=plan, source=_("ElevenLabs API"))
