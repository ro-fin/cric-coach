"""SAF: content lint — release-gating, never waivable."""

import pytest
from cricai_coaching.content_lint import (
    BANNED_COACHING_PHRASES,
    BANNED_MEDICAL_PHRASES,
    BANNED_SPIN_CLAIMS,
    assert_kid_safe,
    find_banned_phrases,
)

pytestmark = pytest.mark.safety


def test_clean_coaching_copy_passes() -> None:
    assert_kid_safe(
        "Great session! Front foot moved toward the ball on 34 of 42 full balls. "
        "Tomorrow: 80-ball cover-drive block; goal 70% controlled contact."
    )


def test_absolutist_shaming_is_caught() -> None:
    with pytest.raises(ValueError, match="you always"):
        assert_kid_safe("You always plant your foot too straight.")


def test_medical_diagnosis_language_is_caught() -> None:
    with pytest.raises(ValueError, match="diagnos"):
        assert_kid_safe("This looks like a diagnosable stress issue.")


def test_case_insensitive_matching() -> None:
    violations = find_banned_phrases("YOU NEVER watch the ball", BANNED_COACHING_PHRASES)
    assert [v.phrase for v in violations] == ["you never"]
    assert violations[0].index == 0


def test_multiple_violations_sorted_by_position() -> None:
    text = "spin rate is high and rpm is 2400 and the spin axis tilted"
    violations = find_banned_phrases(text, BANNED_SPIN_CLAIMS)
    assert [v.phrase for v in violations] == ["spin rate", "rpm", "spin axis"]
    assert violations[0].context.startswith("spin rate")


def test_no_violations_on_empty_text() -> None:
    assert find_banned_phrases("", BANNED_COACHING_PHRASES) == []


def test_banned_lists_are_lowercase_stable() -> None:
    for phrase in BANNED_COACHING_PHRASES + BANNED_MEDICAL_PHRASES + BANNED_SPIN_CLAIMS:
        assert phrase == phrase.lower()
