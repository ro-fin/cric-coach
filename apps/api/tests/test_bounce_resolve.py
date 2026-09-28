"""US-F4 precedence resolver: manual always wins, preserved across reprocessing."""

import datetime
import uuid

import pytest
from cricai_api.services.bounce_resolve import (
    ResolvedBounce,
    resolve_bounce,
    resolve_session_bounces,
)
from cricai_data.enums import BowlerSource, EventSource, Length, Line, SessionType
from cricai_data.models import BallEvent, BounceEstimate, BounceMark, Player, Session
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_testing.apptest import make_sqlite_engine


@pytest.fixture
def db() -> OrmSession:
    return OrmSession(make_sqlite_engine())


def _seed_session(db: OrmSession) -> uuid.UUID:
    player = Player(name="Arjun", birthdate=datetime.date(2014, 11, 20))
    session = Session(
        player=player,
        session_date=datetime.date(2026, 7, 8),
        session_type=SessionType.BATTING,
        bowler_source=BowlerSource.MACHINE,
    )
    db.add_all([player, session])
    db.flush()
    return session.id


def _add_mark(
    db: OrmSession,
    session_id: uuid.UUID,
    ball_no: int,
    *,
    camera_id: str = "C3",
    pitch_x: float = 6.5,
    pitch_y: float = 0.25,
    line: Line | None = Line.OFF,
    length: Length | None = Length.GOOD,
    flagged: bool = False,
) -> None:
    db.add(
        BounceMark(
            session_id=session_id,
            ball_no=ball_no,
            camera_id=camera_id,
            frame_no=1000 + ball_no,
            px_x=512.0,
            px_y=300.0,
            pitch_x=pitch_x,
            pitch_y=pitch_y,
            line=line,
            length=length,
            flagged_for_review=flagged,
        )
    )
    db.flush()


def _add_event(db: OrmSession, session_id: uuid.UUID, ball_no: int) -> None:
    db.add(
        BallEvent(
            session_id=session_id,
            ball_no=ball_no,
            start_ms=ball_no * 10_000,
            release_ms=ball_no * 10_000 + 400,
            end_ms=ball_no * 10_000 + 4_000,
            confidence=0.9,
            source=EventSource.AUTO,
            detector_version="det-1",
            valid=True,
        )
    )
    db.flush()


def _reject_event(db: OrmSession, session_id: uuid.UUID, ball_no: int) -> None:
    event = db.execute(
        select(BallEvent).where(BallEvent.session_id == session_id, BallEvent.ball_no == ball_no)
    ).scalar_one()
    event.valid = False  # what POST /events/{id}/reject does (US-D4)
    db.flush()


def _add_estimate(
    db: OrmSession,
    session_id: uuid.UUID,
    ball_no: int,
    *,
    pitch_x: float = 5.0,
    pitch_y: float = 0.2,
    line: Line | None = Line.OFF,
    length: Length | None = Length.GOOD,
    confidence: float = 0.8,
    with_event: bool = True,
) -> BounceEstimate:
    """Seed an auto estimate; estimates only ever exist for balls whose event
    was valid when the worker ran, so a valid event rides along by default."""
    if with_event:
        _add_event(db, session_id, ball_no)
    row = BounceEstimate(
        session_id=session_id,
        ball_no=ball_no,
        pitch_x=pitch_x,
        pitch_y=pitch_y,
        line=line,
        length=length,
        confidence=confidence,
        tracker_version="trk-1+bounce-est-1",
    )
    db.add(row)
    db.flush()
    return row


def test_empty_session_resolves_to_nothing(db: OrmSession) -> None:
    session_id = _seed_session(db)
    assert resolve_session_bounces(db, session_id) == []
    assert resolve_bounce(db, session_id, 1) is None


def test_auto_only_ball_resolves_to_the_estimate(db: OrmSession) -> None:
    session_id = _seed_session(db)
    _add_estimate(db, session_id, 1, confidence=0.42)
    (bounce,) = resolve_session_bounces(db, session_id)
    assert bounce == ResolvedBounce(
        ball_no=1,
        pitch_x=5.0,
        pitch_y=0.2,
        line=Line.OFF,
        length=Length.GOOD,
        source=EventSource.AUTO,
        confidence=0.42,
        flagged=False,
    )
    assert resolve_bounce(db, session_id, 1) == bounce


def test_manual_only_ball_resolves_to_the_mark_without_confidence(db: OrmSession) -> None:
    session_id = _seed_session(db)
    _add_mark(db, session_id, 1)
    (bounce,) = resolve_session_bounces(db, session_id)
    assert bounce.source is EventSource.MANUAL
    assert bounce.confidence is None  # a click is ground truth, not a model output
    assert (bounce.pitch_x, bounce.pitch_y) == (6.5, 0.25)
    assert resolve_bounce(db, session_id, 1) == bounce


