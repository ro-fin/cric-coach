"""Drill Planner agent (US-J3): findings -> tomorrow's practice plan.

Builds DrillPlan blocks per pinned contract #5:
``[{intent, balls, drill_id|null, machine_settings, success_metric,
finding_id|null}]`` — batting blocks use the ``BlockIntent`` vocabulary and
follow the batting_split config (US-H2: one block per configured intent,
totals within the daily range); the fun block is always present and NEVER
converted into drills (kid-first rule, US-H2 AC). An optional bowling block
uses the :data:`BOWLING_INTENT` string and its balls never exceed the
remaining US-H1 allowance, which is injected via :class:`PlanContext` by the
caller (honest default 0 — no allowance information means no bowling).

Safety supremacy (US-H5, SAF): a SafetyVerdict (pinned contract #3, produced
by story h2's ``safety_agent``) whose active codes include ``workload_ceiling``
or ``pain_flag`` is a HARD bowling block — the planner emits zero bowling
balls regardless of the allowance parameter or any adversarial finding/drill
combination. Verdicts are hash-verified (sha256 of the verbatim text) before
they are embedded; a tampered verdict raises, never plans.

Traceability (US-J3 AC): every non-fun block cites the ``finding_id`` that
motivated its drill, or the literal :data:`MAINTENANCE` marker when no finding
mapped. Machine settings are validated against the machine's actual
configurable envelope (:class:`MachineEnvelope`) — a drill with out-of-envelope
settings can never enter a plan. :func:`validate_plan` is the publish-time
check (US-H5 AC): coach-edited plans run it too, and violations reject the
plan rather than store it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from cricai_data.enums import BlockIntent, Length, Line, SafetyCode

#: Block-intent string for bowling practice blocks. Plan blocks are JSON, so
#: batting intents are ``BlockIntent`` values and bowling uses this marker —
#: the shape the H1/H2 validators key on (pinned contract #5 seam).
BOWLING_INTENT = "bowling"

#: Traceability marker for non-fun blocks with no motivating finding (US-J3).
MAINTENANCE = "maintenance"

#: Fixed success-metric templates: checkable metrics, never free prose.
DEFAULT_SUCCESS_METRIC = "control_pct"
FUN_SUCCESS_METRIC = "enjoyment"
BOWLING_SUCCESS_METRIC = "landing_accuracy_pct"

#: Default bowling block size when the allowance permits (5 overs of 6).
DEFAULT_BOWLING_BALLS = 30

#: Safety codes that hard-block bowling (pinned contract #3: the planner
#: treats workload_ceiling/pain_flag as hard blocks; day-pattern warnings
#: are handled upstream by H1 inside the allowance it hands us).
HARD_BLOCK_CODES: frozenset[str] = frozenset(
    {SafetyCode.WORKLOAD_CEILING.value, SafetyCode.PAIN_FLAG.value}
)

#: Deterministic batting-block order (US-J2/J3 determinism): enum order.
BATTING_INTENT_ORDER: tuple[BlockIntent, ...] = tuple(BlockIntent)

_BATTING_INTENT_VALUES: frozenset[str] = frozenset(member.value for member in BlockIntent)


class PlannerError(ValueError):
    """The plan cannot be built honestly: bad config, findings, or verdict."""


@dataclass(frozen=True)
class MachineEnvelope:
    """The bowling machine's actual configurable parameters (US-J3 AC).

    ``machine_settings`` may only use :attr:`allowed_keys`; ``speed_kph`` must
    sit within the speed bounds and ``line``/``length`` must be canonical
    zone-vocabulary values (``cricai_data.enums``).
    """

    speed_kph_min: float = 40.0
    speed_kph_max: float = 130.0
    allowed_keys: frozenset[str] = frozenset({"speed_kph", "line", "length"})
    allowed_lines: frozenset[str] = frozenset(member.value for member in Line)
    allowed_lengths: frozenset[str] = frozenset(member.value for member in Length)


DEFAULT_MACHINE_ENVELOPE = MachineEnvelope()


@dataclass(frozen=True)
class PlanContext:
    """The planner's injected constraint inputs (cross-story seams).

    ``split`` is the batting_split config (US-H2, ``safety_configs`` shape);
    ``bowling_allowance_balls`` is the remaining US-H1 allowance computed by
    story h1's workload module — the honest default 0 plans no bowling;
    ``safety`` is story h2's SafetyVerdict dict (contract #3) or None.
    """

    split: Mapping[str, Any]
    bowling_allowance_balls: int = 0
    safety: Mapping[str, Any] | None = None
    envelope: MachineEnvelope = DEFAULT_MACHINE_ENVELOPE


def validate_machine_settings(
    settings: Mapping[str, Any], envelope: MachineEnvelope = DEFAULT_MACHINE_ENVELOPE
) -> list[str]:
    """Problems with ``settings`` against the machine envelope; [] = valid."""
    problems = [
        f"unknown machine setting {key!r}" for key in sorted(set(settings) - envelope.allowed_keys)
    ]
    speed = settings.get("speed_kph")
    if speed is not None:
        if not isinstance(speed, int | float) or isinstance(speed, bool):
            problems.append(f"speed_kph must be a number, got {speed!r}")
        elif not envelope.speed_kph_min <= float(speed) <= envelope.speed_kph_max:
            problems.append(
                f"speed_kph {speed} outside the machine envelope"
                f" [{envelope.speed_kph_min}, {envelope.speed_kph_max}]"
            )
    if "line" in settings and settings["line"] not in envelope.allowed_lines:
        problems.append(f"line {settings['line']!r} is not a machine line setting")
    if "length" in settings and settings["length"] not in envelope.allowed_lengths:
        problems.append(f"length {settings['length']!r} is not a machine length setting")
    return problems


def verify_safety_verdict(safety: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Hash-verify a SafetyVerdict dict (contract #3) before trusting it.

    Recomputes SHA-256 over the verbatim ``text``; any mismatch means the
    verdict was altered somewhere between the safety agent and us — the
    planner refuses to plan rather than embed a tampered verdict (US-H5).
    """
    if safety is None:
        return None
    try:
        verdict = {
            "active": bool(safety["active"]),
            "codes": [str(code) for code in safety["codes"]],
            "text": str(safety["text"]),
            "sha256": str(safety["sha256"]),
        }
    except KeyError as exc:
        raise PlannerError(f"safety verdict missing key: {exc}") from exc
    if hashlib.sha256(str(verdict["text"]).encode()).hexdigest() != verdict["sha256"]:
        raise PlannerError("safety verdict sha256 does not match its text (tampering?)")
    return verdict


