import pytest

from youtube_bot_dashboard.chat_visibility import (
    AI_WHITELIST_MIN_MODERATION_CONFIDENCE,
    is_ai_whitelisted_for_display,
)

SAFE = {"category": "NO_VIOLATION", "confidence": 0.98}
USEFUL = {"selected": True, "confidence": 0.92}


def decide(*, moderation_state="COMPLETED", moderation_result=SAFE,
           reply_state="COMPLETED", reply_result=USEFUL, is_super_chat=False):
    return is_ai_whitelisted_for_display(
        moderation_state=moderation_state,
        moderation_result=moderation_result,
        reply_finder_state=reply_state,
        reply_finder_result=reply_result,
        is_super_chat=is_super_chat,
    )


def test_whitelist_requires_both_safety_and_usefulness_checks():
    allowed, reason = decide()
    assert allowed is True
    assert "AI-whitelisted" in reason


@pytest.mark.parametrize("state", [None, "PENDING", "SENT", "FAILED_RETRYABLE", "FAILED_FINAL"])
def test_incomplete_safety_check_fails_closed(state):
    allowed, _ = decide(moderation_state=state)
    assert allowed is False


@pytest.mark.parametrize("category", ["HARASSMENT", "THREATS", "SPAM_FLOODING", "UNCERTAIN", None])
def test_flagged_uncertain_or_unknown_safety_result_is_hidden(category):
    allowed, _ = decide(moderation_result={"category": category, "confidence": 0.99})
    assert allowed is False


def test_low_confidence_safety_result_is_hidden():
    allowed, reason = decide(moderation_result={"category": "NO_VIOLATION", "confidence": 0.89})
    assert allowed is False
    assert f"{AI_WHITELIST_MIN_MODERATION_CONFIDENCE:.0%}" in reason


@pytest.mark.parametrize("reply_state", [None, "PENDING", "SENT", "FAILED_RETRYABLE"])
def test_message_is_hidden_until_usefulness_check_completes(reply_state):
    allowed, _ = decide(reply_state=reply_state)
    assert allowed is False


@pytest.mark.parametrize("selected", [False, None, "true", 1])
def test_safe_but_not_selected_message_is_hidden_as_unproductive(selected):
    allowed, _ = decide(reply_result={"selected": selected})
    assert allowed is False


def test_malformed_or_missing_safety_confidence_fails_closed():
    for confidence in (None, True, float("nan"), float("inf"), -0.1, 1.1):
        allowed, _ = decide(moderation_result={"category": "NO_VIOLATION", "confidence": confidence})
        assert allowed is False

def test_safe_super_chat_is_visible_even_if_reply_finder_does_not_select_it():
    allowed, reason = decide(
        is_super_chat=True,
        reply_state="COMPLETED",
        reply_result={"selected": False},
    )
    assert allowed is True
    assert "Super Chat prioritized" in reason


def test_super_chat_still_requires_completed_high_confidence_safety_clearance():
    assert decide(is_super_chat=True, moderation_state="PENDING")[0] is False
    assert decide(
        is_super_chat=True,
        moderation_result={"category": "HARASSMENT", "confidence": 0.99},
    )[0] is False
    assert decide(
        is_super_chat=True,
        moderation_result={"category": "NO_VIOLATION", "confidence": 0.89},
    )[0] is False
