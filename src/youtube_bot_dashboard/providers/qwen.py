"""Qwen adapter via a configurable OpenAI-compatible endpoint.

The API base URL is deliberately required: Qwen availability and endpoints vary
by service/region. No endpoint or model is hard-coded.
"""

from __future__ import annotations

import json
import os

from youtube_bot_dashboard.providers.base import ProviderError, ProviderResponse


class QwenProvider:
    provider_name = "QWEN"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model_id: str | None = None,
        client=None,
    ):
        self.api_key = api_key or os.getenv("QWEN_API_KEY")
        self.base_url = base_url or os.getenv("QWEN_BASE_URL")
        self.model_id = model_id or os.getenv("QWEN_MODEL")
        if not self.api_key or not self.base_url or not self.model_id:
            raise ProviderError("Set QWEN_API_KEY, QWEN_BASE_URL, and QWEN_MODEL to enable Qwen.")
        try:
            if client is None:
                from openai import OpenAI
                client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        except ImportError as exc:
            raise ProviderError("Install the optional AI dependencies to use Qwen.") from exc
        self.client = client

    def generate_json(self, *, system_prompt: str, payload: dict) -> ProviderResponse:
        try:
            response = self.client.chat.completions.create(
                model=self.model_id,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
            )
        except Exception as exc:  # SDK/API exceptions vary by endpoint
            raise ProviderError(f"Qwen request failed: {exc}") from exc

        choices = getattr(response, "choices", None)
        if not choices or not getattr(choices[0], "message", None):
            raise ProviderError("Qwen returned no message choice.")
        text = choices[0].message.content
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("Qwen returned an empty response.")
        usage = getattr(response, "usage", None)
        prompt_details = getattr(usage, "prompt_tokens_details", None) if usage else None
        cached = getattr(prompt_details, "cached_tokens", None) if prompt_details else None
        return ProviderResponse(
            text=text,
            model_id=self.model_id,
            input_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
            output_tokens=getattr(usage, "completion_tokens", None) if usage else None,
            cached_input_tokens=cached,
        )
