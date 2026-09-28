"""Model-drift monitoring (US-L4): weekly auto-vs-manual agreement + canary.

The drift protocol (docs/observability.md):

- 10 balls per week stay manually tagged forever. :func:`select_weekly_sample`
  picks them, stratified and deterministically seeded by the week, so the
  human workload is bounded and the sample composition is auditable.
- :func:`field_agreement` compares auto-derived per-ball fields against the
  ``ground_truth_eligible`` manual tags; :func:`drift_alerts` turns per-field
  agreement below :data:`DEFAULT_AGREEMENT_THRESHOLDS` into developer alert
  dicts (plain dicts — the worker maps them onto ``alerts`` rows).
- The canary is a fixed synthetic session (:func:`canary_expectations`,
  ``cricai_data.synthetic`` seed :data:`CANARY_SEED`): the pipeline re-derives
  it and pooled agreement below :data:`CANARY_AGREEMENT_BOUND` fires a
  critical alert — a deliberately mislabeled batch must trip the alarm (MV).

Everything here is pure over plain dicts: DB reads live in
``cricai_worker.drift_monitor``. Field values are compared with ``==`` on
plain values (enum ``.value`` strings, bools) — callers normalize rows.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from cricai_data.synthetic import BlockSpec, generate_session

#: Auto-derivable per-ball fields compared against manual tags (US-B4 schema).
GROUND_TRUTH_FIELDS: tuple[str, ...] = (
    "line",
    "length",
    "shot",
    "footwork",
    "contact",
    "outcome",
    "control",
)

#: Per-field minimum agreement before a developer alert fires. Line/length/shot
#: come from the T3 model-quality gates; the rest default to 0.80.
DEFAULT_AGREEMENT_THRESHOLDS: dict[str, float] = {
    "line": 0.90,
    "length": 0.85,
    "shot": 0.75,
    "footwork": 0.80,
    "contact": 0.80,
    "outcome": 0.80,
    "control": 0.80,
}

#: Fields with fewer compared pairs than this stay silent (no evidence, no alarm);
#: the sample-shortfall alert covers the systemic "protocol broke" case.
MIN_COMPARED_PER_FIELD = 5

#: The forever-manual weekly sample size (US-L4: 10 balls/week, T2 protocol).
WEEKLY_SAMPLE_SIZE = 10

#: Fixed canary session: seed + blocks are frozen forever — changing them
#: invalidates every recorded canary expectation.
CANARY_SEED = 424242
CANARY_BLOCKS: tuple[BlockSpec, ...] = (BlockSpec(n_balls=15), BlockSpec(n_balls=15))

#: Pooled canary agreement below this is a critical pipeline regression.
CANARY_AGREEMENT_BOUND = 0.98

AUDIENCE_DEVELOPER = "developer"


@dataclass(frozen=True)
class FieldAgreement:
    """Agreement of one field over the balls where both sides have a value."""

    field: str
    matched: int
    compared: int

    @property
    def pct(self) -> float | None:
        """Agreement fraction, or ``None`` when nothing was comparable."""
        if self.compared == 0:
            return None
        return self.matched / self.compared


def field_agreement[K](
    manual: Mapping[K, Mapping[str, object]],
    auto: Mapping[K, Mapping[str, object]],
    *,
    fields: Sequence[str] = GROUND_TRUTH_FIELDS,
) -> dict[str, FieldAgreement]:
    """Per-field agreement over balls present on both sides (US-L4).

    A field pair is compared only when both sides carry a non-``None`` value:
    an auto pipeline that cannot produce a field yet is not "disagreeing".
    """
    matched = dict.fromkeys(fields, 0)
    compared = dict.fromkeys(fields, 0)
    for key, truth in manual.items():
        observed = auto.get(key)
        if observed is None:
            continue
        for name in fields:
            truth_value = truth.get(name)
            observed_value = observed.get(name)
            if truth_value is None or observed_value is None:
                continue
            compared[name] += 1
            if truth_value == observed_value:
                matched[name] += 1
    return {
        name: FieldAgreement(field=name, matched=matched[name], compared=compared[name])
        for name in fields
    }


def paired_ball_count[K](
    manual: Mapping[K, Mapping[str, object]], auto: Mapping[K, Mapping[str, object]]
) -> int:
    """Balls present on both sides — the effective weekly sample actually paired."""
    return len(set(manual) & set(auto))


def drift_alerts(
    agreements: Mapping[str, FieldAgreement],
    *,
    thresholds: Mapping[str, float] | None = None,
    min_compared: int = MIN_COMPARED_PER_FIELD,
) -> list[dict[str, Any]]:
    """Developer alert dicts for every field whose agreement is below threshold.

    Fields with fewer than ``min_compared`` pairs are skipped: thin evidence
    must not fire (or silence) the alarm — the shortfall alert reports it.
    """
    alerts: list[dict[str, Any]] = []
    merged = {**DEFAULT_AGREEMENT_THRESHOLDS, **(thresholds or {})}
    for name in sorted(agreements):
        agreement = agreements[name]
        threshold = merged.get(name, 0.80)
        pct = agreement.pct
        if agreement.compared < min_compared or pct is None or pct >= threshold:
            continue
        alerts.append(
            {
                "audience": AUDIENCE_DEVELOPER,
                "code": "drift_agreement",
                "severity": "warning",
                "detail": {
                    "field": name,
                    "agreement_pct": round(pct, 4),
                    "threshold": threshold,
                    "matched": agreement.matched,
                    "compared": agreement.compared,
                },
            }
        )
    return alerts


def sample_shortfall_alert(
    paired_balls: int, *, expected: int = WEEKLY_SAMPLE_SIZE
) -> dict[str, Any] | None:
    """Developer alert when the forever-manual weekly sample fell short.

    The drift monitor is only as honest as its sample: fewer paired balls than
    the protocol's ~10/week means the human workflow broke, which is itself a
    pipeline-health event (US-L4 routing: developer).
    """
    if paired_balls >= expected:
        return None
    return {
        "audience": AUDIENCE_DEVELOPER,
        "code": "drift_sample_short",
        "severity": "warning",
        "detail": {"expected": expected, "paired": paired_balls},
    }


def _ball_fields(ball_dict: Mapping[str, object]) -> dict[str, object]:
    return {name: ball_dict[name] for name in GROUND_TRUTH_FIELDS}


def canary_expectations() -> dict[int, dict[str, object]]:
    """Expected per-ball fields of the frozen synthetic canary session.

    Deterministic forever: same seed, same blocks, byte-identical balls
    (``cricai_data.synthetic`` guarantee) — the canary alarm compares the
    pipeline's re-derivation of this session against these values.
    """
    session = generate_session(CANARY_SEED, CANARY_BLOCKS)
    return {ball.ball_no: _ball_fields(ball.to_dict()) for ball in session.balls}


def canary_alerts(
    observed: Mapping[int, Mapping[str, object]],
    *,
    bound: float = CANARY_AGREEMENT_BOUND,
) -> list[dict[str, Any]]:
    """Critical alerts when the canary session disagrees with its expectations.

    Pooled agreement across all fields below ``bound`` fires ``drift_canary``;
    a pipeline that produced nothing comparable for the canary fires
    ``drift_canary_missing`` (silence is a failure, never a pass).
    """
    expectations = canary_expectations()
    agreements = field_agreement(expectations, observed)
    compared = sum(a.compared for a in agreements.values())
    if compared == 0:
        return [
            {
                "audience": AUDIENCE_DEVELOPER,
                "code": "drift_canary_missing",
                "severity": "critical",
                "detail": {"expected_balls": len(expectations)},
            }
        ]
    matched = sum(a.matched for a in agreements.values())
    pooled = matched / compared
    if pooled >= bound:
        return []
    per_field = {name: agreements[name].pct for name in sorted(agreements)}
    return [
        {
            "audience": AUDIENCE_DEVELOPER,
            "code": "drift_canary",
            "severity": "critical",
            "detail": {
                "agreement_pct": round(pooled, 4),
                "bound": bound,
                "compared": compared,
                "per_field": per_field,
            },
        }
    ]


@dataclass(frozen=True)
class SampleCandidate:
    """One ball eligible for this week's forever-manual sample (T2 protocol)."""

    session_id: str
    ball_no: int
    stratum: str = ""


