"""Cline (ClinePass): 5-hour, weekly and monthly usage limits.

Source: Cline's ``usage-limits`` API, authenticated with the sign-in that
``cline auth`` saved in ``~/.cline/data/settings/providers.json`` or with a
Cline API key from GNOME Keyring or ``$CLINE_API_KEY``. That sign-in token
lives only minutes and is never refreshed here (a refresh would sign you
out of Cline), so an API key is the reliable choice for background updates.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quotaglance.i18n import _
from quotaglance.models import ProviderSnapshot, UsageWindow
from quotaglance.net import HttpError
from quotaglance.providers.base import (
    AuthError,
    FetchContext,
    NotConfigured,
    Provider,
    ProviderError,
    api_key_setting,
    from_http_error,
)
from quotaglance.util import clamp, decode_jwt_claims, dig, parse_time, read_json, to_float

URL = "https://api.cline.bot/api/v1/users/me/plan/usage-limits"
ENV_KEYS = ("CLINE_API_KEY", "CLINEPASS_API_KEY")
AUTH_HINT = _("Check your Cline API key, or run `cline auth` to refresh your sign-in.")
EXPIRED_HINT = _("Open Cline or run `cline auth`, or paste a Cline API key in Preferences.")
# limit type → (window id, label, length)
LIMITS = {
    "five_hour": ("session", _("5-hour"), 5 * 3600),
    "weekly": ("weekly", _("Weekly"), 7 * 86400),
    "monthly": ("monthly", _("Monthly"), 30 * 86400),
}


@dataclass
class Credential:
    token: str
    origin: str
    expires_ms: float | None = None  # only for the short-lived OAuth sign-in
    oauth: bool = False


# -- credentials ------------------------------------------------------------------


def providers_path(ctx: FetchContext) -> Path:
    """Where Cline keeps provider settings (same lookup order as Cline itself)."""
    for name, suffix in (("CLINE_PROVIDER_SETTINGS_PATH", ""), ("CLINE_DATA_DIR", "settings"),
                         ("CLINE_DIR", "data/settings")):
        value = (ctx.env.get(name) or "").strip()
        if value:
            base = ctx.path(value)
            return base / suffix / "providers.json" if suffix else base
    return ctx.path("~/.cline/data/settings/providers.json")


def parse_providers(data: Any) -> Credential | None:
    """The ``cline`` provider's sign-in (OAuth wins over an API key kept beside it)."""
    settings = dig(data, "providers", "cline", "settings")
    if not isinstance(settings, dict):
        return None
    auth = settings.get("auth") if isinstance(settings.get("auth"), dict) else {}
    access = auth.get("accessToken")
    if isinstance(access, str) and access.strip():
        token = access.strip()
        token = token if token.startswith("workos:") else f"workos:{token}"
        expires = to_float(auth.get("expiresAt"))
        if expires is None or expires <= 0:
            exp = to_float(decode_jwt_claims(token.removeprefix("workos:")).get("exp"))
            expires = exp * 1000 if exp else None
        return Credential(token, _("Cline sign-in"), expires or 0.0, oauth=True)
    for value in (settings.get("apiKey"), auth.get("apiKey")):
        if isinstance(value, str) and value.strip():
            return Credential(value.strip(), _("Cline settings"))
    return None


def find_credentials(ctx: FetchContext) -> list[Credential]:
    """The Cline sign-in first, then a pasted or environment API key."""
    found = []
    try:
        session = parse_providers(read_json(providers_path(ctx)))
    except (OSError, ValueError):
        session = None
    if session:
        found.append(session)
    stored = ctx.secret("api_key", (), provider="clinepass")
    if stored:
        found.append(Credential(stored, _("Keyring")))
    for name in ENV_KEYS:
        value = (ctx.env.get(name) or "").strip()
        if value and value != stored:
            found.append(Credential(value, f"${name}"))
            break
    return found


# -- parsing ----------------------------------------------------------------------


def parse_usage_limits(payload: Any) -> list[UsageWindow]:
    """Validate ``{success, data: {limits: [...]}}`` as strictly as Cline's own client."""
    if not isinstance(payload, dict):
        raise ProviderError(_("Unexpected response from Cline"))
    success = payload.get("success")
    if success is False:
        error = payload.get("error")
        raise ProviderError(_("No ClinePass usage for this account"),
                            str(error) if isinstance(error, str) and error else None)
    if success is not True:
        raise ProviderError(_("Unexpected response from Cline (no success flag)"))
    data = payload.get("data")
    limits = data.get("limits") if isinstance(data, dict) else None
    if not isinstance(limits, list):
        raise ProviderError(_("Unexpected response from Cline (no limits)"))
    windows: dict[str, UsageWindow] = {}
    for item in limits:
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            raise ProviderError(_("Unexpected ClinePass limit in Cline's response"))
        kind = item["type"]
        if kind not in LIMITS:
            continue  # pools QuotaGlance doesn't know yet
        percent = item.get("percentUsed")
        if isinstance(percent, bool) or not isinstance(percent, (int, float)) \
                or to_float(percent) is None:
            raise ProviderError(_("ClinePass sent no usage number for {kind}").format(kind=kind))
        reset = item.get("resetsAt")
        resets_at = None
        if reset is not None:
            resets_at = parse_time(reset) if isinstance(reset, str) else None
            if resets_at is None:
                raise ProviderError(_("ClinePass sent an invalid reset time for {kind}")
                                    .format(kind=kind))
        window_id, label, seconds = LIMITS[kind]
        windows[kind] = UsageWindow(id=window_id, label=label,
                                    used_percent=clamp(float(percent), 0, 100),
                                    resets_at=resets_at, window_seconds=seconds)
    return [windows[kind] for kind in LIMITS if kind in windows]


class ClinePassProvider(Provider):
    id = "clinepass"
    name = "Cline"
    short = "Cn"
    color = "#14B8A6"
    category = "editors"
    homepage = "https://app.cline.bot/dashboard"
    source_summary = _("ClinePass usage API (Cline sign-in or API key)")
    setup_hint = _("Paste a Cline API key below (best for background updates), or run "
                   "`cline auth`.")
    settings = (api_key_setting(ENV_KEYS),)

    def detect(self, ctx: FetchContext) -> bool:
        return bool(find_credentials(ctx))

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        credentials = find_credentials(ctx)
        if not credentials:
            raise NotConfigured(_("No Cline API key or sign-in found"), self.setup_hint)
        now_ms = ctx.now().timestamp() * 1000
        rejected: AuthError | None = None
        for credential in credentials:  # an expired or rejected one falls through
            if credential.oauth and (credential.expires_ms or 0) <= now_ms:
                # Never call the API with it, and never refresh it: Cline owns that session.
                rejected = AuthError(_("The Cline sign-in has expired"), EXPIRED_HINT)
                continue
            try:
                payload = ctx.http.get_json(URL, headers={
                    "Authorization": f"Bearer {credential.token}",
                    "Content-Type": "application/json"}, timeout=15)
            except HttpError as exc:
                if exc.status in (401, 403):
                    rejected = AuthError(_("Cline rejected the credentials"), AUTH_HINT)
                    continue
                raise from_http_error(exc, login_hint=AUTH_HINT, service=self.name) from None
            windows = parse_usage_limits(payload)
            if not windows:
                raise ProviderError(_("No ClinePass subscription on this account"))
            return self.snapshot(ctx, windows, plan="ClinePass", source=credential.origin)
        raise rejected or AuthError(_("Cline rejected the credentials"), AUTH_HINT)
