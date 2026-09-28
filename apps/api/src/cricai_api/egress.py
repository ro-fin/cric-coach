"""US-L3 egress allow-list: the only shapes permitted to leave the LAN (US-G4 foundation).

Any payload bound for a cloud/LLM endpoint must pass through this module first.
Structured ball metrics and aggregates are allowed; identity, free text, media,
and storage locations are not. Enforcement is code + test, never convention.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

#: Structured-metric field names permitted in outbound payloads. Everything else
#: is dropped (benign keys) or rejected (strict fields, see below).
ALLOWED_EGRESS_FIELDS: frozenset[str] = frozenset(
    {
        # Per-ball structured tags (US-B4 schema; identical for future auto output).
        "ball_no",
        "line",
        "length",
        "shot",
        "footwork",
        "contact",
        "outcome",
        "control",
        # Delivery / machine metrics.
        "speed_kph",
        # Named metrics, values, counts and percentages.
        "metric",
        "metrics",
        "value",
        "values",
        "count",
        "counts",
        "pct",
        "n",
        # Session context — never identity.
        "session_date",
        "block_no",
        "intent",
        "intents",
        # Structured containers.
        "balls",
        "blocks",
        "findings",
    }
)

#: Fields that must never appear in an egress payload, at any depth.
#: Phase-5 wellness free text and body-map detail (US-H4) plus drill/ledger
#: free text (US-J3/H1) are health-adjacent PII about a child — LLM payloads
#: carry structured findings only. Phase-6 adds ``body``: a ``coach_notes.body``
#: is untrusted human free text (US-K3) that must never reach a cloud endpoint.
STRICT_FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {
        "player_name",
        "notes",
        "object_key",
        "birthdate",
        "pain_note",
        "soreness",
        "note",
        "setup",
        "body",
    }
)

#: Substrings that mark a key as secret/location-bearing unless explicitly allowed.
_SENSITIVE_MARKERS: tuple[str, ...] = ("token", "path", "key")

#: Numeric aggregate suffixes (e.g. ``control_pct``) allowed when the flag is on.
_NUMERIC_STAT_SUFFIXES: tuple[str, ...] = ("_pct", "_count", "_n")

_BASE64_DATA_URI = re.compile(r"^data:[\w.+-]+/[\w.+-]+;base64,")
_STORAGE_KEY_PREFIX = "sessions/"


class EgressViolation(ValueError):
    """A payload contained fields or media that must never leave the LAN."""

    def __init__(self, message: str, keys: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.keys: list[str] = list(keys)


def _is_strict_violation(key: str) -> bool:
    lowered = key.lower()
    if lowered in STRICT_FORBIDDEN_FIELDS:
        return True
    if lowered in ALLOWED_EGRESS_FIELDS:
        return False
    return any(marker in lowered for marker in _SENSITIVE_MARKERS)


def _collect_strict_violations(value: object) -> set[str]:
    violations: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_strict_violation(str(key)):
                violations.add(str(key))
            violations |= _collect_strict_violations(item)
    elif isinstance(value, list):
        for item in value:
            violations |= _collect_strict_violations(item)
    return violations


def _is_numeric(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _sanitize_value(value: object, *, allow_extra_numeric_stats: bool) -> object:
    if isinstance(value, dict):
        return _keep_allowed(value, allow_extra_numeric_stats=allow_extra_numeric_stats)
    if isinstance(value, list):
        return [
            _sanitize_value(item, allow_extra_numeric_stats=allow_extra_numeric_stats)
            for item in value
        ]
    return value


def _keep_allowed(payload: dict[str, Any], *, allow_extra_numeric_stats: bool) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    for key in sorted(payload):
        lowered = str(key).lower()
        value = payload[key]
        if lowered in ALLOWED_EGRESS_FIELDS:
            kept[key] = _sanitize_value(value, allow_extra_numeric_stats=allow_extra_numeric_stats)
        elif (
            allow_extra_numeric_stats
            and lowered.endswith(_NUMERIC_STAT_SUFFIXES)
            and _is_numeric(value)
        ):
            kept[key] = value
    return kept


def sanitize_egress_payload(
    payload: dict[str, Any], *, allow_extra_numeric_stats: bool = True
) -> dict[str, Any]:
    """Return the allow-listed subset of ``payload`` with deterministic key order.

    Raises :class:`EgressViolation` if any strict field (identity, free text,
    storage locations, secrets) is present at any depth. Benign unknown keys are
    silently dropped; numeric aggregates (``*_pct``/``*_count``/``*_n``) are kept
    when ``allow_extra_numeric_stats`` is on.
    """
    violations = sorted(_collect_strict_violations(payload))
    if violations:
        raise EgressViolation(f"forbidden fields in egress payload: {violations}", violations)
    return _keep_allowed(payload, allow_extra_numeric_stats=allow_extra_numeric_stats)


def _looks_like_media(value: object) -> bool:
    if isinstance(value, bytes | bytearray):
        return True
    return isinstance(value, str) and (
        _BASE64_DATA_URI.match(value) is not None or value.startswith(_STORAGE_KEY_PREFIX)
    )


def _collect_media_paths(value: object, path: str) -> set[str]:
    offenders: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            offenders |= _collect_media_paths(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            offenders |= _collect_media_paths(item, f"{path}[{index}]")
    elif _looks_like_media(value):
        offenders.add(path)
    return offenders


def assert_no_media(payload: dict[str, Any]) -> None:
    """Raise :class:`EgressViolation` if any value looks like media or a storage key.

    Media means raw bytes, a base64 data-URI, or an object-store key (the
    ``sessions/`` layout). LLM calls must carry structured metrics only (US-L3).
    """
    offenders = sorted(_collect_media_paths(payload, "$"))
    if offenders:
        raise EgressViolation(f"media-like values in egress payload: {offenders}", offenders)
