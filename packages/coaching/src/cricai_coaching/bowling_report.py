"""US-I7: the leg-spin session report body — Report-body-v1 plus a bowling section.

Reuses :mod:`cricai_coaching.report` READ-ONLY: the base body comes from
:func:`~cricai_coaching.report.assemble_report_body`, so every US-G3 AC holds
unchanged (one correction / one drill / one goal, honesty path, claims ledger,
safety verbatim). A single additive ``bowling`` key then carries the leg-spin
data blocks — the same additive-keys pattern the weekly/monthly bodies use for
``trends``/``milestones`` (Phase-6 plan contract #3):

- ``accuracy_scorecard`` (US-I4): hit counts and shares per declared variation
  and overall. Accuracy is computed ONLY over deliveries with a confident
  target call (``target_hit`` non-null); the excluded remainder is shown as an
  uncounted total, never guessed.
- ``release_scatter`` (US-I3): per-ball release heights plus mean/1-sigma
  stats overall and per declared variation — the scatter-chart data. Height
  only for now, stated plainly.
- ``variation_agreement`` (US-I6): the intent-vs-detected matrix. Declared
  intent is ground truth; detected labels are the model's honest guess and
  below-gate deliveries stay unclear — the block never asserts a variation the
  classifier could not confidently call (variation honesty, plan contract #5).
- ``learning_modules``: static Warne/Saqlain lesson + drill content keyed by
  the session's bowling finding kinds, each linked to the player's own example
  balls and clips (principles referenced, no third-party footage bundled —
  US-I7 AC). Each module's ``approved_by`` carries the HONEST sign-off state:
  :data:`PENDING_COACH_REVIEW` until a coach sign-off is recorded in the
  docs/coaching_metrics.md ledger — the payload never claims an endorsement
  that never happened.
- ``workload``: week-to-date overs vs the US-H1 ceiling, injected by the
  worker and ALWAYS present on a bowling report (US-I7 AC), the honesty path
  included.

Numbers inside the bowling blocks are structured data fields (rendered as
data, exactly like ``fatigue_note``'s numeric components), not report wording,
and each is recomputable from stored rows — the US-G3 claims-coverage gate
applies to wording, which stays digit-free here. Every string is geometric
language only (no rotation-rate claims, US-I5 SAF) and passes the kid-safety
content lint.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from typing import Any

from cricai_data.enums import SessionType

from cricai_coaching.bowling_analysis import (
    ACTION_CHECKPOINT,
    RELEASE_CONSISTENCY,
    TARGET_ACCURACY,
    VARIATION_AGREEMENT,
    comparable_label,
)
from cricai_coaching.report import (
    STRENGTH_DEFAULT_POSITIVE,
    ReportSources,
    assemble_report_body,
    is_strength,
    rank_findings,
)

#: The single additive key the bowling section rides under (contract #3 pattern).
BOWLING_BODY_KEY = "bowling"

#: Honest, digit-free block notes (fixed strings — free prose never originates
#: here, US-J2; all pass the kid-safety and banned-claim lints).
ACCURACY_DENOMINATOR_NOTE = (
    "Accuracy is counted only over deliveries with a confident target call; the rest are "
    "shown as uncounted, never guessed."
)
RELEASE_SCATTER_NOTE = (
    "Scatter uses release height only for now; a tighter cluster means a more repeatable "
    "release slot."
)
VARIATION_HONESTY_NOTE = (
    "Your declared intent is the ground truth. Detected labels are the computer's guess "
    "from ball flight geometry; low-confidence deliveries stay unclear and are never "
    "forced into a class."
)

#: Scorecard/matrix group for deliveries bowled without a declared variation.
UNLABELED_VARIATION = "unlabeled"

#: Matrix column for deliveries the classifier honestly left uncalled (US-I6).
UNCLEAR_DETECTION = "unclear"

#: Numberless "what went well" wording for bowling strength probes (positives
#: carry no claims, so numbers here would fail the claims-coverage gate).
TARGET_STRENGTH_POSITIVE = (
    "Your {variation} found the target zone more often than your bowling overall - keep that going."
)
CHECKPOINT_STRENGTH_POSITIVE = (
    "Your front-leg brace got stronger as the session went on - keep that going."
)

#: Honest sign-off marker: no coach has reviewed the module content yet (the
#: sign-off ledger in docs/coaching_metrics.md is blank). The content was
#: authored in-repo; ``approved_by`` may only ever say ``"coach"`` together
#: with a recorded sign-off in that ledger — a human act, never a code
#: default. Coach endorsement is therefore DISABLED by default: until the
#: sign-off is recorded, every surface must present these modules as
#: suggestions pending review, never as coach-endorsed. (The US-J5 review
#: gate likewise defaults to ``auto_publish``, so nothing in the default
#: pipeline constitutes a sign-off.) The SAF suite pins this honest state.
PENDING_COACH_REVIEW = "pending_coach_review"

#: Static Warne/Saqlain learning modules keyed by bowling finding kind (US-I7).
#: Principle/lesson/drill text referencing the legends' principles, never
#: their footage; ``approved_by`` carries the honest sign-off state (see
#: :data:`PENDING_COACH_REVIEW`). All text is digit-free, kid-safe and
#: geometric-only.
LEARNING_MODULES: dict[str, dict[str, str]] = {
    RELEASE_CONSISTENCY: {
        "legend": "Shane Warne",
        "title": "Same slot, every ball",
        "principle": (
            "Warne's fundamentals start with a repeatable action: the same run-up, the same "
            "braced front leg and the same release slot, so every variation looks identical "
            "until it leaves the hand."
        ),
        "lesson": (
            "Watch your example clips and freeze each one at the moment of release. Compare "
            "where the ball leaves your hand - the closer those points sit, the harder you "
            "are to read and the easier it is to land your leg-break where you want."
        ),
        "drill": (
            "Shadow-bowl in front of a mirror, then bowl a short spell aiming to make every "
            "release feel like a copy of the one before."
        ),
        "approved_by": PENDING_COACH_REVIEW,
    },
    TARGET_ACCURACY: {
        "legend": "Shane Warne",
        "title": "Land it on the coin",
        "principle": (
            "Warne practised landing his leg-break on a coin - target discipline first, magic "
            "second. A leg-spinner earns the right to attack by hitting the same spot over "
            "and over."
        ),
        "lesson": (
            "Your scorecard shows how often each variation found the target zone. Pick the "
            "one that missed most and watch your example clips - look at where the ball "
            "pitched, not at the batter."
        ),
        "drill": (
            "Place a marker on a good length in the off-stump channel and bowl a full block "
            "at it, counting your hits out loud."
        ),
        "approved_by": PENDING_COACH_REVIEW,
    },
    VARIATION_AGREEMENT: {
        "legend": "Saqlain Mushtaq",
        "title": "Disguise without change",
        "principle": (
            "Saqlain's mystery came from bowling every variation with the same action and the "
            "same flight - the difference lived in the hand, never in the run-up."
        ),
        "lesson": (
            "The agreement table compares what you meant to bowl with what the ball actually "
            "did. A mismatch is not a failure - it tells you which variation to groove next."
        ),
        "drill": (
            "Call your variation out loud before each ball of a short spell, then check with "
            "your coach which ones behaved as called."
        ),
        "approved_by": PENDING_COACH_REVIEW,
    },
    ACTION_CHECKPOINT: {
        "legend": "Shane Warne",
        "title": "Strong front side",
        "principle": (
            "A braced front leg and a strong front arm give a leg-spinner something to bowl "
            "against - the energy goes up and over the front side, not falling away from it."
        ),
        "lesson": (
            "Watch your example clips at the moment the front foot lands. A braced front leg "
            "keeps your head level through the crease; when it bends late in a spell, the "
            "ball tends to drift shorter and wider."
        ),
        "drill": (
            "Bowl off a shortened run-up focusing only on landing with a firm front leg and "
            "holding your follow-through."
        ),
        "approved_by": PENDING_COACH_REVIEW,
    },
}

#: How many example balls a learning module links (the player's own clips).
MODULE_EXAMPLE_LIMIT = 3


def _bowling_records(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Only ``mode == "bowling"`` records carry the v1.1 bowling fields."""
    return [record for record in records if record.get("mode") == SessionType.BOWLING.value]


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _variation_group(record: Mapping[str, Any]) -> str:
    variation = record.get("variation_intent")
    return variation if isinstance(variation, str) else UNLABELED_VARIATION


