"""Bowling pattern probes over BallRecord v1.1 (US-I7, consuming US-I2/I3/I4/I6).

The leg-spin analogue of :mod:`cricai_coaching.analysis_agent`: pure functions
over canonical BallRecord dicts (contract #1) that emit Finding dicts per the
pinned Phase-5 contract #2, carrying the Phase-6 bowling finding kinds (plan
contract #2): ``release_consistency``, ``target_accuracy``,
``variation_agreement`` and ``action_checkpoint``. Probes only read the v1.1
bowling fields, which exist solely on ``mode == "bowling"`` records — batting
records are filtered out, never misread.

Probes (each deterministic, min-sample gated, honest about direction):

- ``release_consistency`` (US-I3): the 1-sigma scatter of ``release_height_cm``
  across the session; fires when the scatter reaches the coach-set gate (a
  wandering release slot). Smaller is better, so the effect is negative.
- ``target_accuracy`` (US-I4): each declared variation's ``target_hit`` share
  vs the session's overall share — the same contrast discipline as the batting
  zone probe (min-n gate + effect-size floor). Negative deltas are weaknesses;
  positive deltas are strengths (INFO), routed by the report to "what went
  well", never headlined as corrections.
- ``variation_agreement`` (US-I6): the share of compared deliveries where
  ``variation_detected`` matched ``variation_intent``; fires below the honesty
  gate. ``unknown`` intents and unclear (null/``unknown``) detections never
  enter the denominator — a low-confidence delivery is never counted as a
  disagreement (US-I6: "unclear", never force-classified).
- ``action_checkpoint`` (US-I2): front-leg brace trend — braced share over the
  last half of the session's evaluable deliveries vs the first half (disjoint
  halves), the fatigue-contrast idiom applied to an action checkpoint.

Honesty rules: every measurement here is geometric (release position, landing
target, post-bounce behaviour); nothing claims rotation measurements the
cameras cannot make (US-I5 — the banned-claim lint gates every string). Free
prose never originates here (US-J2): all ``text_data`` strings come from this
module's fixed templates, and each number a report may quote rides in
``payload`` (``value``/``threshold``/``target``) so the claims ledger can
recompute it.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cricai_data.enums import BowlingVariation, BraceState, FindingSeverity, SessionType

from cricai_coaching.analysis_agent import (
    AnalysisError,
    finding_id,
    rank_findings,
    validate_finding,
)

#: The agent name stamped on every finding this module emits (contract #2).
AGENT_NAME = "bowling_analysis"

#: The four Phase-6 bowling finding kinds (plan contract #2). Each probe's
#: ``kind`` equals its ``probe`` name, mirroring the batting probes.
RELEASE_CONSISTENCY = "release_consistency"
TARGET_ACCURACY = "target_accuracy"
VARIATION_AGREEMENT = "variation_agreement"
ACTION_CHECKPOINT = "action_checkpoint"
BOWLING_FINDING_KINDS: tuple[str, ...] = (
    RELEASE_CONSISTENCY,
    TARGET_ACCURACY,
    VARIATION_AGREEMENT,
    ACTION_CHECKPOINT,
)

#: Derived metric names the probes report on (free strings under contract #2;
#: distinct from raw BallRecord fields so wording like "your
#: release_height_sigma_cm measured 9.5" states what was actually computed).
RELEASE_METRIC = "release_height_sigma_cm"
TARGET_METRIC = "target_hit_pct"
AGREEMENT_METRIC = "variation_agreement_pct"
CHECKPOINT_METRIC = "braced_pct"

#: Fixed text templates — the ONLY source of probe prose (US-J2 AC). Summaries
#: may carry numbers (they are dashboard data, not report wording); corrections
#: and drills are digit-free so a headlined finding never quotes a number the
#: claims ledger cannot recompute. Geometric language only (US-I5 SAF).
RELEASE_TEXT_TEMPLATE = (
    "release height 1-sigma scatter {sigma_cm} cm across {n} balls (gate {gate_cm} cm)"
)
RELEASE_CORRECTION = (
    "Your release point is wandering between deliveries - groove one slot with the same "
    "stride and the same arm path every ball."
)
RELEASE_DRILL = (
    "Shadow-bowl your action in slow motion, then bowl a short spell trying to make every "
    "release feel like a copy of the one before."
)
TARGET_TEXT_TEMPLATE = (
    "{variation} target accuracy {cell_pct}% vs {overall_pct}% overall ({n} balls)"
)
TARGET_CORRECTION = (
    "This variation is missing the target zone more than the rest of your bowling - slow "
    "down and land it on your spot before adding tricks."
)
TARGET_DRILL = (
    "Bowl a block of only this variation at a marked target zone and count your hits out loud."
)
AGREEMENT_TEXT_TEMPLATE = (
    "declared intent matched the detected variation on {agree_pct}% of {n} compared balls "
    "(gate {gate_pct}%)"
)
AGREEMENT_CORRECTION = (
    "Some deliveries are not behaving the way you call them - pick one variation, call it "
    "before the ball, and check what the ball really did."
)
AGREEMENT_DRILL = (
    "Bowl a declared-variation block: call each ball out loud first, then review the "
    "agreement table with your coach."
)
CHECKPOINT_TEXT_TEMPLATE = (
    "front leg braced on {last_pct}% of the last {segment} balls vs {first_pct}% of the "
    "first {segment}"
)
CHECKPOINT_CORRECTION = (
    "Your front leg starts braced but bends later in the spell - land tall and hold a firm "
    "front side all the way through the crease."
)
CHECKPOINT_DRILL = (
    "Finish practice with a short spell off a shortened run-up, focusing only on a braced "
    "front leg and a full follow-through."
)


@dataclass(frozen=True)
class BowlingAnalysisConfig:
    """Probe discipline knobs for the leg-spin lab (US-I7).

    ``min_sample`` is US-G2's no-conclusions-from-3-balls gate; ``effect_floor``
    is the multiple-comparison effect-size floor on share deltas (0.15 = 15
    percentage points); ``top_k`` caps the merged ranked output;
    ``release_sigma_gate_cm`` is the coach-set 1-sigma release-height scatter a
    repeatable slot should stay under (US-I3 "smaller = better");
    ``agreement_gate`` mirrors the US-I6 honesty gate (below it, intent and
    detection disagree often enough to coach on).
    """

    min_sample: int = 10
    effect_floor: float = 0.15
    top_k: int = 5
    release_sigma_gate_cm: float = 8.0
    agreement_gate: float = 0.8


DEFAULT_BOWLING_ANALYSIS_CONFIG = BowlingAnalysisConfig()


def _check_schema(record: Mapping[str, Any]) -> None:
    """Reject unknown MAJOR schema versions (contract #1); absent = v1 era."""
    version = str(record.get("schema_version", "1.0"))
    major = version.split(".", maxsplit=1)[0]
    if major != "1":
        raise AnalysisError(f"unsupported BallRecord schema major version: {version!r}")


def _ordered_bowling_records(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Validate every record, order by ball_id, keep only bowling-mode records.

    The v1.1 bowling fields (:data:`~cricai_data.ballrecord.BOWLING_FIELDS`)
    exist only on ``mode == "bowling"`` records, so batting/mixed-session
    records are excluded up front rather than probed for fields they never
    carry. Explicit ball_id ordering, never input luck (US-J2 determinism).
    """
    for record in records:
        _check_schema(record)
        if not isinstance(record.get("ball_id"), int):
            raise AnalysisError("BallRecord requires an integer ball_id")
    ordered = sorted(records, key=lambda record: int(record["ball_id"]))
    return [record for record in ordered if record.get("mode") == SessionType.BOWLING.value]


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _pct(rate: float) -> float:
    """Share -> percent, rounded once so wording and claims agree byte-for-byte."""
    return round(rate * 100, 1)


def _fmt(value: float) -> str:
    """Canonical number form in summaries (mirrors ``report.format_number``)."""
    return f"{value:g}"


def _confidence(scored: Sequence[Mapping[str, Any]], field: str) -> float:
    """Mean stored confidence of the driving field; absent entries count 1.0.

    Callers only build findings from non-empty gated samples, so the mean is
    always defined (the min-n gate ran first).
    """
    values = [float(record.get("confidence", {}).get(field, 1.0)) for record in scored]
    return round(sum(values) / len(values), 4)


def _evidence(scored: Sequence[Mapping[str, Any]]) -> dict[int, dict[str, str]]:
    """ball_no -> {camera_id -> clip_id} for every contributing ball with clips."""
    return {
        int(record["ball_id"]): dict(record["clips"])
        for record in scored
        if isinstance(record.get("clips"), Mapping) and record["clips"]
    }


def _severity(effect: float, floor: float) -> str:
    """Negative deltas are weaknesses; positive ones are strengths (INFO)."""
    if effect > 0:
        return FindingSeverity.INFO.value
    if abs(effect) >= 2 * floor:
        return FindingSeverity.MAJOR.value
    return FindingSeverity.MINOR.value


def _text_data(summary: str, effect: float, correction: str, drill: str) -> dict[str, str]:
    """Weaknesses carry coach-ready wording; strengths carry only the summary.

    A positive-effect probe finding is a strength the report routes to "what
    went well" (never a correction), so attaching correction/drill text to it
    would invent an issue (US-G3 SAF).
    """
    if effect < 0:
        return {"summary": summary, "correction": correction, "drill": drill}
    return {"summary": summary}


def _finding(  # noqa: PLR0913  (internal builder: every field is one Finding key)
    *,
    probe: str,
    metric: str,
    condition: dict[str, Any],
    effect: float,
    scored: Sequence[Mapping[str, Any]],
    confidence_field: str,
    text_data: dict[str, str],
    payload: dict[str, Any],
    seed: int,
    floor: float,
) -> dict[str, Any]:
    return {
        "finding_id": finding_id(seed, probe, metric, condition),
        "agent": AGENT_NAME,
        "probe": probe,
        "kind": probe,
        "severity": _severity(effect, floor),
        "metric": metric,
        "condition": condition,
        "n": len(scored),
        "effect_size": round(effect, 4),
        "confidence": _confidence(scored, confidence_field),
        "ball_ids": sorted(int(record["ball_id"]) for record in scored),
        "evidence": _evidence(scored),
        "text_data": text_data,
        "payload": payload,
    }


def _release_consistency_probe(
    records: Sequence[Mapping[str, Any]], config: BowlingAnalysisConfig, seed: int
) -> list[dict[str, Any]]:
    """1-sigma release-height scatter vs the coach-set gate (US-I3).

    Fires only when the slot is wandering (sigma >= gate); the effect is the
    normalized shortfall ``(gate - sigma) / gate`` — negative, growing with the
    scatter — so ranking and severity treat it like any other weakness delta.
    """
    scored = [record for record in records if _number(record.get("release_height_cm"))]
    if len(scored) < config.min_sample:
        return []
    heights = [float(record["release_height_cm"]) for record in scored]
    sigma = statistics.pstdev(heights)
    if sigma < config.release_sigma_gate_cm:
        return []  # a repeatable slot is not a finding
    effect = (config.release_sigma_gate_cm - sigma) / config.release_sigma_gate_cm
    sigma_cm = round(sigma, 1)
    summary = RELEASE_TEXT_TEMPLATE.format(
        sigma_cm=_fmt(sigma_cm), n=len(scored), gate_cm=_fmt(config.release_sigma_gate_cm)
    )
    return [
        _finding(
            probe=RELEASE_CONSISTENCY,
            metric=RELEASE_METRIC,
            condition={"mode": SessionType.BOWLING.value},
            effect=effect,
            scored=scored,
            confidence_field="release_height_cm",
            text_data=_text_data(summary, effect, RELEASE_CORRECTION, RELEASE_DRILL),
            payload={
                "value": sigma_cm,
                "mean_cm": round(statistics.fmean(heights), 1),
                "threshold": config.release_sigma_gate_cm,
            },
            seed=seed,
            floor=config.effect_floor,
        )
    ]


def _target_accuracy_probe(
    records: Sequence[Mapping[str, Any]], config: BowlingAnalysisConfig, seed: int
) -> list[dict[str, Any]]:
    """Per declared-variation ``target_hit`` share vs overall (US-I4 contrast).

    The denominator is only deliveries with a confident target call
    (``target_hit`` non-null) — the US-I4 "denominator shown" honesty rule; the
    per-variation share, the overall share (the goal target) and the hit count
    all ride in ``payload`` for claim recomputation.
    """
    scored_all = [record for record in records if isinstance(record.get("target_hit"), bool)]
    if len(scored_all) < config.min_sample:
        return []
    overall = sum(1 for record in scored_all if record["target_hit"]) / len(scored_all)
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for record in scored_all:
        variation = record.get("variation_intent")
        if isinstance(variation, str):
            groups.setdefault(variation, []).append(record)
    findings: list[dict[str, Any]] = []
    for variation in sorted(groups):  # sorted group order: no dict-iteration luck
        scored = groups[variation]
        if len(scored) < config.min_sample:
            continue  # min-n gate (US-G2)
        hits = sum(1 for record in scored if record["target_hit"])
        share = hits / len(scored)
        effect = share - overall
        if abs(effect) < config.effect_floor:
            continue  # effect-size floor (multiple-comparison discipline)
        summary = TARGET_TEXT_TEMPLATE.format(
            variation=variation,
            cell_pct=_fmt(_pct(share)),
            overall_pct=_fmt(_pct(overall)),
            n=len(scored),
        )
        findings.append(
            _finding(
                probe=TARGET_ACCURACY,
                metric=TARGET_METRIC,
                condition={"mode": SessionType.BOWLING.value, "variation_intent": variation},
                effect=effect,
                scored=scored,
                confidence_field="target_hit",
                text_data=_text_data(summary, effect, TARGET_CORRECTION, TARGET_DRILL),
                payload={"value": _pct(share), "target": _pct(overall), "hits": hits},
                seed=seed,
                floor=config.effect_floor,
            )
        )
    return findings


def comparable_label(value: Any) -> bool:
    """A variation label that may enter the agreement denominator (US-I6).

    ``unknown`` is the honest "unclear" class on either side — an undeclared
    intent or a below-gate detection — and is never scored as a disagreement.
    Shared with the report's agreement block so both denominators agree.
    """
    return isinstance(value, str) and value != BowlingVariation.UNKNOWN.value


def _variation_agreement_probe(
    records: Sequence[Mapping[str, Any]], config: BowlingAnalysisConfig, seed: int
) -> list[dict[str, Any]]:
    """Intent-vs-detected agreement share vs the honesty gate (US-I6).

    Only deliveries with BOTH a declared intent and a confident detection are
    compared; the probe fires when agreement falls below the gate. The gate is
    the pinned honesty threshold itself, not a noise floor, so any below-gate
    agreement is a finding.
    """
    compared = [
        record
        for record in records
        if comparable_label(record.get("variation_intent"))
        and comparable_label(record.get("variation_detected"))
    ]
    if len(compared) < config.min_sample:
        return []
    matches = sum(
        1 for record in compared if record["variation_intent"] == record["variation_detected"]
    )
    agreement = matches / len(compared)
    if agreement >= config.agreement_gate:
        return []
    effect = agreement - config.agreement_gate
    summary = AGREEMENT_TEXT_TEMPLATE.format(
        agree_pct=_fmt(_pct(agreement)), n=len(compared), gate_pct=_fmt(_pct(config.agreement_gate))
    )
    return [
        _finding(
            probe=VARIATION_AGREEMENT,
            metric=AGREEMENT_METRIC,
            condition={"mode": SessionType.BOWLING.value},
            effect=effect,
            scored=compared,
            confidence_field="variation_detected",
            text_data=_text_data(summary, effect, AGREEMENT_CORRECTION, AGREEMENT_DRILL),
            payload={
                "value": _pct(agreement),
                "threshold": _pct(config.agreement_gate),
                "matches": matches,
            },
            seed=seed,
            floor=config.effect_floor,
        )
    ]


def _action_checkpoint_probe(
    records: Sequence[Mapping[str, Any]], config: BowlingAnalysisConfig, seed: int
) -> list[dict[str, Any]]:
    """Front-leg brace trend: last half vs first half of the spell (US-I2).

    Disjoint halves of the deliveries with an evaluable ``brace_state`` (an odd
    middle ball joins neither); each half must clear the min-sample gate. A
    negative effect (brace fading late) is the weakness; a positive one is a
    strength the report words positively.
    """
    scored = [record for record in records if isinstance(record.get("brace_state"), str)]
    half = len(scored) // 2
    if half < config.min_sample:
        return []
    first, last = scored[:half], scored[len(scored) - half :]
    first_rate = sum(1 for r in first if r["brace_state"] == BraceState.BRACED.value) / half
    last_rate = sum(1 for r in last if r["brace_state"] == BraceState.BRACED.value) / half
    effect = last_rate - first_rate
    if abs(effect) < config.effect_floor:
        return []
    summary = CHECKPOINT_TEXT_TEMPLATE.format(
        last_pct=_fmt(_pct(last_rate)), first_pct=_fmt(_pct(first_rate)), segment=half
    )
    return [
        _finding(
            probe=ACTION_CHECKPOINT,
            metric=CHECKPOINT_METRIC,
            condition={
                "mode": SessionType.BOWLING.value,
                "checkpoint": "front_leg_brace",
                "segment": f"last_{half}_vs_first_{half}",
            },
            effect=effect,
            scored=[*first, *last],
            confidence_field="brace_state",
            text_data=_text_data(summary, effect, CHECKPOINT_CORRECTION, CHECKPOINT_DRILL),
            payload={"first_pct": _pct(first_rate), "last_pct": _pct(last_rate)},
            seed=seed,
            floor=config.effect_floor,
        )
    ]


def run_bowling_analysis(
    records: Sequence[Mapping[str, Any]],
    *,
    rule_findings: Sequence[Mapping[str, Any]] = (),
    config: BowlingAnalysisConfig = DEFAULT_BOWLING_ANALYSIS_CONFIG,
    seed: int = 0,
) -> list[dict[str, Any]]:
    """Run the bowling probes and merge with injected rule-runner findings (US-I7).

    Mirrors :func:`cricai_coaching.analysis_agent.run_analysis`: ``rule_findings``
    is the US-G2 runner's output for the same session (the bowling seed rules
    fire through the ordinary DSL), validated against contract #2 before the
    merge — contract-invalid findings raise
    :class:`~cricai_coaching.analysis_agent.AnalysisError`, never silently
    dropped. The merged list is ranked deterministically and cut to
    ``config.top_k``; pure noise below the gates yields no probe findings.
    """
    for finding in rule_findings:
        validate_finding(finding)
    bowling = _ordered_bowling_records(records)
    probe_findings = [
        *_release_consistency_probe(bowling, config, seed),
        *_target_accuracy_probe(bowling, config, seed),
        *_variation_agreement_probe(bowling, config, seed),
        *_action_checkpoint_probe(bowling, config, seed),
    ]
    for finding in probe_findings:
        validate_finding(finding)  # this layer honors the contract it enforces
    return rank_findings([*rule_findings, *probe_findings], top_k=config.top_k)


#: The v1.1 bowling fields the probes read (US-I7 consumes what US-I2/I3/I4/I6
#: produce); a drift test asserts each is published by
#: :data:`cricai_data.ballrecord.BOWLING_FIELDS`.
CONSUMED_BOWLING_FIELDS: tuple[str, ...] = (
    "release_height_cm",
    "target_hit",
    "variation_intent",
    "variation_detected",
    "brace_state",
)
