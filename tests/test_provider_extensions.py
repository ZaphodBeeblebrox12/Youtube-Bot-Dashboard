import sys
from types import ModuleType, SimpleNamespace

import pytest

from youtube_bot_dashboard.live_queue import ProviderName
from youtube_bot_dashboard.providers.base import ProviderError
from youtube_bot_dashboard.providers.deepseek import DeepSeekProvider
from youtube_bot_dashboard.providers.gemini import GeminiProvider
from youtube_bot_dashboard.providers.kimi import KimiProvider
from youtube_bot_dashboard.providers.registry import ProviderRegistry
from youtube_bot_dashboard.providers.qwen import QwenProvider


class FakeCompletions:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def fake_client(response):
    completions = FakeCompletions(response)
    return SimpleNamespace(
        chat=SimpleNamespace(completions=completions),
        completions_for_test=completions,
    )


def response_with_usage(*, cached=None, cache_write=None, prompt_cache_hit=None):
    details = SimpleNamespace(cached_tokens=cached, cache_write_tokens=cache_write)
    usage = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=20,
        prompt_tokens_details=details,
        prompt_cache_hit_tokens=prompt_cache_hit,
    )
    choice = SimpleNamespace(message=SimpleNamespace(content='{"results": []}'))
    return SimpleNamespace(choices=[choice], usage=usage)


def test_deepseek_adapter_reports_cached_input_tokens_and_json_mode():
    client = fake_client(response_with_usage(prompt_cache_hit=45))
    provider = DeepSeekProvider(api_key="test-key", model_id="deepseek-chat", client=client)

    result = provider.generate_json(system_prompt="Be precise", payload={"messages": []})

    assert result.text == '{"results": []}'
    assert result.model_id == "deepseek-chat"
    assert result.input_tokens == 100
    assert result.output_tokens == 20
    assert result.cached_input_tokens == 45
    assert result.cache_write_tokens is None
    assert client.completions_for_test.kwargs["response_format"] == {"type": "json_object"}
    assert client.completions_for_test.kwargs["messages"][0]["content"] == "Be precise"


def test_deepseek_adapter_falls_back_to_prompt_token_details():
    client = fake_client(response_with_usage(cached=37))
    provider = DeepSeekProvider(api_key="test-key", model_id="deepseek-chat", client=client)

    result = provider.generate_json(system_prompt="instructions", payload={})

    assert result.cached_input_tokens == 37


def test_kimi_adapter_reports_cache_reads_and_writes_without_forcing_temperature():
    client = fake_client(response_with_usage(cached=30, cache_write=25))
    provider = KimiProvider(api_key="test-key", model_id="kimi-test", client=client)

    result = provider.generate_json(system_prompt="Be precise", payload={"messages": []})

    assert result.input_tokens == 100
    assert result.output_tokens == 20
    assert result.cached_input_tokens == 30
    assert result.cache_write_tokens == 25
    assert "temperature" not in client.completions_for_test.kwargs
    assert client.completions_for_test.kwargs["response_format"] == {"type": "json_object"}


def test_provider_registry_available_order_includes_four_providers():
    providers = {provider: object() for provider in (
        ProviderName.GEMINI, ProviderName.QWEN, ProviderName.DEEPSEEK, ProviderName.KIMI
    )}

    registry = ProviderRegistry(providers)

    assert registry.available() == (
        ProviderName.GEMINI, ProviderName.QWEN, ProviderName.DEEPSEEK, ProviderName.KIMI
    )


def test_deepseek_requires_key_and_model(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)

    with pytest.raises(ProviderError):
        DeepSeekProvider()




def test_gemini_adapter_includes_reported_thinking_tokens_in_billable_output(monkeypatch):
    google_module = ModuleType("google")
    genai_module = ModuleType("google.genai")
    genai_module.types = SimpleNamespace(
        GenerateContentConfig=lambda **kwargs: kwargs
    )
    google_module.genai = genai_module
    monkeypatch.setitem(sys.modules, "google", google_module)
    monkeypatch.setitem(sys.modules, "google.genai", genai_module)

    usage = SimpleNamespace(
        prompt_token_count=100,
        candidates_token_count=35,
        thoughts_token_count=20,
        cached_content_token_count=10,
    )
    response = SimpleNamespace(text='{"results": []}', usage_metadata=usage)
    client = SimpleNamespace(
        models=SimpleNamespace(generate_content=lambda **kwargs: response)
    )
    provider = GeminiProvider(api_key="test-key", model_id="gemini-thinking-test", client=client)

    result = provider.generate_json(system_prompt="Test instruction", payload={})

    assert result.input_tokens == 100
    assert result.output_tokens == 55
    assert result.cached_input_tokens == 10



def test_qwen_adapter_reads_nested_cached_and_cache_write_tokens():
    client = fake_client(response_with_usage(cached=40, cache_write=15))
    provider = QwenProvider(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        model_id="qwen-test",
        client=client,
    )

    result = provider.generate_json(system_prompt="instructions", payload={})

    assert result.input_tokens == 100
    assert result.output_tokens == 20
    assert result.cached_input_tokens == 40
    assert result.cache_write_tokens == 15


def test_qwen_adapter_reads_documented_top_level_cached_tokens_variant():
    usage = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=20,
        prompt_tokens_details=None,
        cached_tokens=30,
    )
    choice = SimpleNamespace(message=SimpleNamespace(content='{"results": []}'))
    client = fake_client(SimpleNamespace(choices=[choice], usage=usage))
    provider = QwenProvider(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        model_id="qwen-test",
        client=client,
    )

    result = provider.generate_json(system_prompt="instructions", payload={})

    assert result.input_tokens == 100
    assert result.cached_input_tokens == 30
    assert result.cache_write_tokens is None
