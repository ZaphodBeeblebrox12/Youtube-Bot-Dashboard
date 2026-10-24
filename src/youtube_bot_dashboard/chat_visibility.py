"""Fail-closed visibility rules for the private live-chat dashboard.

A message is displayed by default only after the moderation pipeline gives a
high-confidence NO_VIOLATION verdict and Reply Finder independently selects it
as useful or constructive. This is a display filter, not YouTube enforcement.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

AI_WHITELIST_MIN_MODERATION_CONFIDENCE = 0.90


def is_ai_whitelisted_for_display(
    *,
    moderation_state: str | None,
    moderation_result: Mapping[str, Any] | None,
    reply_finder_state: str | None,
    reply_finder_result: Mapping[str, Any] | None,
    min_moderation_confidence: float = AI_WHITELIST_MIN_MODERATION_CONFIDENCE,
) -> tuple[bool, str]:
    """Return whether a message is safe and useful enough for the default feed.

    The filter fails closed: missing, incomplete, malformed, uncertain, flagged,
    or low-confidence safety results never enter the default visible feed.
    """
    if moderation_state != "COMPLETED" or not isinstance(moderation_result, Mapping):
        return False, "Waiting for a completed AI safety check"

    category = moderation_result.get("category")
    if category != "NO_VIOLATION":
        return False, f"Safety check did not clear the message ({category or 'unknown result'})"

    confidence = moderation_result.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0.0 <= confidence <= 1.0
        or confidence < min_moderation_confidence
    ):
        return False, f"Safety confidence is below the {min_moderation_confidence:.0%} whitelist threshold"

    if reply_finder_state != "COMPLETED" or not isinstance(reply_finder_result, Mapping):
        return False, "Waiting for a completed AI usefulness check"

    if reply_finder_result.get("selected") is not True:
        return False, "Not selected as useful or constructive by Reply Finder"

    return True, "AI-whitelisted: safety cleared and selected as useful/constructive"