def select_weekly_sample(
    candidates: Sequence[SampleCandidate],
    week_start: date,
    *,
    size: int = WEEKLY_SAMPLE_SIZE,
) -> tuple[SampleCandidate, ...]:
    """Pick ~``size`` balls for manual tagging, stratified and reproducible.

    Candidates are grouped by ``stratum`` and drawn round-robin across strata
    so no single session/zone dominates the week's ground truth. The RNG is
    seeded by ``week_start`` and candidates are canonically ordered first, so
    the same week and candidate set always selects the same balls regardless
    of DB row order.
    """
    rng = random.Random(f"gt-sample:{week_start.isoformat()}")
    by_stratum: dict[str, list[SampleCandidate]] = {}
    ordered = sorted(candidates, key=lambda c: (c.stratum, c.session_id, c.ball_no))
    for candidate in ordered:
        by_stratum.setdefault(candidate.stratum, []).append(candidate)
    for group in by_stratum.values():
        rng.shuffle(group)
    strata = sorted(by_stratum)
    picked: list[SampleCandidate] = []
    while len(picked) < size and any(by_stratum[name] for name in strata):
        for name in strata:
            if len(picked) >= size:
                break
            if by_stratum[name]:
                picked.append(by_stratum[name].pop())
    return tuple(picked)
