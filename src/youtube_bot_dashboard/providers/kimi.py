"""Kimi API adapter using Moonshot's OpenAI-compatible Chat Completions API."""

from __future__ import annotations

import json
import os

from youtube_bot_dashboard.providers.base import ProviderError, ProviderResponse


def _field(value, name: str, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


class KimiProvider:
    provider_name = "KIMI"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model_id: str | None = None,
        client=None,
    ):
        self.api_key = api_key or os.getenv("KIMI_API_KEY") or os.getenv("MOONSHOT_API_KEY")
        self.base_url = base_url or os.getenv("KIMI_BASE_URL", "https://api.moonshot.ai/v1")
        self.model_id = model_id or os.getenv("KIMI_MODEL")
        if not self.api_key or not self.model_id:
            raise ProviderError("Set KIMI_API_KEY (or MOONSHOT_API_KEY) and KIMI_MODEL to enable Kimi.")
        try:
            if client is None:
                from openai import OpenAI
                client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        except ImportError as exc:
            raise ProviderError("Install the optional AI dependencies to use Kimi.") from exc
        self.client = client

    def generate_json(self, *, system_prompt: str, payload: dict) -> ProviderResponse:
        try:
            # Do not force a temperature: some current Kimi model families fix
            # their sampling parameters and reject overrides. Prefix caching is
            # managed automatically by the compatible API for supported models.
            response = self.client.chat.completions.create(
                model=self.model_id,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
                ],
                response_format={"type": "json_object"},
            )
        except Exception as exc:  # SDK exceptions vary by version
            raise ProviderError(f"Kimi request failed: {exc}") from exc

        choices = _field(response, "choices")
        message = _field(choices[0], "message") if choices else None
        text = _field(message, "content") if message else None
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("Kimi returned an empty response.")

        usage = _field(response, "usage")
        details = _field(usage, "prompt_tokens_details")
        cached = _field(details, "cached_tokens")
        cache_write = _field(details, "cache_write_tokens")
        return ProviderResponse(
            text=text,
            model_id=self.model_id,
            input_tokens=_field(usage, "prompt_tokens"),
            output_tokens=_field(usage, "completion_tokens"),
            cached_input_tokens=cached,
            cache_write_tokens=cache_write,
        )
