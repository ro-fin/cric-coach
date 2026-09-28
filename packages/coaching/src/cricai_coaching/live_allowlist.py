"""US-L5 SAF: the live-surface allow-list validator.

Live mode, when enabled, is restricted to low-risk surfaces — ball counts,
target-hit tally, workload countdown, fatigue nudge (US-L5 AC). Three layers
enforce that, in order:

1. :data:`ALLOWED_LIVE_KEYS` — a CLOSED registry of every key that may ever be
   stored in ``app_settings.settings['live_mode']['allowlist']`` or emitted
   live. Widening the live surface is an explicit code registration here (a
   product decision behind coach opt-in), never a config edit.
2. The stored allowlist — the guardian-approved subset of the registry that
   the live seam may actually emit.
3. :data:`NEVER_LIVE` — the hard-coded safety net underneath both: per-ball
   technique verdicts, correction surfaces AND the strict identity/free-text
   PII set are rejected at ANY DEPTH of a payload, EVEN IF someone writes them
   into the stored allowlist by hand.

Every live emission path must call :func:`validate_live_payload`; the settings
API calls :func:`assert_storable_allowlist` at write time (defense in depth —
the stored list can never even contain a never-live or unregistered key via
the API, and the validator still rejects one smuggled in by hand).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from cricai_data.ballrecord import BOWLING_FIELDS

from cricai_coaching.llm_writer import FORBIDDEN_PAYLOAD_KEYS

__all__ = [
    "ALLOWED_LIVE_KEYS",
    "NEVER_LIVE",
    "LiveSurfaceViolation",
    "assert_storable_allowlist",
    "validate_live_payload",
]


class LiveSurfaceViolation(ValueError):
    """A live-surface payload (or stored allowlist) broke the US-L5 SAF contract."""


#: The CLOSED registry of live-storable keys (US-L5: counts and safety nudges
#: only — the four seeded surfaces). Any future live key must be registered
#: here explicitly; arbitrary strings are rejected at settings-write time AND
#: at emission, so the live surface can never widen via configuration alone.
ALLOWED_LIVE_KEYS: frozenset[str] = frozenset(
    {
        "ball_count",
        "target_hit_tally",
        "workload_remaining_balls",
        "fatigue_nudge",
    }
)

#: Keys that may NEVER appear on a live surface (at any depth), allow-listed or
#: not. Every BallRecord v1.1 per-ball bowling verdict field (release geometry,
#: brace state, turn/flight, per-ball target_hit, variation labels), the
#: batting technique metrics, every report correction surface, plus the strict
#: identity/free-text PII set mirrored from the API egress layer
#: (:data:`cricai_coaching.llm_writer.FORBIDDEN_PAYLOAD_KEYS` — body, notes,
#: pain_note, soreness, note, setup, player_name, birthdate, object_key). The
#: aggregate ``target_hit_tally`` counter is a different key and stays allowed.
NEVER_LIVE: frozenset[str] = (
    frozenset(BOWLING_FIELDS)
    | FORBIDDEN_PAYLOAD_KEYS
    | frozenset(
        {
            # Report/correction surfaces (US-G3 body fields): corrections are
            # post-session, evidence-linked and coach-reviewable — never live.
            "main_correction",
            "secondary",
            "finding",
            "findings",
            "drill",
            "drill_plan",
            "technique_correction",
            "correction",
            # Per-ball batting technique verdicts (BallRecord/fatigue metrics).
            "head_stability_score",
            "front_foot_direction_cm",
            "footwork_score",
            "trigger_to_contact_ms",
            "contact_quality",
        }
    )
)


def _never_live_keys(keys: Sequence[str]) -> list[str]:
    return sorted(set(keys) & NEVER_LIVE)


def _collect_never_live_at_depth(value: Any, found: set[str]) -> None:
    """Walk nested mappings/sequences collecting never-live keys (US-L5 SAF:
    a technique verdict or PII key smuggled under an allow-listed counter key
    is still a violation — the net holds at any depth, finding 23)."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in NEVER_LIVE:
                found.add(key)
            _collect_never_live_at_depth(item, found)
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for item in value:
            _collect_never_live_at_depth(item, found)


def validate_live_payload(payload: Mapping[str, Any], allowlist: Sequence[str]) -> None:
    """Raise unless every payload key is allow-listed, registered and — at any
    depth — never never-live.

    The never-live check runs first and wins: a technique/PII key is reported
    as the SAF violation it is, even when it is also simply absent from (or
    smuggled into) the stored allowlist. The registry check then rejects keys
    smuggled into the stored allowlist by hand (write AND emission both check,
    finding 25).
    """
    banned_at_depth: set[str] = set()
    _collect_never_live_at_depth(payload, banned_at_depth)
    if banned_at_depth:
        raise LiveSurfaceViolation(
            f"keys that can never go live: {', '.join(sorted(banned_at_depth))}"
        )
    allowed = set(allowlist)
    unknown = sorted(key for key in payload if key not in allowed)
    if unknown:
        raise LiveSurfaceViolation(
            f"live-surface keys outside the stored allow-list: {', '.join(unknown)}"
        )
    unregistered = sorted(key for key in payload if key not in ALLOWED_LIVE_KEYS)
    if unregistered:
        raise LiveSurfaceViolation(
            f"live-surface keys must be registered live keys: {', '.join(unregistered)}"
        )


def assert_storable_allowlist(allowlist: Sequence[Any]) -> None:
    """Settings-write guard: entries must be strings, never never-live, and
    drawn from the closed :data:`ALLOWED_LIVE_KEYS` registry."""
    non_strings = [repr(entry) for entry in allowlist if not isinstance(entry, str)]
    if non_strings:
        raise LiveSurfaceViolation(
            f"live_mode.allowlist entries must be strings: {', '.join(non_strings)}"
        )
    banned = _never_live_keys(list(allowlist))
    if banned:
        raise LiveSurfaceViolation(
            f"never-live keys may not be stored in the live allow-list: {', '.join(banned)}"
        )
    unregistered = sorted(set(allowlist) - ALLOWED_LIVE_KEYS)
    if unregistered:
        raise LiveSurfaceViolation(
            f"live_mode.allowlist entries must be registered live keys: {', '.join(unregistered)}"
        )
