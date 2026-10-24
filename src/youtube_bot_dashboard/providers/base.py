"""Shared provider response and protocol types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ProviderError(RuntimeError):
    """Provider network, authorization, or response failure."""


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    text: str
    model_id: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None


class AIProvider(Protocol):
    provider_name: str
    model_id: str

    def generate_json(self, *, system_prompt: str, payload: dict) -> ProviderResponse:
        """Run one request and return the unparsed response text and usage."""
        ...
