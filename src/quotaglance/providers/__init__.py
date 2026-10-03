"""Registry of supported providers (display order = default order)."""

from __future__ import annotations

from quotaglance.providers.amp import AmpProvider
from quotaglance.providers.antigravity import AntigravityProvider
from quotaglance.providers.augment import AugmentProvider
from quotaglance.providers.base import Provider
from quotaglance.providers.chutes import ChutesProvider
from quotaglance.providers.claude import ClaudeProvider
from quotaglance.providers.clinepass import ClinePassProvider
from quotaglance.providers.codebuff import CodebuffProvider
from quotaglance.providers.codex import CodexProvider
from quotaglance.providers.copilot import CopilotProvider
from quotaglance.providers.cursor import CursorProvider
from quotaglance.providers.deepseek import DeepSeekProvider
from quotaglance.providers.elevenlabs import ElevenLabsProvider
from quotaglance.providers.factory import FactoryProvider
from quotaglance.providers.gemini import GeminiProvider
from quotaglance.providers.jetbrains import JetBrainsProvider
from quotaglance.providers.kilo import KiloProvider
from quotaglance.providers.kimi import KimiProvider
from quotaglance.providers.kiro import KiroProvider
from quotaglance.providers.minimax import MiniMaxProvider
from quotaglance.providers.moonshot import MoonshotProvider
from quotaglance.providers.opencode import OpenCodeProvider
from quotaglance.providers.openrouter import OpenRouterProvider
from quotaglance.providers.poe import PoeProvider
from quotaglance.providers.synthetic import SyntheticProvider
from quotaglance.providers.vercel import VercelProvider
from quotaglance.providers.warp import WarpProvider
from quotaglance.providers.windsurf import WindsurfProvider
from quotaglance.providers.zai import ZaiProvider
from quotaglance.providers.zed import ZedProvider

PROVIDER_CLASSES: tuple[type[Provider], ...] = (
    # The tools most people hit limits on first.
    ClaudeProvider,
    CodexProvider,
    CursorProvider,
    CopilotProvider,
    GeminiProvider,
    KiroProvider,
    # Editors and IDEs.
    JetBrainsProvider,
    WindsurfProvider,
    ZedProvider,
    AntigravityProvider,
    KiloProvider,
    ClinePassProvider,
    # Coding agents.
    OpenCodeProvider,
    AmpProvider,
    AugmentProvider,
    FactoryProvider,
    WarpProvider,
    CodebuffProvider,
    # Coding plans and API platforms.
    ZaiProvider,
    KimiProvider,
    MiniMaxProvider,
    SyntheticProvider,
    OpenRouterProvider,
    DeepSeekProvider,
    MoonshotProvider,
    VercelProvider,
    ChutesProvider,
    PoeProvider,
    # Voice and media.
    ElevenLabsProvider,
)


def all_providers() -> list[Provider]:
    return [cls() for cls in PROVIDER_CLASSES]


def provider_class(provider_id: str) -> type[Provider] | None:
    for cls in PROVIDER_CLASSES:
        if cls.id == provider_id:
            return cls
    return None
