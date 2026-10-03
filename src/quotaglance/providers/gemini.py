"""Gemini CLI: per-model daily request quotas (Pro, Flash, Flash Lite).

Source: the OAuth sign-in that Gemini CLI caches in ``~/.gemini/oauth_creds.json``
and the Code Assist quota API it uses itself. When the cached access token
has expired, QuotaGlance refreshes it *in memory only* with the client ID the
installed Gemini CLI ships (Google refresh tokens do not rotate, so this
never disturbs the CLI's own session). The file is never written.
"""

from __future__ import annotations

import glob
import json
import os
import re
import threading
import urllib.parse
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
    from_http_error,
)
from quotaglance.util import clamp, decode_jwt_claims, dig, parse_time, read_json, to_float

API = "https://cloudcode-pa.googleapis.com/v1internal"
TOKEN_URL = "https://oauth2.googleapis.com/token"
PROJECTS_URL = "https://cloudresourcemanager.googleapis.com/v1/projects"
LOGIN_HINT = _("Run `gemini` and choose “Login with Google”.")
UNSUPPORTED_AUTH = {
    "gemini-api-key": _("an API key"), "api-key": _("an API key"), "vertex-ai": "Vertex AI",
    "cloud-shell": "Cloud Shell", "compute-default-credentials": _("default credentials"),
    "gateway": _("a gateway"),
}
CLIENT_ID_RE = re.compile(r"(?:const|let|var)?\s*OAUTH_CLIENT_ID\s*=\s*['\"]([\w\-\.]+)['\"]\s*;")
CLIENT_SECRET_RE = re.compile(r"(?:const|let|var)?\s*OAUTH_CLIENT_SECRET\s*=\s*['\"]([\w\-]+)['\"]\s*;")

_lock = threading.Lock()
_access_cache: dict[str, tuple[str, float]] = {}  # refresh token -> (access token, expiry)
_client_cache: dict[str, tuple[float, tuple[str, str] | None]] = {}


def gemini_dir(ctx: FetchContext) -> Path:
    home = ctx.env.get("GEMINI_CLI_HOME")
    return (Path(home) if home else ctx.home) / ".gemini"


# -- credentials ----------------------------------------------------------------


def load_credentials(ctx: FetchContext) -> dict[str, Any] | None:
    path = gemini_dir(ctx) / "oauth_creds.json"
    if path.is_file():
        try:
            data = read_json(path)
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            return data
    # GEMINI_FORCE_ENCRYPTED_FILE_STORAGE mode keeps the token in the keyring.
    for _attrs, secret in ctx.secrets.search({"service": "gemini-cli-oauth",
                                              "account": "main-account"}):
        try:
            token = json.loads(secret).get("token") or {}
        except (ValueError, AttributeError):
            continue
        return {"access_token": token.get("accessToken"),
                "refresh_token": token.get("refreshToken"),
                "expiry_date": token.get("expiresAt")}
    return None


def selected_auth_type(ctx: FetchContext) -> str | None:
    path = gemini_dir(ctx) / "settings.json"
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    value = dig(data, "security", "auth", "selectedType") or data.get("selectedAuthType")
    return str(value) if value else None


def package_roots(ctx: FetchContext) -> list[Path]:
    roots: list[Path] = []
    binary = ctx.env.get("GEMINI_CLI_PATH") or ctx.which("gemini")
    if binary:
        current = Path(os.path.realpath(binary)).parent
        for _ in range(8):
            for candidate in (current, current / "lib/node_modules/@google/gemini-cli",
                              current / "libexec/lib/node_modules/@google/gemini-cli"):
                manifest = candidate / "package.json"
                if manifest.is_file():
                    try:
                        if read_json(manifest).get("name") == "@google/gemini-cli":
                            roots.append(candidate)
                    except (OSError, ValueError, AttributeError):
                        pass
            if current.parent == current:
                break
            current = current.parent
    patterns = [
        "/usr/lib/node_modules/@google/gemini-cli",
        "/usr/local/lib/node_modules/@google/gemini-cli",
        str(ctx.path("~/.npm-global/lib/node_modules/@google/gemini-cli")),
        str(ctx.path("~/.nvm/versions/node/*/lib/node_modules/@google/gemini-cli")),
        str(ctx.path("~/.local/share/fnm/node-versions/*/installation/lib/node_modules/"
                     "@google/gemini-cli")),
        str(ctx.path("~/.volta/tools/image/packages/@google/gemini-cli/lib/node_modules/"
                     "@google/gemini-cli")),
        str(ctx.path("~/.bun/install/global/node_modules/@google/gemini-cli")),
        str(ctx.path("~/.local/share/pnpm/global/*/node_modules/@google/gemini-cli")),
        "/home/linuxbrew/.linuxbrew/lib/node_modules/@google/gemini-cli",
    ]
    for pattern in patterns:
        for match in sorted(glob.glob(pattern), reverse=True):
            path = Path(match)
            if path not in roots and (path / "package.json").is_file():
                roots.append(path)
    return roots


