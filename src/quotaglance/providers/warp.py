"""Warp: monthly AI credits and add-on credit grants.

Source: Warp's GraphQL ``GetRequestLimitInfo`` query (what the Warp app
asks), called with a personal Warp API key (``wk-…``) from GNOME Keyring,
``$WARP_API_KEY`` or ``$WARP_TOKEN``. The Warp app's own sign-in is never
read: refreshing it could interfere with the app.
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
    api_key_setting,
    from_http_error,
)
from quotaglance.timefmt import format_amount
from quotaglance.util import clamp, dig, parse_time, to_float

URL = "https://app.warp.dev/graphql/v2?op=GetRequestLimitInfo"
# Known-good client identity: Warp's edge limiter answers 429 "Rate exceeded." to other
# User-Agents, and these OS values are what the official client sends.
OS_CONTEXT = {"category": "macOS", "name": "macOS", "version": "15.6.1"}
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "x-warp-client-id": "warp-app",
    "x-warp-os-category": OS_CONTEXT["category"],
    "x-warp-os-name": OS_CONTEXT["name"],
    "x-warp-os-version": OS_CONTEXT["version"],
    "User-Agent": "Warp/1.0",
}
# Unknown fields turn into GraphQL errors, so the selection set stays exactly this.
QUERY = """query GetRequestLimitInfo($requestContext: RequestContext!) {
  user(requestContext: $requestContext) {
    __typename
    ... on UserOutput {
      user {
        requestLimitInfo {
          isUnlimited
          nextRefreshTime
          requestLimit
          requestsUsedSinceLastRefresh
        }
        bonusGrants {
          requestCreditsGranted
          requestCreditsRemaining
          expiration
        }
        workspaces {
          bonusGrantsInfo {
            grants {
              requestCreditsGranted
              requestCreditsRemaining
              expiration
            }
          }
        }
      }
    }
  }
}"""
KEY_HINT = _("Warp API keys can expire. Create a personal key in Warp → Settings → Cloud "
             "platform → API keys and paste it in Preferences.")


def _number(value: Any) -> float:
    return to_float(value) or 0.0


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return isinstance(value, str) and value.strip().lower() in ("true", "1", "yes")


def _grants(user: dict[str, Any]) -> list[dict[str, Any]]:
    """The user's own add-on grants plus those of every workspace they belong to."""
    grants = user.get("bonusGrants")
    found = [g for g in grants if isinstance(g, dict)] if isinstance(grants, list) else []
    workspaces = user.get("workspaces")
    for workspace in workspaces if isinstance(workspaces, list) else []:
        extra = dig(workspace, "bonusGrantsInfo", "grants")
        if isinstance(extra, list):
            found += [g for g in extra if isinstance(g, dict)]
    return found


def _addon_window(grants: list[dict[str, Any]]) -> UsageWindow | None:
    granted = sum(_number(g.get("requestCreditsGranted")) for g in grants)
    remaining = sum(_number(g.get("requestCreditsRemaining")) for g in grants)
    if granted <= 0 and remaining <= 0:
        return None
    detail = None
    expiring = [(parse_time(g.get("expiration")), _number(g.get("requestCreditsRemaining")))
                for g in grants]
    expiring = [(when, left) for when, left in expiring if when and left > 0]
    if expiring:
        soonest = min(when for when, _left in expiring)
        amount = sum(left for when, left in expiring
                     if int(when.timestamp()) == int(soonest.timestamp()))
        detail = _("{amount} expire {date}").format(
            amount=format_amount(amount, "credits"), date=f"{soonest:%b} {soonest.day}")
    return UsageWindow(
        id="addon_credits", label=_("Add-ons"),
        used_percent=clamp((granted - remaining) / granted * 100, 0, 100) if granted > 0
        else 0.0,
        used=max(0.0, granted - remaining) if granted > 0 else None,
        limit=granted if granted > 0 else None, unit="credits" if granted > 0 else None,
        detail=detail)


