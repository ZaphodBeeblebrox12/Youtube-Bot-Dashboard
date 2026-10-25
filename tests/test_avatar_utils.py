import pytest

from youtube_bot_dashboard.avatar_utils import normalize_avatar_url


def test_normalize_avatar_url_trims_valid_https_url():
    assert normalize_avatar_url("  https://yt3.ggpht.com/avatar  ") == "https://yt3.ggpht.com/avatar"


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "http://example.com/avatar.png",
        "javascript:alert(1)",
        "https:///missing-host",
        "https://user:pass@example.com/avatar.png",
        "https://example.com/a b.png",
        "https://example.com/" + ("x" * 2050),
    ],
)
def test_normalize_avatar_url_rejects_unsafe_or_invalid_values(value):
    assert normalize_avatar_url(value) is None
