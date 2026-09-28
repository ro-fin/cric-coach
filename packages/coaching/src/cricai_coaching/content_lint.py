"""Age-appropriateness & honesty content lint (US-G2, US-H4, US-I5, US-K4).

Every kid-facing or wellness string in the system must pass this lint; it runs
in CI as part of the SAF suite and can never be waived.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Absolutist / shaming language banned from findings and reports (US-G2).
BANNED_COACHING_PHRASES: tuple[str, ...] = (
    "you always",
    "you never",
    "lazy",
    "useless",
    "hopeless",
    "too fat",
    "too slow for",
    "overweight",
    "skinny",
)

#: Medical / diagnostic language banned from wellness copy (US-H4: never diagnoses).
BANNED_MEDICAL_PHRASES: tuple[str, ...] = (
    "stress fracture",
    "diagnos",
    "prescri",
    "you have injured",
    "torn ligament",
    "tendinitis",
    "you should take medication",
)

#: Unmeasurable spin claims banned from bowling UI/reports (US-I5 honest limits).
BANNED_SPIN_CLAIMS: tuple[str, ...] = (
    "rpm",
    "revs per",
    "spin rate",
    "spin axis",
    "seam axis",
)


@dataclass(frozen=True)
class LintViolation:
    phrase: str
    index: int
    context: str


def find_banned_phrases(text: str, banned: tuple[str, ...]) -> list[LintViolation]:
    """Case-insensitive scan; returns every match with surrounding context."""
    violations: list[LintViolation] = []
    lowered = text.lower()
    for phrase in banned:
        for match in re.finditer(re.escape(phrase.lower()), lowered):
            start, end = match.start(), match.end()
            context = text[max(0, start - 20) : min(len(text), end + 20)]
            violations.append(LintViolation(phrase=phrase, index=start, context=context))
    return sorted(violations, key=lambda v: v.index)


def assert_kid_safe(text: str) -> None:
    """Raise if coaching copy violates the age-appropriateness rules."""
    violations = find_banned_phrases(text, BANNED_COACHING_PHRASES + BANNED_MEDICAL_PHRASES)
    if violations:
        detail = "; ".join(f"{v.phrase!r} at {v.index}" for v in violations)
        raise ValueError(f"kid-facing copy failed content lint: {detail}")
