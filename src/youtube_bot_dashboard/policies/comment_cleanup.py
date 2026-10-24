"""Conservative decision policy for historical YouTube comment cleanup.

This module does not call YouTube APIs and never deletes comments. It converts a
validated AI assessment into KEEP, REVIEW, or DELETION_CANDIDATE. The initial
workflow remains preview-first and requires owner approval for deletion.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math


class CommentTarget(StrEnum):
    CREATOR = "CREATOR"
    CONTENT = "CONTENT"
    CREATOR_AND_CONTENT = "CREATOR_AND_CONTENT"
    OTHER_TARGET = "OTHER_TARGET"
    MARKET_OR_THIRD_PARTY = "MARKET_OR_THIRD_PARTY"
    UNCERTAIN = "UNCERTAIN"


class Sentiment(StrEnum):
    NEGATIVE = "NEGATIVE"
    NEUTRAL = "NEUTRAL"
    POSITIVE = "POSITIVE"
    MIXED = "MIXED"
    UNCERTAIN = "UNCERTAIN"


class ConductType(StrEnum):
    PERSONAL_ABUSE = "PERSONAL_ABUSE"
    HARASSMENT = "HARASSMENT"
    THREAT = "THREAT"
    DOXXING_PRIVACY = "DOXXING_PRIVACY"
    SPAM_OR_SCAM = "SPAM_OR_SCAM"
    CONTENT_CRITICISM = "CONTENT_CRITICISM"
    RESPECTFUL_CORRECTION = "RESPECTFUL_CORRECTION"
    OFF_TOPIC_COMMENTARY = "OFF_TOPIC_COMMENTARY"
    SATIRE_OR_AMBIGUOUS = "SATIRE_OR_AMBIGUOUS"
    OTHER = "OTHER"
    UNCERTAIN = "UNCERTAIN"


class CleanupDecision(StrEnum):
    KEEP = "KEEP"
    REVIEW = "REVIEW"
    DELETION_CANDIDATE = "DELETION_CANDIDATE"


@dataclass(frozen=True, slots=True)
class CommentAssessment:
    """Validated classification supplied by an AI adapter or review tool."""

    comment_id: str
    video_id: str
    target: CommentTarget
    sentiment: Sentiment
    conduct_type: ConductType
    confidence: float
    rationale: str
    text: str = ""

    def __post_init__(self) -> None:
        if not self.comment_id.strip():
            raise ValueError("comment_id must not be empty")
        if not self.video_id.strip():
            raise ValueError("video_id must not be empty")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be a finite number between 0 and 1")
        if not self.rationale.strip():
            raise ValueError("rationale must not be empty")


@dataclass(frozen=True, slots=True)
class CleanupPolicy:
    """Configurable decision thresholds; does not enable auto-deletion."""

    high_confidence_threshold: float = 0.90

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.high_confidence_threshold)
            or not 0.0 <= self.high_confidence_threshold <= 1.0
        ):
            raise ValueError("high_confidence_threshold must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class CleanupResult:
    decision: CleanupDecision
    reason_code: str
    explanation: str
    requires_owner_approval: bool
    policy_version: str = "comment-cleanup-v1"


_RELEVANT_TARGETS = {
    CommentTarget.CREATOR,
    CommentTarget.CONTENT,
    CommentTarget.CREATOR_AND_CONTENT,
}

# These categories are protected as legitimate criticism/commentary even when
# the sentiment is negative. Target wording such as "you/your" is not enough
# to turn criticism of the creator's work into personal abuse.
_ALWAYS_KEEP_TYPES = {
    ConductType.CONTENT_CRITICISM,
    ConductType.RESPECTFUL_CORRECTION,
    ConductType.OFF_TOPIC_COMMENTARY,
}

_POTENTIAL_VIOLATION_TYPES = {
    ConductType.PERSONAL_ABUSE,
    ConductType.HARASSMENT,
    ConductType.THREAT,
    ConductType.DOXXING_PRIVACY,
    ConductType.SPAM_OR_SCAM,
}


def decide_cleanup(
    assessment: CommentAssessment,
    policy: CleanupPolicy | None = None,
) -> CleanupResult:
    """Apply conservative deterministic rules to one AI assessment.

    Negative sentiment alone can never make a comment a deletion candidate.
    This function only proposes candidates; it does not perform YouTube API
    operations. The application must still require owner approval.
    """

    policy = policy or CleanupPolicy()

    if assessment.target is CommentTarget.UNCERTAIN:
        return CleanupResult(
            CleanupDecision.REVIEW,
            "UNCERTAIN_TARGET",
            "The AI could not confidently determine whether the comment targets the creator or content.",
            requires_owner_approval=True,
        )

    if assessment.target not in _RELEVANT_TARGETS:
        return CleanupResult(
            CleanupDecision.KEEP,
            "OUT_OF_SCOPE_TARGET",
            "The comment does not clearly target the creator or their content; negative sentiment alone is not a reason to delete it.",
            requires_owner_approval=False,
        )

    if assessment.conduct_type in _ALWAYS_KEEP_TYPES:
        return CleanupResult(
            CleanupDecision.KEEP,
            "LEGITIMATE_CRITICISM_OR_COMMENTARY",
            "This is classified as criticism, correction, or commentary rather than a separate conduct violation. Keep it published by default.",
            requires_owner_approval=False,
        )

    if assessment.conduct_type in {
        ConductType.UNCERTAIN,
        ConductType.SATIRE_OR_AMBIGUOUS,
        ConductType.OTHER,
    }:
        return CleanupResult(
            CleanupDecision.REVIEW,
            "AMBIGUOUS_CONDUCT",
            "The conduct label does not establish a specific violation. Review the original wording and context before deciding.",
            requires_owner_approval=True,
        )

    if assessment.conduct_type not in _POTENTIAL_VIOLATION_TYPES:
        return CleanupResult(
            CleanupDecision.REVIEW,
            "UNMAPPED_CONDUCT",
            "The conduct category is not mapped to a cleanup action; review is required.",
            requires_owner_approval=True,
        )

    # For a possible violation, uncertain or mixed sentiment raises ambiguity;
    # only clear threats/privacy violations use the exception for urgent review.
    if assessment.sentiment in {Sentiment.UNCERTAIN, Sentiment.MIXED} and assessment.conduct_type not in {
        ConductType.THREAT,
        ConductType.DOXXING_PRIVACY,
    }:
        return CleanupResult(
            CleanupDecision.REVIEW,
            "AMBIGUOUS_SENTIMENT",
            "The message has mixed or uncertain tone. Human review is required before proposing deletion.",
            requires_owner_approval=True,
        )

    if assessment.confidence < policy.high_confidence_threshold:
        return CleanupResult(
            CleanupDecision.REVIEW,
            "BELOW_CONFIDENCE_THRESHOLD",
            f"Confidence {assessment.confidence:.2f} is below the configured threshold {policy.high_confidence_threshold:.2f}.",
            requires_owner_approval=True,
        )

    return CleanupResult(
        CleanupDecision.DELETION_CANDIDATE,
        "HIGH_CONFIDENCE_POLICY_VIOLATION",
        "The assessment identifies a high-confidence potential conduct violation directed at the creator or content. This is a candidate only; owner approval is still required and no deletion is performed here.",
        requires_owner_approval=True,
    )