@pytest.mark.safety
def test_manual_mark_always_wins_and_survives_reprocessing(db: OrmSession) -> None:
    """US-F4 AC: manual override wins over auto and is preserved on reprocessing."""
    session_id = _seed_session(db)
    _add_mark(db, session_id, 1, pitch_x=7.0, line=Line.MIDDLE)
    estimate = _add_estimate(db, session_id, 1, pitch_x=5.0, line=Line.OFF)

    (bounce,) = resolve_session_bounces(db, session_id)
    assert bounce.source is EventSource.MANUAL
    assert (bounce.pitch_x, bounce.line) == (7.0, Line.MIDDLE)

    # Reprocessing updates ONLY the auto row (the worker never touches marks):
    # the effective bounce must not move.
    estimate.pitch_x = 4.0
    estimate.confidence = 0.99
    db.flush()
    (after,) = resolve_session_bounces(db, session_id)
    assert after == bounce
    single = resolve_bounce(db, session_id, 1)
    assert single is not None and single.source is EventSource.MANUAL


def test_session_resolution_mixes_sources_ordered_by_ball(db: OrmSession) -> None:
    session_id = _seed_session(db)
    _add_estimate(db, session_id, 3)
    _add_mark(db, session_id, 1)
    _add_estimate(db, session_id, 2)
    _add_mark(db, session_id, 2)  # ball 2 has both: manual wins
    bounces = resolve_session_bounces(db, session_id)
    assert [(b.ball_no, b.source) for b in bounces] == [
        (1, EventSource.MANUAL),
        (2, EventSource.MANUAL),
        (3, EventSource.AUTO),
    ]


def test_other_sessions_do_not_leak(db: OrmSession) -> None:
    session_a = _seed_session(db)
    session_b = _seed_session(db)
    _add_estimate(db, session_a, 1)
    _add_mark(db, session_b, 2)
    assert [b.ball_no for b in resolve_session_bounces(db, session_a)] == [1]
    assert resolve_bounce(db, session_a, 2) is None


def test_representative_mark_is_lowest_camera_and_any_flag_flags(db: OrmSession) -> None:
    session_id = _seed_session(db)
    _add_mark(db, session_id, 1, camera_id="C4", pitch_x=6.9, flagged=True)
    _add_mark(db, session_id, 1, camera_id="C3", pitch_x=6.5)
    (bounce,) = resolve_session_bounces(db, session_id)
    assert bounce.pitch_x == 6.5  # C3 is the representative (US-C6 dedupe rule)
    assert bounce.flagged is True  # ANY flagged camera mark flags the ball


def test_representative_mark_prefers_classified_marks(db: OrmSession) -> None:
    session_id = _seed_session(db)
    _add_mark(db, session_id, 1, camera_id="C3", pitch_x=6.5, line=None, length=None)
    _add_mark(db, session_id, 1, camera_id="C4", pitch_x=6.9)
    (bounce,) = resolve_session_bounces(db, session_id)
    assert bounce.pitch_x == 6.9  # the classified C4 mark can be placed in a cell
    assert (bounce.line, bounce.length) == (Line.OFF, Length.GOOD)


def test_unclassified_manual_mark_still_blocks_the_auto_estimate(db: OrmSession) -> None:
    """Manual precedence holds even when the mark has no derived zone classes."""
    session_id = _seed_session(db)
    _add_mark(db, session_id, 1, line=None, length=None)
    _add_estimate(db, session_id, 1)
    (bounce,) = resolve_session_bounces(db, session_id)
    assert bounce.source is EventSource.MANUAL
    assert bounce.line is None and bounce.length is None


@pytest.mark.safety
def test_rejecting_the_event_immediately_hides_the_auto_estimate(db: OrmSession) -> None:
    """US-D4: rejected events are not balls. The phantom auto row must stop
    being served the moment the event is rejected — not only after an operator
    happens to re-run estimate_bounces (audit bounce_resolve.py:83)."""
    session_id = _seed_session(db)
    _add_estimate(db, session_id, 1)
    (bounce,) = resolve_session_bounces(db, session_id)  # served while valid
    assert bounce.source is EventSource.AUTO
    assert resolve_bounce(db, session_id, 1) == bounce

    _reject_event(db, session_id, 1)
    assert resolve_session_bounces(db, session_id) == []  # immediately gone
    assert resolve_bounce(db, session_id, 1) is None


def test_manual_mark_on_a_rejected_ball_is_still_served(db: OrmSession) -> None:
    """The validity gate applies to auto estimates only: a coach's click is
    ground truth and survives the event rejection untouched."""
    session_id = _seed_session(db)
    _add_estimate(db, session_id, 1)
    _add_mark(db, session_id, 1)
    _reject_event(db, session_id, 1)
    (bounce,) = resolve_session_bounces(db, session_id)
    assert bounce.source is EventSource.MANUAL
    single = resolve_bounce(db, session_id, 1)
    assert single is not None and single.source is EventSource.MANUAL


def test_estimate_without_any_event_row_is_not_served(db: OrmSession) -> None:
    """An estimate whose event row is gone entirely (deleted, re-detected) is
    just as phantom as a rejected one: no valid event, nothing served."""
    session_id = _seed_session(db)
    _add_estimate(db, session_id, 1, with_event=False)
    assert resolve_session_bounces(db, session_id) == []
    assert resolve_bounce(db, session_id, 1) is None