def bowling_hard_blocked(safety: Mapping[str, Any] | None) -> bool:
    """True when an active verdict carries a hard bowling-block code (US-H5)."""
    return (
        safety is not None
        and bool(safety["active"])
        and any(str(code) in HARD_BLOCK_CODES for code in safety["codes"])
    )


def _split_blocks(split: Mapping[str, Any]) -> list[tuple[BlockIntent, int]]:
    """The configured batting blocks in deterministic enum order (US-H2)."""
    configured = split.get("blocks")
    if not isinstance(configured, Mapping) or BlockIntent.FUN.value not in configured:
        raise PlannerError("batting_split config must configure blocks including a fun block")
    blocks = [
        (intent, int(configured[intent.value]))
        for intent in BATTING_INTENT_ORDER
        if intent.value in configured
    ]
    daily_balls = int(split.get("daily_balls", 0))
    total = sum(balls for _, balls in blocks)
    if total > daily_balls:
        raise PlannerError(
            f"batting_split blocks total {total} balls, above the {daily_balls} daily range"
        )
    return blocks


def _drill_dict(drill: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(drill["id"]),
        "target_metric": str(drill["target_metric"]),
        "intent": str(drill["intent"]),
        "enabled": bool(drill.get("enabled", True)),
        "machine_settings": dict(drill.get("machine_settings", {})),
    }


@dataclass
class _Assignment:
    """Mutable mapping state while blocks are being filled (one plan build)."""

    findings: Sequence[Mapping[str, Any]]
    drills: Sequence[dict[str, Any]]
    envelope: MachineEnvelope
    used_findings: set[str] = field(default_factory=set)
    used_drills: set[str] = field(default_factory=set)

    def match_drill(self, finding: Mapping[str, Any], intent: BlockIntent) -> dict[str, Any] | None:
        """First enabled, unused, in-envelope drill matching the finding's metric."""
        for drill in self.drills:
            if (
                drill["enabled"]
                and drill["id"] not in self.used_drills
                and drill["intent"] == intent.value
                and drill["target_metric"] == str(finding["metric"])
                and not validate_machine_settings(drill["machine_settings"], self.envelope)
            ):
                return drill
        return None

    def batting_block(self, intent: BlockIntent, balls: int) -> dict[str, Any]:
        """One batting block: drill-mapped from the top unused finding, else
        maintenance. The fun block never reaches here (US-H2 protection)."""
        for finding in self.findings:
            finding_id = str(finding["finding_id"])
            if finding_id in self.used_findings:
                continue
            drill = self.match_drill(finding, intent)
            if drill is None:
                continue
            self.used_findings.add(finding_id)
            self.used_drills.add(drill["id"])
            return {
                "intent": intent.value,
                "balls": balls,
                "drill_id": drill["id"],
                "machine_settings": drill["machine_settings"],
                "success_metric": drill["target_metric"],
                "finding_id": finding_id,
            }
        return {
            "intent": intent.value,
            "balls": balls,
            "drill_id": None,
            "machine_settings": {},
            "success_metric": DEFAULT_SUCCESS_METRIC,
            "finding_id": MAINTENANCE,
        }


def _fun_block(balls: int) -> dict[str, Any]:
    """The protected fun block (US-H2): present, never converted to drills."""
    return {
        "intent": BlockIntent.FUN.value,
        "balls": balls,
        "drill_id": None,
        "machine_settings": {},
        "success_metric": FUN_SUCCESS_METRIC,
        "finding_id": None,
    }


