"""US-I6 variation classification job: model-source seam, intent never touched.

The job reads geometric features from ``ball_metrics`` payloads, writes ONLY
``delivery_labels.variation_detected``, skips loudly (clearing stale claims)
when evidence is missing, and reports the honest session agreement + T3 gate.
"""

import datetime
import uuid
from pathlib import Path
from typing import Any

import pytest
from cricai_data.db import create_all, make_session_factory
from cricai_data.enums import BowlerSource, BowlingVariation, MetricPhase, SessionType
from cricai_data.models import BallMetrics, DeliveryLabel, Player, Session
from cricai_data.storage import FsObjectStore
from cricai_vision.variation import DeliveryFeatures, VariationPrediction
from cricai_worker.classify_variations import (
    VariationClassificationSummary,
    classify_session_variations,
)
from cricai_worker.context import WorkerContext
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

LB = BowlingVariation.LEG_BREAK
TS = BowlingVariation.TOP_SPINNER
GO = BowlingVariation.GOOGLY
UN = BowlingVariation.UNKNOWN


def _context(tmp_path: Path) -> WorkerContext:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_all(engine)
    return WorkerContext(
        session_factory=make_session_factory(engine),
        store=FsObjectStore(tmp_path / "store"),
    )


def _seed_session(ctx: WorkerContext) -> uuid.UUID:
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=datetime.date(2014, 11, 20))
        session = Session(
            player=player,
            session_date=datetime.date(2026, 7, 10),
            session_type=SessionType.BOWLING,
            bowler_source=BowlerSource.HUMAN,
        )
        db.add_all([player, session])
        db.commit()
        return session.id


def _seed_label(
    ctx: WorkerContext,
    session_id: uuid.UUID,
    ball_no: int,
    intent: BowlingVariation,
    *,
    detected: BowlingVariation | None = None,
    source: str = "manual",
) -> None:
    with ctx.session_factory() as db:
        db.add(
            DeliveryLabel(
                session_id=session_id,
                ball_no=ball_no,
                variation_intent=intent,
                variation_detected=detected,
                labeler="coach",
                source=source,
            )
        )
        db.commit()


def _metric(value: Any, **extra: Any) -> dict[str, Any]:
    return {"value": value, "unit": "cm", "confidence": 0.9, **extra}


def _seed_metrics(
    ctx: WorkerContext,
    session_id: uuid.UUID,
    ball_no: int,
    metrics: dict[str, Any],
    *,
    phase: MetricPhase = MetricPhase.FLIGHT,
) -> None:
    with ctx.session_factory() as db:
        db.add(BallMetrics(session_id=session_id, ball_no=ball_no, phase=phase, metrics=metrics))
        db.commit()


def _labels(ctx: WorkerContext, session_id: uuid.UUID) -> dict[int, DeliveryLabel]:
    with ctx.session_factory() as db:
        rows = db.scalars(select(DeliveryLabel).where(DeliveryLabel.session_id == session_id)).all()
        return {row.ball_no: row for row in rows}