def _raise_graphql_errors(errors: list[Any]) -> None:
    messages, unauthorized = [], False
    for error in errors:
        message = error if isinstance(error, str) else (
            error.get("message") if isinstance(error, dict) else None)
        code = dig(error, "extensions", "code") if isinstance(error, dict) else None
        text = str(message or "").strip()
        if text:
            messages.append(text)
        if str(code or "").upper() in ("UNAUTHENTICATED", "UNAUTHORIZED", "FORBIDDEN") \
                or "unauthorized" in text.lower():
            unauthorized = True
    if unauthorized:
        raise AuthError(_("Warp rejected the API key"), KEY_HINT)
    raise ProviderError(" | ".join(messages[:3]) or _("Warp's API returned an error"))


def parse_request_limits(payload: Any) -> list[UsageWindow]:
    """``GetRequestLimitInfo`` → monthly credits (+ add-on grants). Fields count credits."""
    if not isinstance(payload, dict):
        raise ProviderError(_("Unexpected response from Warp"))
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        _raise_graphql_errors(errors)
    outer = dig(payload, "data", "user")
    if not isinstance(outer, dict):
        raise ProviderError(_("Unexpected response from Warp"))
    user = outer.get("user")
    info = user.get("requestLimitInfo") if isinstance(user, dict) else None
    if not isinstance(user, dict) or not isinstance(info, dict):
        typename = str(outer.get("__typename") or "").strip()
        if "auth" in typename.lower():
            raise AuthError(_("Warp rejected the API key"), KEY_HINT)
        if typename and typename != "UserOutput":
            raise ProviderError(_("Unexpected user type '{name}' from Warp").format(name=typename))
        raise ProviderError(_("Warp didn't report credit usage"))
    limit = _number(info.get("requestLimit"))
    used = _number(info.get("requestsUsedSinceLastRefresh"))
    if _flag(info.get("isUnlimited")):
        credits = UsageWindow(id="credits", label=_("Credits"), used_percent=0.0, used=used,
                              unit="credits", detail=_("Unlimited"))
    else:
        credits = UsageWindow(
            id="credits", label=_("Credits"),
            used_percent=clamp(used / limit * 100, 0, 100) if limit > 0 else 0.0,
            resets_at=parse_time(info.get("nextRefreshTime")), used=used,
            limit=limit if limit > 0 else None, unit="credits")
    addons = _addon_window(_grants(user))
    return [credits, addons] if addons else [credits]


def api_body() -> dict[str, Any]:
    return {
        "query": QUERY,
        "variables": {"requestContext": {"clientContext": {}, "osContext": dict(OS_CONTEXT)}},
        "operationName": "GetRequestLimitInfo",
    }


class WarpProvider(Provider):
    id = "warp"
    name = "Warp"
    short = "Wa"
    color = "#38BDF8"
    category = "agents"
    homepage = "https://app.warp.dev/settings/billing"
    source_summary = _("Warp request-limit API (personal API key)")
    setup_hint = _("Create a personal API key in Warp (Settings → Cloud platform → API keys) "
                   "and paste it below.")
    settings = (api_key_setting(("WARP_API_KEY", "WARP_TOKEN")),)

    def detect(self, ctx: FetchContext) -> bool:
        return bool(self.api_key(ctx))

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        key = self.require_api_key(ctx)
        try:
            payload = ctx.http.post_json(URL, api_body(),
                                         headers={**HEADERS, "Authorization": f"Bearer {key}"},
                                         timeout=15)
        except HttpError as exc:
            if exc.status in (401, 403):
                raise AuthError(_("Warp rejected the API key"), KEY_HINT) from None
            if exc.status == 429:
                # Usually an unexpected User-Agent at Warp's edge; back off for a while.
                raise ProviderError(_("Rate limited by Warp; will retry later"), transient=True,
                                    retry_after=max(60.0, exc.retry_after or 0.0)) from None
            raise from_http_error(exc, login_hint=KEY_HINT, service=self.name) from None
        return self.snapshot(ctx, parse_request_limits(payload), source=_("Warp API"))
