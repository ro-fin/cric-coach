"""US-E4 V1 unification: manual sources -> one flight schema with provenance,
plus the pure decision-quality aggregation (leaves vs chases outside off)."""

import uuid

import pytest
from cricai_coaching.decision import (
    FLIGHT_METRIC_KEYS,
    BallContext,
    DecisionQuality,
    ZoneDecision,
    assemble_flight_metrics,
    decision_quality,
)
from cricai_data.enums import Contact, Footwork, Length, Line, Outcome, Shot
from cricai_data.models import BallTag, BounceMark


def _tag(
    *,
    line: Line = Line.OUTSIDE_OFF,
    length: Length = Length.GOOD,
    shot: Shot = Shot.COVER_DRIVE,
    footwork: Footwork = Footwork.FRONT,
) -> BallTag:
    return BallTag(
        session_id=uuid.uuid4(),
        ball_no=1,
        line=line,
        length=length,
        shot=shot,
        footwork=footwork,
        contact=Contact.MIDDLE,
        outcome=Outcome.CONTROLLED_GROUND_SHOT,
        control=True,
        created_by="coach",
    )


def _mark(*, line: Line | None = Line.OFF, length: Length | None = Length.FULL) -> BounceMark:
    return BounceMark(
        session_id=uuid.uuid4(),
        ball_no=1,
        camera_id="C3",
        frame_no=10,
        px_x=800.0,
        px_y=400.0,
        pitch_x=0.2,
        pitch_y=5.5,
        line=line,
        length=length,
    )


def test_full_context_populates_every_key_with_manual_provenance() -> None:
    metrics = assemble_flight_metrics(_tag(), _mark(), 87.5)
    assert tuple(metrics) == FLIGHT_METRIC_KEYS
    assert metrics["speed_kph"].value == 87.5
    assert metrics["speed_kph"].confidence == 1.0
    assert metrics["bounce_xy"].value == [0.2, 5.5]
    assert metrics["bounce_xy"].unit == "m"
    # Tag classes win over the bounce mark's stored classes.
    assert metrics["line"].value == "outside_off"
    assert metrics["length"].value == "good"
    assert metrics["footwork"].value == "front"
    # Granular shot preserved; 8-way decision class exposed alongside (US-E4).
    assert metrics["shot"].value == "cover_drive"
    assert metrics["decision_class"].value == "drive"
    for value in metrics.values():
        assert value.source == "manual"
        assert value.reason is None


def test_tag_only_ball_has_null_bounce_xy_with_reason() -> None:
    metrics = assemble_flight_metrics(_tag(shot=Shot.LEAVE), None, None)
    assert metrics["line"].value == "outside_off"
    assert metrics["decision_class"].value == "leave"
    assert metrics["bounce_xy"].value is None
    reason = metrics["bounce_xy"].reason
    assert reason is not None and "no bounce mark" in reason
    speed = metrics["speed_kph"]
    assert speed.value is None
    assert speed.reason is not None and "machine speed" in speed.reason


def test_bounce_only_ball_takes_zone_classes_from_the_mark() -> None:
    metrics = assemble_flight_metrics(None, _mark(), 92.0)
    assert metrics["line"].value == "off"
    assert metrics["length"].value == "full"
    assert metrics["line"].source == "manual"
    for name in ("footwork", "shot", "decision_class"):
        assert metrics[name].value is None
        reason = metrics[name].reason
        assert reason is not None and "no tag" in reason


def test_bounce_mark_without_derived_classes_is_null_with_reason() -> None:
    metrics = assemble_flight_metrics(None, _mark(line=None, length=None), None)
    line = metrics["line"]
    assert line.value is None
    assert line.reason is not None and "no derived line class" in line.reason
    assert metrics["bounce_xy"].value == [0.2, 5.5]  # raw xy exists even unclassified


def test_no_context_at_all_is_all_null_with_reasons() -> None:
    metrics = assemble_flight_metrics(None, None, None)
    for value in metrics.values():
        assert value.value is None
        assert value.reason
        assert value.confidence == 0.0


def test_every_payload_satisfies_the_metric_contract() -> None:
    for metrics in (
        assemble_flight_metrics(_tag(), _mark(), 80.0),
        assemble_flight_metrics(None, None, None),
    ):
        for value in metrics.values():
            payload = value.to_payload()
            assert {"value", "unit", "confidence"} <= set(payload)
            if payload["value"] is None:
                assert payload["reason"]


@pytest.mark.parametrize(
    ("shot", "expected"),
    [
        (Shot.COVER_DRIVE, "drive"),
        (Shot.STRAIGHT_DRIVE, "drive"),
        (Shot.ON_DRIVE, "drive"),
        (Shot.HOOK, "pull"),
        (Shot.SWEEP, "sweep"),
        (Shot.LEAVE, "leave"),
    ],
)
def test_decision_class_collapses_granular_shots(shot: Shot, expected: str) -> None:
    metrics = assemble_flight_metrics(_tag(shot=shot), None, None)
    assert metrics["decision_class"].value == expected


# --- decision_quality ---------------------------------------------------------------


def _ball(
    ball_no: int,
    line: Line | None,
    length: Length | None = Length.GOOD,
    shot: Shot | None = Shot.LEAVE,
) -> BallContext:
    return BallContext(ball_no=ball_no, line=line, length=length, shot=shot)


def test_decision_quality_counts_leaves_and_chases_per_zone() -> None:
    quality = decision_quality(
        [
            _ball(1, Line.OUTSIDE_OFF, Length.GOOD, Shot.LEAVE),
            _ball(2, Line.OUTSIDE_OFF, Length.GOOD, Shot.COVER_DRIVE),
            _ball(3, Line.OUTSIDE_OFF, Length.FULL, Shot.HOOK),
            _ball(4, Line.MIDDLE, Length.GOOD, Shot.DEFEND),  # not outside off: ignored
            _ball(5, Line.OUTSIDE_OFF, Length.SHORT, None),  # no shot: unclassified
            _ball(6, None, Length.GOOD, Shot.DRIVE),  # unknown line: ignored
            _ball(7, Line.OUTSIDE_OFF, None, Shot.DRIVE),  # no length: unclassified
        ]
    )
    assert quality == DecisionQuality(
        zones=(
            ZoneDecision(length=Length.FULL, leaves=0, chases=1),
            ZoneDecision(length=Length.GOOD, leaves=1, chases=1),
        ),
        outside_off_balls=5,
        leaves=1,
        chases=2,
        unclassified=2,
    )
    assert quality.zones[1].balls == 2  # the reported per-zone denominator


def test_decision_quality_zones_follow_canonical_length_order() -> None:
    quality = decision_quality(
        [
            _ball(1, Line.OUTSIDE_OFF, Length.SHORT, Shot.CUT),
            _ball(2, Line.OUTSIDE_OFF, Length.YORKER, Shot.DEFEND),
            _ball(3, Line.OUTSIDE_OFF, Length.FULL, Shot.LEAVE),
        ]
    )
    assert [zone.length for zone in quality.zones] == [Length.YORKER, Length.FULL, Length.SHORT]


def test_decision_quality_of_nothing_is_empty() -> None:
    quality = decision_quality([])
    assert quality == DecisionQuality(
        zones=(), outside_off_balls=0, leaves=0, chases=0, unclassified=0
    )