def test_missing_session_is_loud(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    with pytest.raises(ValueError, match="session not found"):
        classify_session_variations(ctx, uuid.uuid4())


def test_classifies_labeled_deliveries_and_reports_agreement(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    # Ball 1: big turn away -> leg break, agreeing with the declared intent.
    _seed_label(ctx, session_id, 1, LB)
    _seed_metrics(ctx, session_id, 1, {"turn_cm": _metric(14.0), "apex_m": _metric(2.2, unit="m")})
    # Ball 2: big turn in -> googly, disagreeing with the declared leg break.
    _seed_label(ctx, session_id, 2, LB)
    _seed_metrics(ctx, session_id, 2, {"turn_cm": _metric(-11.0)})
    # Ball 3: straight with dip -> top spinner (release metrics ride along).
    _seed_label(ctx, session_id, 3, TS)
    _seed_metrics(
        ctx, session_id, 3, {"turn_cm": _metric(1.0), "dip_flag": _metric(True, unit="flag")}
    )
    _seed_metrics(
        ctx,
        session_id,
        3,
        {"release_height_cm": _metric(198.0)},
        phase=MetricPhase.PRE_RELEASE,
    )
    summary = classify_session_variations(ctx, session_id)
    labels = _labels(ctx, session_id)
    assert labels[1].variation_detected is LB
    assert labels[2].variation_detected is GO
    assert labels[3].variation_detected is TS
    assert summary == VariationClassificationSummary(
        session_id=str(session_id),
        classified=3,
        unclear=0,
        cleared=0,
        skipped=(),
        agreement_3way=2 / 3,
        gate_met=False,
        classifier_version="fake-variation-1",
    )


def test_intent_labeler_source_are_never_touched(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_label(ctx, session_id, 1, GO, source="manual")
    _seed_metrics(ctx, session_id, 1, {"turn_cm": _metric(14.0)})  # detected LEG_BREAK
    classify_session_variations(ctx, session_id)
    label = _labels(ctx, session_id)[1]
    assert label.variation_intent is GO  # ground truth survives disagreement
    assert label.variation_detected is LB
    assert label.labeler == "coach"
    assert label.source == "manual"


def test_ambiguous_delivery_is_written_unclear_never_forced(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_label(ctx, session_id, 1, LB)
    _seed_metrics(ctx, session_id, 1, {"turn_cm": _metric(5.0)})  # between straight and big
    summary = classify_session_variations(ctx, session_id)
    assert _labels(ctx, session_id)[1].variation_detected is UN
    assert summary.classified == 0
    assert summary.unclear == 1
    assert summary.agreement_3way is None
    assert summary.gate_met is False


def test_missing_turn_metric_skips_and_clears_stale_claim(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_label(ctx, session_id, 1, LB, detected=GO)  # stale model claim, no metrics
    _seed_label(ctx, session_id, 2, LB)  # never classified: nothing to clear
    summary = classify_session_variations(ctx, session_id)
    labels = _labels(ctx, session_id)
    assert labels[1].variation_detected is None
    assert labels[2].variation_detected is None
    assert summary.cleared == 1
    assert summary.skipped == (
        (1, "ball 1: no stored turn_cm metric (trajectory not measured)"),
        (2, "ball 2: no stored turn_cm metric (trajectory not measured)"),
    )


def test_null_with_reason_turn_payload_skips(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_label(ctx, session_id, 1, LB)
    _seed_metrics(
        ctx, session_id, 1, {"turn_cm": {"value": None, "reason": "post-bounce track too short"}}
    )
    summary = classify_session_variations(ctx, session_id)
    assert summary.skipped[0][0] == 1
    assert "no stored turn_cm" in summary.skipped[0][1]


def test_unusable_stored_feature_values_skip_loudly(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_label(ctx, session_id, 1, LB)
    _seed_metrics(
        ctx,
        session_id,
        1,
        {"turn_cm": _metric(12.0), "apex_m": _metric(-2.0, unit="m")},
    )
    summary = classify_session_variations(ctx, session_id)
    assert _labels(ctx, session_id)[1].variation_detected is None
    assert summary.skipped == (
        (1, "ball 1: unusable stored features (apex_m must be > 0, got -2.0)"),
    )


def test_non_numeric_and_non_boolean_payload_values_read_as_absent(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_label(ctx, session_id, 1, TS)
    _seed_metrics(
        ctx,
        session_id,
        1,
        {
            "turn_cm": _metric(1.0),
            "dip_flag": _metric("yes"),  # non-boolean: reads as absent, not truthy
            "apex_m": _metric(True),  # boolean is not a number
            "release_height_cm": {"note": "no value key"},
            "not_a_payload": 3,  # non-dict payloads are ignored by the merge
        },
    )
    summary = classify_session_variations(ctx, session_id)
    # Straight turn without usable dip evidence: unclear, never a guess.
    assert _labels(ctx, session_id)[1].variation_detected is UN
    assert summary.unclear == 1


def test_rerun_is_idempotent_and_overwrites_model_output(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_label(ctx, session_id, 1, LB)
    _seed_metrics(ctx, session_id, 1, {"turn_cm": _metric(14.0)})
    first = classify_session_variations(ctx, session_id)
    second = classify_session_variations(ctx, session_id)
    assert first == second
    assert _labels(ctx, session_id)[1].variation_detected is LB
    # Re-measured metrics flip the answer on the next run: model output is
    # re-derived, never sticky.
    with ctx.session_factory() as db:
        row = db.scalars(select(BallMetrics)).one()
        row.metrics = {"turn_cm": _metric(-14.0)}
        db.commit()
    classify_session_variations(ctx, session_id)
    assert _labels(ctx, session_id)[1].variation_detected is GO


class _AlwaysLegBreak:
    """Injected classifier double proving the protocol seam."""

    version = "coach-eye-1"

    def classify(self, features: DeliveryFeatures) -> VariationPrediction:
        return VariationPrediction(LB, min(1.0, abs(features.turn_cm) / 9.0))


def test_injected_classifier_and_gate_met(tmp_path: Path) -> None:
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    for ball_no in (1, 2, 3, 4, 5):
        _seed_label(ctx, session_id, ball_no, LB)
        _seed_metrics(ctx, session_id, ball_no, {"turn_cm": _metric(9.0)})
    summary = classify_session_variations(ctx, session_id, classifier=_AlwaysLegBreak())
    assert summary.classifier_version == "coach-eye-1"
    assert summary.classified == 5
    assert summary.agreement_3way == 1.0
    assert summary.gate_met is True


def test_session_without_labels_writes_nothing(tmp_path: Path) -> None:
    """The job never fabricates intent rows (variation_intent is human ground
    truth): a session with metrics but no declared labels is a no-op."""
    ctx = _context(tmp_path)
    session_id = _seed_session(ctx)
    _seed_metrics(ctx, session_id, 1, {"turn_cm": _metric(14.0)})
    summary = classify_session_variations(ctx, session_id)
    assert summary.classified == 0
    assert summary.unclear == 0
    assert summary.skipped == ()
    assert _labels(ctx, session_id) == {}
