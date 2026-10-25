"""Google Gemini adapter. Credentials and model are supplied via environment."""

from __future__ import annotations

import json
import os

from youtube_bot_dashboard.providers.base import ProviderError, ProviderResponse


class GeminiProvider:
    provider_name = "GEMINI"

    def __init__(self, *, api_key: str | None = None, model_id: str | None = None, client=None):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.model_id = model_id or os.getenv("GEMINI_MODEL")
        if not self.api_key or not self.model_id:
            raise ProviderError("Set GEMINI_API_KEY and GEMINI_MODEL to enable Gemini.")
        try:
            if client is None:
                from google import genai
                client = genai.Client(api_key=self.api_key)
            from google.genai import types
        except ImportError as exc:
            raise ProviderError("Install the optional AI dependencies to use Gemini.") from exc
        self.client = client
        self._types = types

    def generate_json(self, *, system_prompt: str, payload: dict) -> ProviderResponse:
        try:
            response = self.client.models.generate_content(
                model=self.model_id,
                contents=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                config=self._types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )
        except Exception as exc:  # SDK exceptions vary by version
            raise ProviderError(f"Gemini request failed: {exc}") from exc

        text = getattr(response, "text", None)
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("Gemini returned an empty response.")
        usage = getattr(response, "usage_metadata", None)
        candidate_tokens = getattr(usage, "candidates_token_count", None) if usage else None
        thought_tokens = getattr(usage, "thoughts_token_count", None) if usage else None
        # Gemini bills thinking tokens at the output-token rate for thinking models.
        # Keep output usage unknown if the candidate-token count itself is missing.
        billable_output_tokens = (
            None if candidate_tokens is None else candidate_tokens + (thought_tokens or 0)
        )
        return ProviderResponse(
            text=text,
            model_id=self.model_id,
            input_tokens=getattr(usage, "prompt_token_count", None) if usage else None,
            output_tokens=billable_output_tokens,
            cached_input_tokens=getattr(usage, "cached_content_token_count", None) if usage else None,
        )