def extract_client(text: str) -> tuple[str, str] | None:
    client_id = CLIENT_ID_RE.search(text)
    secret = CLIENT_SECRET_RE.search(text)
    if client_id and secret:
        return client_id.group(1), secret.group(1)
    return None


def find_oauth_client(ctx: FetchContext) -> tuple[str, str] | None:
    """The installed Gemini CLI's public OAuth client (never hardcoded here)."""
    env_id = ctx.env.get("GEMINI_OAUTH_CLIENT_ID")
    env_secret = ctx.env.get("GEMINI_OAUTH_CLIENT_SECRET")
    if env_id and env_secret:
        return env_id, env_secret
    files: list[Path] = []
    if ctx.env.get("GEMINI_OAUTH2_JS_PATH"):
        files.append(Path(ctx.env["GEMINI_OAUTH2_JS_PATH"]))
    for root in package_roots(ctx):
        files.append(root / "dist/src/code_assist/oauth2.js")
        files.append(root / "node_modules/@google/gemini-cli-core/dist/src/code_assist/oauth2.js")
        files.extend(sorted((root / "bundle").glob("*.js")))
    for path in files:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        key = str(path)
        cached = _client_cache.get(key)
        if cached and cached[0] == mtime:
            if cached[1]:
                return cached[1]
            continue
        try:
            found = extract_client(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            found = None
        _client_cache[key] = (mtime, found)
        if found:
            return found
    return None


def access_token(ctx: FetchContext, creds: dict[str, Any]) -> tuple[str, str | None]:
    """A valid access token (refreshed in memory if needed) and maybe a new id_token."""
    now = ctx.now().timestamp()
    token = creds.get("access_token")
    expiry = to_float(creds.get("expiry_date"))
    if expiry and expiry > 1e11:
        expiry /= 1000.0
    if token and (expiry is None or expiry - 60 > now):
        return token, None
    refresh = creds.get("refresh_token")
    if not refresh:
        raise AuthError(_("Gemini CLI sign-in has expired"), LOGIN_HINT)
    with _lock:
        cached = _access_cache.get(refresh)
    if cached and cached[1] - 60 > now:
        return cached[0], None
    client = find_oauth_client(ctx)
    if client is None:
        raise ProviderError(_("Couldn't refresh the Gemini CLI sign-in"),
                            _("Update Gemini CLI, or run `gemini` once to refresh it."))
    body = urllib.parse.urlencode({"client_id": client[0], "client_secret": client[1],
                                   "refresh_token": refresh, "grant_type": "refresh_token"})
    try:
        data = ctx.http.post(TOKEN_URL, data=body.encode(), headers={
            "Content-Type": "application/x-www-form-urlencoded"}).json()
    except HttpError as exc:
        if shutdown_signal(exc.text(4000)):
            raise consumer_shutdown_error() from None
        raise AuthError(_("Gemini CLI sign-in has expired"), LOGIN_HINT) from None
    new_token = (data or {}).get("access_token")
    if not new_token:
        raise AuthError(_("Gemini CLI sign-in has expired"), LOGIN_HINT)
    lifetime = to_float(data.get("expires_in")) or 3000.0
    with _lock:
        _access_cache[refresh] = (new_token, now + lifetime)
    return new_token, data.get("id_token")


# -- consumer shutdown (June 2026) ---------------------------------------------------


def shutdown_signal(text: str | None) -> bool:
    lowered = (text or "").lower()
    return ("unsupported_client" in lowered or "ineligibletiererror" in lowered
            or ("no longer supported" in lowered and "gemini code assist" in lowered)
            or ("migrate" in lowered and "antigravity" in lowered and "gemini" in lowered))


def consumer_shutdown_error() -> ProviderError:
    return ProviderError(
        _("Google no longer offers Gemini CLI quotas for personal accounts"),
        _("Gemini Code Assist for individuals ended in June 2026. Standard and Enterprise "
          "subscriptions still work; Google suggests Antigravity for personal use."))


# -- parsing ----------------------------------------------------------------------


def parse_load_code_assist(data: dict[str, Any]) -> dict[str, Any]:
    tier = data.get("currentTier") or {}
    project = data.get("cloudaicompanionProject")
    if isinstance(project, dict):
        project = project.get("id") or project.get("projectId")
    project = str(project).strip() if project else None
    flagged = any(shutdown_signal(f"{t.get('reasonCode')} {t.get('reasonMessage')}")
                  for t in data.get("ineligibleTiers") or [] if isinstance(t, dict))
    credits = 0.0
    for credit in dig(data, "paidTier", "availableCredits") or []:
        if isinstance(credit, dict) and credit.get("creditType") == "GOOGLE_ONE_AI":
            credits += to_float(credit.get("creditAmount")) or 0.0
    return {
        "tier": tier.get("id") if isinstance(tier, dict) else None,
        "paid_name": (dig(data, "paidTier", "name") or "").strip() or None,
        "project": project or None,
        "shutdown": flagged and not tier,
        "credits": credits or None,
    }


def resolve_plan(info: dict[str, Any], hosted_domain: str | None) -> str | None:
    if info.get("paid_name"):
        return info["paid_name"]
    tier = info.get("tier")
    if tier == "standard-tier":
        return _("Paid")
    if tier == "free-tier":
        return _("Workspace") if hosted_domain else _("Free")
    if tier == "legacy-tier":
        return _("Legacy")
    return None


FAMILIES = (("pro", _("Pro")), ("flash", _("Flash")), ("flash_lite", _("Flash Lite")))


def _family(model: str) -> str | None:
    model = model.lower()
    if "flash-lite" in model or "flash_lite" in model:
        return "flash_lite"
    if "flash" in model:
        return "flash"
    if "pro" in model:
        return "pro"
    return None


def parse_quota(data: dict[str, Any]) -> list[UsageWindow]:
    buckets = data.get("buckets") if isinstance(data, dict) else None
    if not buckets:
        raise ProviderError(_("Gemini returned no quota buckets"))
    lowest: dict[str, dict[str, Any]] = {}
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        fraction = to_float(bucket.get("remainingFraction"))
        family = _family(str(bucket.get("modelId") or ""))
        if fraction is None or family is None:
            continue
        current = lowest.get(family)
        if current is None or fraction < current["fraction"]:
            lowest[family] = {"fraction": fraction, "bucket": bucket}
    windows = []
    for family, label in FAMILIES:
        entry = lowest.get(family)
        if entry is None:
            continue
        fraction = clamp(entry["fraction"], 0.0, 1.0)
        bucket = entry["bucket"]
        remaining = to_float(bucket.get("remainingAmount"))
        limit = round(remaining / fraction) if remaining is not None and fraction > 0 else None
        windows.append(UsageWindow(
            id=family, label=label, used_percent=(1 - fraction) * 100,
            resets_at=parse_time(bucket.get("resetTime")), window_seconds=86400,
            used=(limit - remaining) if limit is not None else None, limit=limit,
            unit="requests" if limit is not None else None))
    if not windows:
        raise ProviderError(_("Gemini returned no recognised model quotas"))
    return windows


def pick_project(data: dict[str, Any]) -> str | None:
    for project in data.get("projects") or []:
        if not isinstance(project, dict):
            continue
        project_id = str(project.get("projectId") or "")
        if project_id.startswith("gen-lang-client") or "generative-language" in (
                project.get("labels") or {}):
            return project_id
    return None


class GeminiProvider(Provider):
    id = "gemini"
    name = "Gemini CLI"
    short = "G"
    color = "#4285F4"
    category = "agents"
    homepage = "https://github.com/google-gemini/gemini-cli"
    source_summary = _("Gemini CLI sign-in → Code Assist quota API")
    setup_hint = _("Run `gemini` and sign in with a Code Assist Standard or Enterprise account.")

    def detect(self, ctx: FetchContext) -> bool:
        return (gemini_dir(ctx) / "oauth_creds.json").is_file()

    def fetch(self, ctx: FetchContext) -> ProviderSnapshot:
        auth_type = selected_auth_type(ctx)
        if auth_type in UNSUPPORTED_AUTH:
            raise NotConfigured(
                _("Gemini CLI uses {method}, which has no quota API").format(
                    method=UNSUPPORTED_AUTH[auth_type]),
                _("Quotas are available when Gemini CLI signs in with Google."))
        creds = load_credentials(ctx)
        if not creds:
            raise NotConfigured(_("Not signed in to Gemini CLI"), LOGIN_HINT)
        token, fresh_id_token = access_token(ctx, creds)
        claims = decode_jwt_claims(fresh_id_token or creds.get("id_token"))
        email = claims.get("email")
        if not email:
            try:
                email = read_json(gemini_dir(ctx) / "google_accounts.json").get("active")
            except (OSError, ValueError, AttributeError):
                email = None
        headers = {"Authorization": f"Bearer {token}"}

        project_env = ctx.env.get("GOOGLE_CLOUD_PROJECT") or ctx.env.get("GOOGLE_CLOUD_PROJECT_ID")
        metadata = {"ideType": "IDE_UNSPECIFIED", "platform": "PLATFORM_UNSPECIFIED",
                    "pluginType": "GEMINI"}
        body: dict[str, Any] = {"metadata": metadata}
        if project_env and not project_env.isdigit():
            body["cloudaicompanionProject"] = project_env
            metadata["duetProject"] = project_env
        info: dict[str, Any] = {}
        try:
            info = parse_load_code_assist(ctx.http.post_json(f"{API}:loadCodeAssist", body,
                                                             headers=headers) or {})
        except HttpError as exc:
            if exc.status in (401,):
                raise AuthError(_("Gemini CLI sign-in was rejected"), LOGIN_HINT) from None
        exempt = bool(info.get("paid_name") or claims.get("hd"))
        if info.get("shutdown") and not exempt:
            raise consumer_shutdown_error()
        project = info.get("project") or (project_env if project_env and
                                          not project_env.isdigit() else None)
        if not project:
            try:
                project = pick_project(ctx.http.get_json(PROJECTS_URL, headers=headers) or {})
            except HttpError:
                project = None
        try:
            quota = ctx.http.post_json(f"{API}:retrieveUserQuota",
                                       {"project": project} if project else {}, headers=headers)
        except HttpError as exc:
            if exc.status == 403 and (shutdown_signal(exc.text(4000)) or (
                    info.get("tier") != "standard-tier" and not exempt and
                    "SUBSCRIPTION_REQUIRED" in exc.text(4000))):
                raise consumer_shutdown_error() from None
            raise from_http_error(exc, login_hint=LOGIN_HINT, service="Google") from None
        windows = parse_quota(quota or {})
        if info.get("credits"):
            windows.append(UsageWindow(id="credits", label=_("AI credits"),
                                       used=info["credits"], unit="credits",
                                       detail=_("{n:,.0f} credits available").format(
                                           n=info["credits"])))
        return self.snapshot(ctx, windows, plan=resolve_plan(info, claims.get("hd")),
                             account=email, source=_("Gemini CLI sign-in"))


def reset_caches() -> None:
    """For tests: forget refreshed tokens and discovered OAuth clients."""
    with _lock:
        _access_cache.clear()
    _client_cache.clear()
