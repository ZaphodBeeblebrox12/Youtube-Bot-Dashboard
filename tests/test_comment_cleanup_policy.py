import pytest

from youtube_bot_dashboard.policies.comment_cleanup import (
    CleanupDecision,
    CleanupPolicy,
    CommentAssessment,
    CommentTarget,
    ConductType,
    Sentiment,
    decide_cleanup,
)


def assessment(
    *,
    target=CommentTarget.CONTENT,
    sentiment=Sentiment.NEGATIVE,
    conduct=ConductType.CONTENT_CRITICISM,
    confidence=0.99,
    text="This lesson is terrible and your explanation makes no sense.",
):
    return CommentAssessment(
        comment_id="comment-1",
        video_id="video-1",
        target=target,
        sentiment=sentiment,
        conduct_type=conduct,
        confidence=confidence,
        rationale="Classification based on the supplied comment.",
        text=text,
    )


def test_genuine_blunt_content_criticism_is_kept():
    result = decide_cleanup(assessment())
    assert result.decision is CleanupDecision.KEEP
    assert result.reason_code == "LEGITIMATE_CRITICISM_OR_COMMENTARY"
    assert not result.requires_owner_approval


def test_second_person_criticism_is_not_automatically_personal_abuse():
    result = decide_cleanup(
        assessment(
            conduct=ConductType.CONTENT_CRITICISM,
            text="Your explanation makes no sense.",
        )
    )
    assert result.decision is CleanupDecision.KEEP


def test_reasoned_correction_is_kept():
    result = decide_cleanup(
        assessment(
            conduct=ConductType.RESPECTFUL_CORRECTION,
            text="Your conclusion is wrong because the chart shows the opposite.",
        )
    )
    assert result.decision is CleanupDecision.KEEP


def test_negative_market_comment_is_out_of_scope():
    result = decide_cleanup(
        assessment(
            target=CommentTarget.MARKET_OR_THIRD_PARTY,
            conduct=ConductType.OFF_TOPIC_COMMENTARY,
            text="NIFTY is terrible today.",
        )
    )
    assert result.decision is CleanupDecision.KEEP
    assert result.reason_code == "OUT_OF_SCOPE_TARGET"


def test_clear_personal_abuse_is_candidate_but_never_auto_deleted():
    result = decide_cleanup(
        assessment(
            target=CommentTarget.CREATOR,
            conduct=ConductType.PERSONAL_ABUSE,
            text="You are an idiot and should never teach anyone.",
        )
    )
    assert result.decision is CleanupDecision.DELETION_CANDIDATE
    assert result.requires_owner_approval


@pytest.mark.parametrize("conduct", [ConductType.THREAT, ConductType.DOXXING_PRIVACY])
def test_critical_violation_is_candidate_when_targeted_and_confident(conduct):
    result = decide_cleanup(
        assessment(
            target=CommentTarget.CREATOR,
            conduct=conduct,
            sentiment=Sentiment.MIXED,
        )
    )
    assert result.decision is CleanupDecision.DELETION_CANDIDATE
    assert result.requires_owner_approval


def test_low_confidence_goes_to_review():
    result = decide_cleanup(
        assessment(
            target=CommentTarget.CREATOR,
            conduct=ConductType.PERSONAL_ABUSE,
            confidence=0.75,
        )
    )
    assert result.decision is CleanupDecision.REVIEW
    assert result.reason_code == "BELOW_CONFIDENCE_THRESHOLD"


def test_uncertain_target_goes_to_review():
    result = decide_cleanup(assessment(target=CommentTarget.UNCERTAIN))
    assert result.decision is CleanupDecision.REVIEW


def test_satire_or_ambiguous_conduct_goes_to_review():
    result = decide_cleanup(assessment(conduct=ConductType.SATIRE_OR_AMBIGUOUS))
    assert result.decision is CleanupDecision.REVIEW


def test_spam_or_scam_is_only_candidate_when_target_is_in_scope_and_confident():
    targeted = decide_cleanup(
        assessment(
            target=CommentTarget.CONTENT,
            conduct=ConductType.SPAM_OR_SCAM,
            text="Fake giveaway link posted under the lesson.",
        )
    )
    unrelated = decide_cleanup(
        assessment(
            target=CommentTarget.OTHER_TARGET,
            conduct=ConductType.SPAM_OR_SCAM,
            text="A spam link about an unrelated product.",
        )
    )
    assert targeted.decision is CleanupDecision.DELETION_CANDIDATE
    assert unrelated.decision is CleanupDecision.KEEP


def test_policy_threshold_validation():
    with pytest.raises(ValueError):
        CleanupPolicy(high_confidence_threshold=1.5)


def test_assessment_requires_ids_and_valid_confidence():
    with pytest.raises(ValueError):
        assessment(confidence=-0.1)
    with pytest.raises(ValueError):
        CommentAssessment(
            comment_id="",
            video_id="video-1",
            target=CommentTarget.CONTENT,
            sentiment=Sentiment.NEGATIVE,
            conduct_type=ConductType.PERSONAL_ABUSE,
            confidence=0.99,
            rationale="Valid rationale",
        )
