"""US-H5: safety supremacy — the verdict no other component can talk around.

:func:`evaluate` turns already-evaluated H1/H4 state into the pinned
SafetyVerdict dict ``{"active", "codes", "text", "sha256"}``:

- Codes come ONLY from :class:`cricai_data.enums.SafetyCode`; unknown code
  strings raise instead of passing through.
- ``text`` is assembled EXCLUSIVELY from the :data:`WARNING_TEXTS` constants
  in this module (mirroring ``docs/safety_workload.md``), joined in the fixed
  :data:`CODE_ORDER`. No input string — session notes, rule ``text_data``,
  player requests, LLM output — can reach the verdict text.
- ``sha256`` is the SHA-256 hex of ``text``; the empty inactive text hashes
  too, so tampering with an inactive verdict is equally detectable.

:func:`validate_artifact` is the publish-time gate (US-H5 AC): it recomputes
the hash, requires the canonical text VERBATIM (byte-equal) in the artifact's
``safety`` block whenever a safety state is active, and rejects any plan or
report that schedules bowling while ``workload_ceiling`` or ``pain_flag`` is
active. Rejection raises :class:`SafetySupremacyError`; callers hard-fail the
publish and raise an alert — there is no override parameter by design.

Cross-story seams (plain data, no imports of other stories' modules):

- ``ledger_summary`` (story h1, ``cricai_coaching.workload``): a mapping whose
  ``"violations"`` item lists SafetyCode strings; every other key is ignored
  and free text is never read.
- ``wellness_state``: :class:`cricai_coaching.wellness.WellnessState` or a
  mapping with a boolean ``"pain_active"``.
- Bowling detection in artifacts (story j2's DrillPlan blocks contract):
  bowling volume is any ``"bowling_balls"`` integer anywhere in the artifact,
  plus the ``"balls"`` count of any block whose ``"intent"``/``"discipline"``
  is ``"bowling"`` (the planner's real block shape); a marked bowling block
  with no declared count still counts as one ball.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from cricai_data.enums import SafetyCode

from cricai_coaching.wellness import WellnessState

#: Fixed assembly order for verdict codes and their warning paragraphs.
CODE_ORDER: tuple[SafetyCode, ...] = (
    SafetyCode.WORKLOAD_CEILING,
    SafetyCode.DAY_PATTERN_VIOLATION,
    SafetyCode.PAIN_FLAG,
)

#: The ONLY source of safety warning wording (mirrors docs/safety_workload.md).
#: These strings are inserted verbatim into reports/plans and hash-verified;
#: they are never LLM-reachable content (US-H5).
WARNING_TEXTS: dict[SafetyCode, str] = {
    SafetyCode.WORKLOAD_CEILING: (
        "SAFETY - WORKLOAD CEILING: The weekly bowling overs ceiling for this age band "
        "has been reached. Bowling is stopped for the rest of this rolling 7-day window, "
        "and no drill plan may schedule more bowling until the window clears."
    ),
    SafetyCode.DAY_PATTERN_VIOLATION: (
        "SAFETY - DAY PATTERN: The bowling-day pattern limit for this rolling 7-day window "
        "has been reached. Rest or batting-only practice is recommended until the pattern "
        "clears."
    ),
    SafetyCode.PAIN_FLAG: (
        "SAFETY - PAIN REPORTED: Pain was reported at check-in, so bowling recommendations "
        "are paused until a parent or coach clears the flag. Tell your coach or parent, and "
        "see a qualified professional if the pain persists. This system does not give "
        "medical advice."
    ),
}

#: Codes that hard-block scheduled bowling in any plan/report (US-H1/H4).
BOWLING_BLOCK_CODES: frozenset[SafetyCode] = frozenset(
    {SafetyCode.WORKLOAD_CEILING, SafetyCode.PAIN_FLAG}
)

#: Verdict keys pinned by the cross-group SafetyVerdict contract.
VERDICT_KEYS: frozenset[str] = frozenset({"active", "codes", "text", "sha256"})

#: Artifact keys whose value "bowling" marks a bowling block (seam with j2).
_BOWLING_MARKER_KEYS: frozenset[str] = frozenset({"intent", "discipline"})


class SafetySupremacyError(Exception):
    """A plan/report failed the publish-time safety validation (US-H5)."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems: tuple[str, ...] = tuple(problems)
        super().__init__("safety supremacy violation: " + "; ".join(self.problems))


def sha256_text(text: str) -> str:
    """SHA-256 hex of the verdict text (the pinned contract's hash)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_text(codes: Sequence[SafetyCode]) -> str:
    """The one true warning text for a set of codes: fixed order, verbatim."""
    return "\n".join(WARNING_TEXTS[code] for code in CODE_ORDER if code in set(codes))


def evaluate(
    ledger_summary: Mapping[str, Any] | None,
    wellness_state: WellnessState | Mapping[str, Any] | None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Produce the pinned SafetyVerdict dict from H1 + H4 state (US-H5).

    Inputs are already-evaluated states (thresholds live in story h1's
    workload math and the US-H4 state machine); ``config`` is accepted for
    the seam and type-checked, reserved for future threshold-aware checks.
    """
    if config is not None and not isinstance(config, Mapping):
        raise TypeError(f"config must be a mapping or None, got {type(config).__name__}")
    codes: set[SafetyCode] = set()
    if ledger_summary is not None:
        for raw in ledger_summary.get("violations", ()):
            codes.add(SafetyCode(raw))  # unknown code strings raise ValueError
    if _pain_active(wellness_state):
        codes.add(SafetyCode.PAIN_FLAG)
    ordered = [code for code in CODE_ORDER if code in codes]
    text = canonical_text(ordered)
    return {
        "active": bool(ordered),
        "codes": [code.value for code in ordered],
        "text": text,
        "sha256": sha256_text(text),
    }