def _bowling_block(balls: int) -> dict[str, Any]:
    return {
        "intent": BOWLING_INTENT,
        "balls": balls,
        "drill_id": None,
        "machine_settings": {},
        "success_metric": BOWLING_SUCCESS_METRIC,
        "finding_id": MAINTENANCE,
    }


@dataclass(frozen=True)
class DrillPlanResult:
    """A built plan: pinned-contract blocks plus the verified safety verdict."""

    blocks: list[dict[str, Any]]
    finding_ids: list[str]
    safety: dict[str, Any] | None
    safety_sha256: str | None
    total_balls: int


def build_plan(
    findings: Sequence[Mapping[str, Any]],
    drills: Sequence[Mapping[str, Any]],
    context: PlanContext,
    *,
    bowling_request_balls: int | None = None,
) -> DrillPlanResult:
    """Build the next session's plan from ranked findings (US-J3).

    ``findings`` are the analysis agent's ranked Finding dicts (contract #2);
    ``drills`` are library rows as plain dicts (id, target_metric, intent,
    machine_settings, enabled). The built plan always satisfies
    :func:`validate_plan` — the builder runs its own publish-time validator
    and raises :class:`PlannerError` instead of returning a violating plan.
    """
    verdict = verify_safety_verdict(context.safety)
    assignment = _Assignment(
        findings=findings,
        drills=[_drill_dict(drill) for drill in drills],
        envelope=context.envelope,
    )
    blocks: list[dict[str, Any]] = []
    for intent, balls in _split_blocks(context.split):
        if intent is BlockIntent.FUN:
            blocks.append(_fun_block(balls))
        else:
            blocks.append(assignment.batting_block(intent, balls))
    allowance = 0 if bowling_hard_blocked(verdict) else max(0, context.bowling_allowance_balls)
    requested = (
        bowling_request_balls if bowling_request_balls is not None else DEFAULT_BOWLING_BALLS
    )
    bowling_balls = min(allowance, requested)
    if bowling_balls > 0:
        blocks.append(_bowling_block(bowling_balls))
    violations = validate_plan(blocks, context)
    if violations:  # the builder must satisfy its own publish-time validator
        raise PlannerError("; ".join(violations))
    return DrillPlanResult(
        blocks=blocks,
        finding_ids=[str(block["finding_id"]) for block in blocks if block["drill_id"] is not None],
        safety=verdict,
        safety_sha256=verdict["sha256"] if verdict is not None else None,
        total_balls=sum(int(block["balls"]) for block in blocks),
    )


def _block_violations(block: Mapping[str, Any], index: int, envelope: MachineEnvelope) -> list[str]:
    violations = []
    intent = str(block.get("intent"))
    if intent != BOWLING_INTENT and intent not in _BATTING_INTENT_VALUES:
        violations.append(f"block {index}: unknown intent {intent!r}")
    if int(block.get("balls", 0)) <= 0:
        violations.append(f"block {index}: balls must be positive")
    violations.extend(
        f"block {index}: {problem}"
        for problem in validate_machine_settings(block.get("machine_settings", {}), envelope)
    )
    if intent == BlockIntent.FUN.value:
        if block.get("drill_id") is not None or block.get("finding_id") is not None:
            violations.append(f"block {index}: fun block converted into a drill (US-H2 violation)")
    elif block.get("finding_id") is None:
        violations.append(
            f"block {index}: non-fun block cites neither a finding nor {MAINTENANCE!r}"
            " (US-J3 traceability)"
        )
    return violations


def validate_plan(blocks: Sequence[Mapping[str, Any]], context: PlanContext) -> list[str]:
    """US-H5 publish-time constraint check; [] = plan is publishable.

    Used both by :func:`build_plan` on its own output and by the API when a
    coach edits/replaces a plan — an edited plan violating H1/H4 state (extra
    bowling balls, converted fun block, out-of-envelope settings, batting
    totals above the daily range) is rejected, never stored.
    """
    violations: list[str] = []
    for index, block in enumerate(blocks):
        violations.extend(_block_violations(block, index, context.envelope))
    batting_total = sum(
        int(block.get("balls", 0)) for block in blocks if block.get("intent") != BOWLING_INTENT
    )
    daily_balls = int(context.split.get("daily_balls", 0))
    if batting_total > daily_balls:
        violations.append(f"batting total {batting_total} exceeds daily range {daily_balls}")
    if not any(block.get("intent") == BlockIntent.FUN.value for block in blocks):
        violations.append("fun block missing (US-H2: the fun block is protected)")
    bowling_total = sum(
        int(block.get("balls", 0)) for block in blocks if block.get("intent") == BOWLING_INTENT
    )
    allowance = (
        0 if bowling_hard_blocked(context.safety) else max(0, context.bowling_allowance_balls)
    )
    if bowling_total > allowance:
        violations.append(
            f"bowling total {bowling_total} exceeds the remaining H1 allowance {allowance}"
        )
    return violations
