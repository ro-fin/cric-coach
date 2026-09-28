"""Per-session variation classification job (US-I6): features -> delivery labels.

For every ``delivery_labels`` row of a session (the bowler/coach declared an
intent — the job NEVER creates rows: ``variation_intent`` is human ground truth
a model must not fabricate), the stored geometric features are read from the
ball's ``ball_metrics`` payloads (``turn_cm``/``apex_m``/``dip_flag`` from the
flight trajectory, ``release_height_cm`` from the release metrics — merged
across phases by metric name, the BallRecord assembler's rule), classified
through the injected :class:`~cricai_vision.variation.VariationClassifierProtocol`,
and the prediction written to ``variation_detected``.

This job is THE model-source write seam for delivery labels: the labels API
only ever writes manual intent, and this job only ever writes
``variation_detected`` — it never touches ``variation_intent``, ``labeler`` or
``source`` (contract #5: intent and detection never conflated; manual wins).

Honesty rules:

* A low-confidence delivery is written as ``unknown`` — unclear, never
  force-classified (US-I6 AC).
* A delivery whose features are missing or unusable is skipped loudly and any
  stale prediction on it is cleared back to NULL: the metrics live in the same
  database as the labels, so their absence is authoritative, and a claim whose
  evidence is gone must not survive a re-run.
* Re-running is idempotent: predictions are re-derived model output, so the
  job plainly overwrites its own previous answers.

The summary reports the session's intent-vs-detected agreement and the T3 gate
verdict (>= 80% 3-way agreement) for observability; report surfaces apply the
gate before showing any auto label to a kid.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from cricai_data.db import session_scope
from cricai_data.enums import BowlingVariation
from cricai_data.models import BallMetrics, DeliveryLabel
from cricai_data.models import Session as SessionRow
from cricai_vision.variation import (
    DeliveryFeatures,
    FakeVariationClassifier,
    VariationClassifierProtocol,
    VariationError,
    evaluate_variations,
    meets_agreement_gate,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from cricai_worker.context import WorkerContext


@dataclass(frozen=True)
class VariationClassificationSummary:
    """What one run wrote: per-outcome counts, loud skips, honest agreement."""

    session_id: str
    classified: int
    unclear: int
    cleared: int
    skipped: tuple[tuple[int, str], ...]
    agreement_3way: float | None
    gate_met: bool
    classifier_version: str


def _merged_payloads(rows: list[BallMetrics]) -> dict[str, dict[str, Any]]:
    """All phases' MetricValue payloads keyed by metric name (the BallRecord
    assembler's merge rule: phases are disjoint per key)."""
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        for key, payload in row.metrics.items():
            if isinstance(payload, dict):
                merged[key] = payload
    return merged


def _numeric(payloads: dict[str, dict[str, Any]], key: str) -> float | None:
    """A stored metric's numeric value, or None (absent / null-with-reason)."""
    payload = payloads.get(key)
    if payload is None:
        return None
    value = payload.get("value")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _flag(payloads: dict[str, dict[str, Any]], key: str) -> bool | None:
    """A stored metric's boolean value, or None (absent / null / non-boolean)."""
    payload = payloads.get(key)
    if payload is None:
        return None
    value = payload.get("value")
    return value if isinstance(value, bool) else None


def _ball_features(payloads: dict[str, dict[str, Any]], ball_no: int) -> DeliveryFeatures | str:
    """The delivery's features, or the loud skip reason when unclassifiable."""
    turn_cm = _numeric(payloads, "turn_cm")
    if turn_cm is None:
        return f"ball {ball_no}: no stored turn_cm metric (trajectory not measured)"
    try:
        return DeliveryFeatures(
            turn_cm=turn_cm,
            apex_m=_numeric(payloads, "apex_m"),
            dip_flag=_flag(payloads, "dip_flag"),
            release_height_cm=_numeric(payloads, "release_height_cm"),
        )
    except VariationError as exc:
        return f"ball {ball_no}: unusable stored features ({exc})"


def _metrics_by_ball(db: Session, session_id: uuid.UUID) -> dict[int, list[BallMetrics]]:
    grouped: dict[int, list[BallMetrics]] = {}
    for row in db.scalars(
        select(BallMetrics)
        .where(BallMetrics.session_id == session_id)
        .order_by(BallMetrics.ball_no, BallMetrics.phase)
    ):
        grouped.setdefault(row.ball_no, []).append(row)
    return grouped


def classify_session_variations(
    ctx: WorkerContext,
    session_id: uuid.UUID,
    *,
    classifier: VariationClassifierProtocol | None = None,
) -> VariationClassificationSummary:
    """Classify every labeled delivery of one session (US-I6).

    ``classifier`` defaults to the deterministic
    :class:`~cricai_vision.variation.FakeVariationClassifier` (the honest
    offline default until a trained model clears the T3 gate); production
    injects the promoted model behind the same protocol.
    """
    classifier = classifier if classifier is not None else FakeVariationClassifier()
    classified = 0
    unclear = 0
    cleared = 0
    skipped: list[tuple[int, str]] = []
    pairs: list[tuple[BowlingVariation, BowlingVariation]] = []
    with session_scope(ctx.session_factory) as db:
        if db.get(SessionRow, session_id) is None:
            raise ValueError(f"session not found: {session_id}")
        metrics = _metrics_by_ball(db, session_id)
        labels = db.scalars(
            select(DeliveryLabel)
            .where(DeliveryLabel.session_id == session_id)
            .order_by(DeliveryLabel.ball_no)
        ).all()
        for label in labels:
            payloads = _merged_payloads(metrics.get(label.ball_no, []))
            features = _ball_features(payloads, label.ball_no)
            if isinstance(features, str):
                skipped.append((label.ball_no, features))
                if label.variation_detected is not None:
                    label.variation_detected = None  # evidence gone: the claim goes too
                    cleared += 1
                continue
            prediction = classifier.classify(features)
            label.variation_detected = prediction.variation
            pairs.append((label.variation_intent, prediction.variation))
            if prediction.variation is BowlingVariation.UNKNOWN:
                unclear += 1
            else:
                classified += 1
    evaluation = evaluate_variations(pairs)
    return VariationClassificationSummary(
        session_id=str(session_id),
        classified=classified,
        unclear=unclear,
        cleared=cleared,
        skipped=tuple(skipped),
        agreement_3way=evaluation.agreement_3way,
        gate_met=meets_agreement_gate(evaluation.agreement_3way),
        classifier_version=classifier.version,
    )
