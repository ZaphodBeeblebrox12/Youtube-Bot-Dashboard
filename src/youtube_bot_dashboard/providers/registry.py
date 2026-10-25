"""Build only the AI providers whose credentials/configuration are present."""

from __future__ import annotations

from youtube_bot_dashboard.live_queue import ProviderName
from youtube_bot_dashboard.providers.base import AIProvider, ProviderError


class ProviderRegistry:
    def __init__(self, providers: dict[ProviderName, AIProvider] | None = None):
        self._providers = providers or {}

    @classmethod
    def from_environment(cls) -> "ProviderRegistry":
        providers: dict[ProviderName, AIProvider] = {}
        # Import lazily so installations without optional SDKs can still run
        # the dashboard and tests without AI credentials.
        from youtube_bot_dashboard.providers.deepseek import DeepSeekProvider
        from youtube_bot_dashboard.providers.gemini import GeminiProvider
        from youtube_bot_dashboard.providers.kimi import KimiProvider
        from youtube_bot_dashboard.providers.qwen import QwenProvider

        try:
            providers[ProviderName.GEMINI] = GeminiProvider()
        except ProviderError:
            pass
        try:
            providers[ProviderName.QWEN] = QwenProvider()
        except ProviderError:
            pass
        try:
            providers[ProviderName.DEEPSEEK] = DeepSeekProvider()
        except ProviderError:
            pass
        try:
            providers[ProviderName.KIMI] = KimiProvider()
        except ProviderError:
            pass
        return cls(providers)

    def available(self) -> tuple[ProviderName, ...]:
        order = (ProviderName.GEMINI, ProviderName.QWEN, ProviderName.DEEPSEEK, ProviderName.KIMI)
        return tuple(provider for provider in order if provider in self._providers)

    def get(self, provider: ProviderName) -> AIProvider:
        adapter = self._providers.get(provider)
        if adapter is None:
            raise ProviderError(f"Provider {provider.value} is not configured.")
        return adapter

    def has(self, provider: ProviderName) -> bool:
        return provider in self._providers