def _accuracy_cell(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    hits = sum(1 for row in rows if row["target_hit"])
    pct = round(100 * hits / len(rows), 1) if rows else None
    return {"hits": hits, "n": len(rows), "pct": pct}


def accuracy_scorecard(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """US-I4 accuracy scorecard: hit shares with the denominator shown.

    ``counted``/``total`` make the exclusion honest: only deliveries with a
    confident target call enter any percentage; the rest are visible, never
    silently folded in.
    """
    bowling = _bowling_records(records)
    scored = [record for record in bowling if isinstance(record.get("target_hit"), bool)]
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for record in scored:
        groups.setdefault(_variation_group(record), []).append(record)
    return {
        "overall": _accuracy_cell(scored),
        "by_variation": [
            {"variation": variation, **_accuracy_cell(groups[variation])}
            for variation in sorted(groups)
        ],
        "counted": len(scored),
        "total": len(bowling),
        "note": ACCURACY_DENOMINATOR_NOTE,
    }


def _scatter_stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    heights = [float(row["release_height_cm"]) for row in rows]
    return {
        "n": len(rows),
        "mean_cm": round(statistics.fmean(heights), 1) if heights else None,
        # A 1-ball sigma of 0.0 would fake perfect consistency; null is honest.
        "sigma_cm": round(statistics.pstdev(heights), 1) if len(heights) >= 2 else None,
    }


def release_scatter(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """US-I3 release-scatter data block: per-ball points plus 1-sigma stats.

    Points carry the declared variation so the dashboard can draw
    per-variation clusters; the honest note says the scatter is height-only
    (lateral release position is not measured yet).
    """
    scored = [
        record for record in _bowling_records(records) if _number(record.get("release_height_cm"))
    ]
    heights = [float(record["release_height_cm"]) for record in scored]
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for record in scored:
        groups.setdefault(_variation_group(record), []).append(record)
    return {
        **_scatter_stats(scored),
        "min_cm": round(min(heights), 1) if heights else None,
        "max_cm": round(max(heights), 1) if heights else None,
        "points": [
            {
                "ball_id": int(record["ball_id"]),
                "release_height_cm": float(record["release_height_cm"]),
                "variation_intent": _variation_group(record),
            }
            for record in scored
        ],
        "by_variation": [
            {"variation": variation, **_scatter_stats(groups[variation])}
            for variation in sorted(groups)
        ],
        "note": RELEASE_SCATTER_NOTE,
    }


def variation_agreement(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """US-I6 intent-vs-detected block: honest matrix, honest denominators.

    The matrix covers every delivery with a declared intent (``unknown``
    included, shown as itself); a null detection shows as ``unclear`` (US-I6:
    never force-classified). ``agreement_pct`` uses the SAME denominator as the
    :func:`~cricai_coaching.bowling_analysis.comparable_label` probe rule —
    both sides declared and confident, ``unknown`` on either side excluded —
    so an unlabeled or unclear ball can never inflate or deflate agreement.
    """
    labeled = [
        record
        for record in _bowling_records(records)
        if isinstance(record.get("variation_intent"), str)
    ]
    compared = [
        record
        for record in labeled
        if comparable_label(record.get("variation_intent"))
        and comparable_label(record.get("variation_detected"))
    ]
    matches = sum(
        1 for record in compared if record["variation_intent"] == record["variation_detected"]
    )
    cells: dict[tuple[str, str], int] = {}
    for record in labeled:
        detected = record.get("variation_detected")
        key = (
            str(record["variation_intent"]),
            detected if isinstance(detected, str) else UNCLEAR_DETECTION,
        )
        cells[key] = cells.get(key, 0) + 1
    return {
        "labeled": len(labeled),
        "compared": len(compared),
        "unclear": len(labeled) - len(compared),
        "agreement_pct": round(100 * matches / len(compared), 1) if compared else None,
        "matrix": [
            {"intent": intent, "detected": detected, "count": cells[(intent, detected)]}
            for intent, detected in sorted(cells)
        ],
        "note": VARIATION_HONESTY_NOTE,
    }


def learning_modules(findings: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Warne/Saqlain modules for the session's bowling finding kinds (US-I7).

    One module per kind, keyed off the highest-ranked finding of that kind and
    linked to the player's OWN example balls and clips from that finding's
    evidence (modules reference principles, never third-party footage). Order
    follows the finding ranking, so the most coachable module leads.
    """
    modules: list[dict[str, Any]] = []
    seen: set[str] = set()
    for finding in rank_findings(findings):
        kind = str(finding.get("kind"))
        if kind in seen or kind not in LEARNING_MODULES:
            continue
        seen.add(kind)
        evidence = {
            str(ball): dict(clips) for ball, clips in dict(finding.get("evidence", {})).items()
        }
        example_ids = [int(ball) for ball in finding.get("ball_ids", ()) if str(ball) in evidence][
            :MODULE_EXAMPLE_LIMIT
        ]
        modules.append(
            {
                "kind": kind,
                **LEARNING_MODULES[kind],
                "finding_id": str(finding["finding_id"]),
                "example_ball_ids": example_ids,
                "examples": {str(ball): evidence[str(ball)] for ball in example_ids},
            }
        )
    return modules


def _bowling_positive(findings: Sequence[Mapping[str, Any]], current: str) -> str:
    """Reword the generic strength line when the top strength is a bowling probe.

    :func:`~cricai_coaching.report._positive` names batting zone strengths but
    falls back to its generic control-flavoured line for anything else; when
    that fallback shipped and the strongest strength is a bowling kind, the
    bowling wording (still numberless — positives carry no claims) is the
    honest replacement. Authored positives and zone strengths pass untouched.
    """
    if current != STRENGTH_DEFAULT_POSITIVE:
        return current
    strengths = sorted(
        (finding for finding in findings if is_strength(finding)),
        key=lambda finding: (-float(finding["effect_size"]), str(finding["finding_id"])),
    )
    if not strengths:
        return current
    top = strengths[0]
    kind = str(top.get("kind"))
    if kind == TARGET_ACCURACY:
        variation = str(top.get("condition", {}).get("variation_intent", "best ball"))
        return TARGET_STRENGTH_POSITIVE.format(variation=variation.replace("_", " "))
    if kind == ACTION_CHECKPOINT:
        return CHECKPOINT_STRENGTH_POSITIVE
    return current


def assemble_bowling_report_body(  # noqa: PLR0913  (public seam, mirrors assemble_report_body)
    *,
    kind: str,
    period_start: Any,
    period_end: Any,
    findings: Sequence[Mapping[str, Any]],
    sources: ReportSources | None = None,
    records: Sequence[Mapping[str, Any]] = (),
    workload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the leg-spin session report body (US-I7).

    The base Report-body-v1 is built by the batting assembler unchanged (all
    US-G3 ACs — one-correction discipline, honesty path, claims — hold by
    construction), then the additive ``bowling`` key attaches the leg-spin
    blocks. The bowling section is attached on EVERY path, the honesty banner
    included, because the workload view must always ship (US-I7 AC: the report
    always shows week-to-date overs vs the ceiling). ``records`` are the
    session's canonical BallRecords; ``workload`` is the worker-computed US-H1
    block (``None`` stays an honest null, never a fabricated zero).
    """
    body = assemble_report_body(
        kind=kind,
        period_start=period_start,
        period_end=period_end,
        findings=findings,
        sources=sources,
    )
    body["positive"] = _bowling_positive(findings, str(body["positive"]))
    body[BOWLING_BODY_KEY] = {
        "accuracy_scorecard": accuracy_scorecard(records),
        "release_scatter": release_scatter(records),
        "variation_agreement": variation_agreement(records),
        "learning_modules": learning_modules(findings),
        "workload": dict(workload) if workload is not None else None,
    }
    return body