def check_verdict(verdict: Mapping[str, Any]) -> list[str]:
    """Every way a SafetyVerdict dict deviates from the canonical form.

    The text must equal the canonical assembly for its codes — so tampering
    with the wording is rejected even when the attacker recomputed the hash.
    """
    problems: list[str] = []
    keys = set(verdict)
    if keys != VERDICT_KEYS:
        problems.append(f"verdict keys must be exactly {sorted(VERDICT_KEYS)}, got {sorted(keys)}")
        return problems
    codes: list[SafetyCode] = []
    for raw in verdict["codes"]:
        try:
            codes.append(SafetyCode(raw))
        except ValueError:
            problems.append(f"unknown safety code: {raw!r}")
    if problems:
        return problems
    canonical_order = [code for code in CODE_ORDER if code in set(codes)]
    if codes != canonical_order or len(set(codes)) != len(codes):
        problems.append(f"codes must be unique and in canonical order {canonical_order}")
    expected_text = canonical_text(codes)
    if verdict["text"] != expected_text:
        problems.append("verdict text is not the canonical warning text for its codes")
    if verdict["sha256"] != sha256_text(str(verdict["text"])):
        problems.append("verdict sha256 does not match its text")
    if bool(verdict["active"]) != bool(codes):
        problems.append("verdict active flag contradicts its codes")
    return problems


def scheduled_bowling(artifact: Mapping[str, Any]) -> tuple[int, list[str]]:
    """(bowling ball count, malformation problems) found anywhere in an artifact.

    Recursive by design: nesting a bowling block deeper never hides it. A
    non-integer ``bowling_balls`` — or ``balls`` on a bowling-intent block — is
    itself a violation: malformed volume can never pass as zero.
    """
    return _scan_bowling(artifact, "artifact")


def check_artifact(artifact: Mapping[str, Any], verdict: Mapping[str, Any]) -> list[str]:
    """Every publish-blocking problem in (artifact, verdict) — empty means safe."""
    problems = check_verdict(verdict)
    if problems:
        return problems
    bowling_balls, malformed = scheduled_bowling(artifact)
    problems.extend(malformed)
    safety = artifact.get("safety")
    if verdict["active"]:
        if not isinstance(safety, Mapping):
            problems.append("active safety state but artifact has no safety block")
        else:
            problems.extend(
                f"artifact safety block does not match the verdict {key!r} verbatim"
                for key in ("active", "codes", "text", "sha256")
                if safety.get(key) != verdict[key]
            )
        declared = artifact.get("safety_sha256")
        if declared is not None and declared != verdict["sha256"]:
            problems.append("artifact safety_sha256 does not match the verdict text hash")
        blocked = {SafetyCode(code) for code in verdict["codes"]} & BOWLING_BLOCK_CODES
        if blocked and bowling_balls > 0:
            names = ", ".join(sorted(code.value for code in blocked))
            problems.append(
                f"artifact schedules {bowling_balls} bowling balls while {names} active"
            )
    elif safety is not None and (
        not isinstance(safety, Mapping)
        or any(safety.get(key) != verdict[key] for key in ("active", "codes", "text", "sha256"))
    ):
        problems.append("inactive safety state but artifact carries a non-matching safety block")
    return problems


def validate_artifact(artifact: Mapping[str, Any], verdict: Mapping[str, Any]) -> None:
    """Publish-time gate (US-H5): raise on ANY safety problem, no override.

    Callers must treat the raise as a hard publish failure and raise an alert;
    the exception lists every problem so the audit trail is complete.
    """
    problems = check_artifact(artifact, verdict)
    if problems:
        raise SafetySupremacyError(problems)


def _pain_active(wellness_state: WellnessState | Mapping[str, Any] | None) -> bool:
    if wellness_state is None:
        return False
    if isinstance(wellness_state, WellnessState):
        return wellness_state.pain_active
    return bool(wellness_state.get("pain_active", False))


def _count_integer_balls(value: object, here: str, problems: list[str]) -> int:
    """A ball-count leaf: a non-negative int contributes, anything else is a
    malformation problem (volume can never pass as zero)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        problems.append(f"{here} must be a non-negative integer, got {value!r}")
        return 0
    return value


def _scan_bowling(node: object, path: str) -> tuple[int, list[str]]:
    balls = 0
    problems: list[str] = []
    if isinstance(node, Mapping):
        # The planner emits bowling as ``{intent: "bowling", balls: N, ...}``
        # (contract #5); the ``balls`` of such a block is its bowling volume.
        is_bowling_block = any(node.get(key) == "bowling" for key in _BOWLING_MARKER_KEYS)
        counted_volume = False
        for key, value in node.items():
            here = f"{path}.{key}"
            if key == "bowling_balls" or (key == "balls" and is_bowling_block):
                balls += _count_integer_balls(value, here, problems)
                counted_volume = True
            elif key in _BOWLING_MARKER_KEYS and value == "bowling":
                continue  # the marker itself; volume is the block's balls / fallback below
            else:
                sub_balls, sub_problems = _scan_bowling(value, here)
                balls += sub_balls
                problems.extend(sub_problems)
        if is_bowling_block and not counted_volume:
            balls += 1  # a bowling block with no declared count still counts
    elif isinstance(node, list | tuple):
        for index, item in enumerate(node):
            sub_balls, sub_problems = _scan_bowling(item, f"{path}[{index}]")
            balls += sub_balls
            problems.extend(sub_problems)
    return balls, problems
