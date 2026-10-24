from youtube_bot_dashboard.prompts.comment_cleanup import COMMENT_CLEANUP_SYSTEM_PROMPT


def test_prompt_protects_genuine_negative_criticism():
    assert "This lesson is terrible and your explanation makes no sense." in COMMENT_CLEANUP_SYSTEM_PROMPT
    assert "Negative sentiment is NOT itself a conduct violation" in COMMENT_CLEANUP_SYSTEM_PROMPT
    assert 'A message is not a personal attack merely because it uses “you” or “your”.' in COMMENT_CLEANUP_SYSTEM_PROMPT
    assert "MANDATORY KEEP EXAMPLES" in COMMENT_CLEANUP_SYSTEM_PROMPT


def test_prompt_requires_structured_json_and_exact_ids():
    assert '"comments"' in COMMENT_CLEANUP_SYSTEM_PROMPT
    assert '"comment_id": "exact supplied comment ID"' in COMMENT_CLEANUP_SYSTEM_PROMPT
    assert "Copy every supplied comment_id and video_id exactly." in COMMENT_CLEANUP_SYSTEM_PROMPT
    assert "do not include a deletion command" in COMMENT_CLEANUP_SYSTEM_PROMPT
