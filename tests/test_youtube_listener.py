from datetime import datetime, timezone
from types import SimpleNamespace

from youtube_bot_dashboard.youtube_listener import extract_viewer_text


class FakeProto:
    def __init__(self, present_fields=(), **values):
        self._present_fields = set(present_fields)
        for key, value in values.items():
            setattr(self, key, value)

    def HasField(self, field):
        return field in self._present_fields


def test_extract_super_chat_amount_tier_and_comment_from_youtube_metadata():
    details = FakeProto(
        present_fields={"amount_micros", "currency", "amount_display_string", "user_comment", "tier"},
        amount_micros=500_000_000,
        currency="INR",
        amount_display_string="₹500.00",
        user_comment="Please explain risk management next.",
        tier=2,
    )
    snippet = FakeProto(
        present_fields={"super_chat_details"},
        super_chat_details=details,
        author_channel_id="channel-paid",
        published_at="2026-10-25T10:00:00Z",
        display_message="A ₹500.00 Super Chat",
    )
    author = FakeProto(
        present_fields={"is_chat_sponsor"},
        channel_id="channel-paid",
        display_name="Paid Viewer",
        is_chat_sponsor=True,
    )
    message = SimpleNamespace(id="youtube-paid-1", snippet=snippet, author_details=author)

    assert extract_viewer_text(message) == (
        "Please explain risk management next.",
        "channel-paid",
        "Paid Viewer",
        datetime(2026, 10, 25, 10, 0, tzinfo=timezone.utc),
        True,
        True,
        500_000_000,
        "INR",
        "₹500.00",
        2,
    )


def test_super_chat_without_user_comment_is_still_ingested_as_a_paid_event():
    details = FakeProto(
        present_fields={"amount_micros", "currency", "amount_display_string"},
        amount_micros=50_000_000,
        currency="INR",
        amount_display_string="₹50.00",
    )
    snippet = FakeProto(
        present_fields={"super_chat_details"},
        super_chat_details=details,
        author_channel_id="channel-paid",
        display_message="A ₹50.00 Super Chat",
    )
    message = SimpleNamespace(id="youtube-paid-2", snippet=snippet, author_details=None)

    extracted = extract_viewer_text(message)

    assert extracted is not None
    assert extracted[0] == "A ₹50.00 Super Chat"
    assert extracted[5] is True
    assert extracted[6] == 50_000_000
    assert extracted[7] == "INR"
