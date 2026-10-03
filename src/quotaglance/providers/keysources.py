"""API keys other tools on this machine already hold (read-only).

Coding plans and API platforms are often already set up in OpenCode
(``opencode auth login``) or in Claude Code's ``settings.json`` (when
Claude Code is pointed at another provider's Anthropic-compatible
endpoint). Reusing those keys means no setup in QuotaGlance. The files
are only ever read.
"""

from __future__ import annotations

from quotaglance.providers.base import FetchContext
from quotaglance.util import read_json


def opencode_keys(ctx: FetchContext) -> dict[str, str]:
    """API keys saved by ``opencode auth login``, by provider id."""
    path = ctx.data_home / "opencode" / "auth.json"
    try:
        data = read_json(path) if path.is_file() else {}
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        return {}
    return {name: entry["key"].strip() for name, entry in data.items()
            if isinstance(entry, dict) and isinstance(entry.get("key"), str)
            and entry["key"].strip()}


def claude_code_env(ctx: FetchContext) -> tuple[str, str | None]:
    """(``ANTHROPIC_BASE_URL``, token) from Claude Code's ``settings.json``."""
    custom = (ctx.env.get("CLAUDE_CONFIG_DIR") or "").strip()
    path = (ctx.path(custom) if custom else ctx.path("~/.claude")) / "settings.json"
    try:
        env = (read_json(path) or {}).get("env") if path.is_file() else None
    except (OSError, ValueError, AttributeError):
        env = None
    if not isinstance(env, dict):
        return "", None
    token = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
    return str(env.get("ANTHROPIC_BASE_URL") or ""), str(token).strip() if token else None
