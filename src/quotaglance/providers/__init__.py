"""Registry of supported providers (display order = default order)."""

from __future__ import annotations

from quotaglance.providers.antigravity import AntigravityProvider
from quotaglance.providers.base import Provider
from quotaglance.providers.claude import ClaudeProvider
from quotaglance.providers.codex import CodexProvider
from quotaglance.providers.copilot import CopilotProvider
from quotaglance.providers.cursor import CursorProvider
from quotaglance.providers.elevenlabs import ElevenLabsProvider
from quotaglance.providers.gemini import GeminiProvider
from quotaglance.providers.jetbrains import JetBrainsProvider
from quotaglance.providers.kiro import KiroProvider
from quotaglance.providers.opencode import OpenCodeProvider
from quotaglance.providers.openrouter import OpenRouterProvider
from quotaglance.providers.windsurf import WindsurfProvider
from quotaglance.providers.zai import ZaiProvider
from quotaglance.providers.zed import ZedProvider

PROVIDER_CLASSES: tuple[type[Provider], ...] = (
    ClaudeProvider,
    CodexProvider,
    CursorProvider,
    CopilotProvider,
    JetBrainsProvider,
    WindsurfProvider,
    ZedProvider,
    AntigravityProvider,
    GeminiProvider,
    KiroProvider,
    ElevenLabsProvider,
    OpenCodeProvider,
    ZaiProvider,
    OpenRouterProvider,
)


def all_providers() -> list[Provider]:
    return [cls() for cls in PROVIDER_CLASSES]


def provider_class(provider_id: str) -> type[Provider] | None:
    for cls in PROVIDER_CLASSES:
        if cls.id == provider_id:
            return cls
    return None
