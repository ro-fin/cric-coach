"""US-D3 refinement job: CONTACT REFINEMENT RULE enforcement (unit, SQLite)."""

import uuid
from datetime import date
from pathlib import Path

import pytest
from cricai_data.db import create_all, make_engine, make_session_factory
from cricai_data.enums import BowlerSource, EventSource, SessionType
from cricai_data.models import BallEvent, Player, Session
from cricai_data.storage import FsObjectStore
from cricai_vision.audio_onset import Onset, OnsetKind
from cricai_worker.context import WorkerContext
from cricai_worker.refine_contacts import refine_session_contacts
from sqlalchemy import select


@pytest.fixture
def ctx(tmp_path: Path) -> WorkerContext:
    engine = make_engine(f"sqlite:///{tmp_path / 'refine.sqlite'}")
    create_all(engine)
    return WorkerContext(
        session_factory=make_session_factory(engine),
        store=FsObjectStore(tmp_path / "store"),
    )


def seed_session(ctx: WorkerContext) -> uuid.UUID:
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=date(2014, 11, 20))
        db.add(player)
        db.flush()
        session = Session(
            player_id=player.id,
            session_date=date(2026, 7, 7),
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
        )
        db.add(session)
        db.commit()
        return session.id


def add_event(
    ctx: WorkerContext,
    session_id: uuid.UUID,
    ball_no: int,
    *,
    start_ms: int,
    end_ms: int,
    release_ms: int | None = None,
    contact_ms: int | None = None,
    source: EventSource = EventSource.AUTO,
    valid: bool = True,
    detector_version: str = "test-d1",
) -> None:
    with ctx.session_factory() as db:
        db.add(
            BallEvent(
                session_id=session_id,
                ball_no=ball_no,
                start_ms=start_ms,
                release_ms=start_ms + 100 if release_ms is None else release_ms,
                contact_ms=contact_ms,
                end_ms=end_ms,
                confidence=0.8,
                source=source,
                detector_version=detector_version,
                valid=valid,
            )
        )
        db.commit()


def read_event(ctx: WorkerContext, session_id: uuid.UUID, ball_no: int) -> BallEvent:
    with ctx.session_factory() as db:
        return db.execute(
            select(BallEvent).where(
                BallEvent.session_id == session_id, BallEvent.ball_no == ball_no
            )
        ).scalar_one()


def crack(time_ms: float, strength: float = 0.9) -> Onset:
    return Onset(time_ms=time_ms, strength=strength, kind=OnsetKind.BAT_CRACK)


def thud(time_ms: float, strength: float = 0.9) -> Onset:
    return Onset(time_ms=time_ms, strength=strength, kind=OnsetKind.THUD)


def test_refines_only_auto_rows_and_never_touches_human_or_rejected_rows(
    ctx: WorkerContext,
) -> None:
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1, start_ms=0, end_ms=4000, contact_ms=1900)
    add_event(
        ctx, session_id, 2, start_ms=4000, end_ms=8000, contact_ms=5900, source=EventSource.MANUAL
    )
    add_event(
        ctx,
        session_id,
        3,
        start_ms=8000,
        end_ms=12_000,
        contact_ms=9900,
        source=EventSource.CORRECTED,
    )
    add_event(ctx, session_id, 4, start_ms=12_000, end_ms=16_000, contact_ms=13_900, valid=False)
    onsets = [crack(2005.0), crack(6100.0), crack(10_100.0), crack(14_100.0)]

    summary = refine_session_contacts(ctx, session_id, onsets)

    assert read_event(ctx, session_id, 1).contact_ms == 2005  # AUTO row refined
    assert read_event(ctx, session_id, 2).contact_ms == 5900  # MANUAL untouched
    assert read_event(ctx, session_id, 3).contact_ms == 9900  # CORRECTED untouched
    assert read_event(ctx, session_id, 4).contact_ms == 13_900  # rejected untouched
    assert read_event(ctx, session_id, 1).confidence == 0.8  # event confidence unchanged
    assert summary.audio_available is True
    assert summary.skipped_human == 2
    assert summary.refined_count == 1
    assert summary.flagged_low_confidence == 0
    assert summary.flagged_missing_audio == 0
    ball = summary.refined[0]
    assert (ball.ball_no, ball.previous_contact_ms, ball.contact_ms) == (1, 1900, 2005)
    assert ball.shift_ms == 105
    assert ball.kind is OnsetKind.BAT_CRACK
    assert ball.confidence == 0.9


def test_missing_audio_flags_auto_rows_and_touches_nothing(ctx: WorkerContext) -> None:
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1, start_ms=0, end_ms=4000, contact_ms=1900)
    add_event(
        ctx, session_id, 2, start_ms=4000, end_ms=8000, contact_ms=5900, source=EventSource.MANUAL
    )

    summary = refine_session_contacts(ctx, session_id, None)

    assert read_event(ctx, session_id, 1).contact_ms == 1900
    assert read_event(ctx, session_id, 2).contact_ms == 5900
    assert summary.audio_available is False
    assert summary.refined == ()
    assert summary.skipped_human == 1
    assert summary.flagged_missing_audio == 1
    assert summary.flagged_low_confidence == 0


def test_onsets_provider_callable_is_resolved(ctx: WorkerContext) -> None:
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1, start_ms=0, end_ms=4000, contact_ms=1900)

    summary = refine_session_contacts(ctx, session_id, lambda: [crack(2100.0)])
    assert summary.refined_count == 1
    assert read_event(ctx, session_id, 1).contact_ms == 2100

    missing = refine_session_contacts(ctx, session_id, lambda: None)
    assert missing.audio_available is False
    assert missing.flagged_missing_audio == 1
    assert read_event(ctx, session_id, 1).contact_ms == 2100  # untouched by the second run


def test_low_confidence_or_absent_onsets_flag_instead_of_guessing(ctx: WorkerContext) -> None:
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1, start_ms=0, end_ms=4000, contact_ms=1900)
    add_event(ctx, session_id, 2, start_ms=4000, end_ms=8000, contact_ms=5900)
    onsets = [thud(2000.0, strength=0.5)]  # fallback kind: 0.5 * 0.5 = 0.25 < 0.3

    summary = refine_session_contacts(ctx, session_id, onsets)

    assert read_event(ctx, session_id, 1).contact_ms == 1900  # low confidence, untouched
    assert read_event(ctx, session_id, 2).contact_ms == 5900  # no onset in window, untouched
    assert summary.flagged_low_confidence == 2
    assert summary.refined == ()
    assert summary.audio_available is True


def test_sets_contact_when_vision_had_none_and_reruns_idempotently(ctx: WorkerContext) -> None:
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1, start_ms=0, end_ms=4000, contact_ms=None)
    onsets = [crack(2100.4)]

    first = refine_session_contacts(ctx, session_id, onsets)
    assert read_event(ctx, session_id, 1).contact_ms == 2100
    assert first.refined[0].previous_contact_ms is None
    assert first.refined[0].shift_ms is None

    second = refine_session_contacts(ctx, session_id, onsets)
    assert read_event(ctx, session_id, 1).contact_ms == 2100
    assert second.refined[0].shift_ms == 0
    assert second.refined[0].previous_contact_ms == 2100


def test_prefer_parameter_passes_through_to_refinement(ctx: WorkerContext) -> None:
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1, start_ms=0, end_ms=4000, contact_ms=1900)
    onsets = [crack(2050.0), thud(1800.0, strength=0.7)]

    summary = refine_session_contacts(ctx, session_id, onsets, prefer=OnsetKind.THUD)

    assert read_event(ctx, session_id, 1).contact_ms == 1800
    assert summary.refined[0].kind is OnsetKind.THUD
    assert summary.refined[0].confidence == 0.7  # preferred kind: no fallback scaling


def test_pre_release_onset_is_never_written_as_contact(ctx: WorkerContext) -> None:
    # Finding-10 shape: left ball (contact None), loud machine thud BEFORE release.
    session_id = seed_session(ctx)
    add_event(
        ctx, session_id, 1, start_ms=30_000, release_ms=33_000, end_ms=34_000, contact_ms=None
    )

    summary = refine_session_contacts(ctx, session_id, [thud(31_800.0, strength=0.9)])

    row = read_event(ctx, session_id, 1)
    assert row.contact_ms is None  # vision timing kept: no fabricated pre-release contact
    assert row.detector_version == "test-d1"  # nothing applied -> provenance unchanged
    assert summary.refined == ()
    assert summary.flagged_low_confidence == 1


def test_post_release_onset_wins_over_nearer_pre_release_one(ctx: WorkerContext) -> None:
    session_id = seed_session(ctx)
    add_event(
        ctx, session_id, 1, start_ms=30_000, release_ms=33_000, end_ms=34_000, contact_ms=None
    )
    onsets = [crack(32_900.0), crack(33_600.0)]  # 32_900 is pre-release: excluded

    summary = refine_session_contacts(ctx, session_id, onsets)

    row = read_event(ctx, session_id, 1)
    assert row.contact_ms is not None
    assert row.contact_ms == 33_600
    assert row.start_ms <= row.release_ms < row.contact_ms <= row.end_ms  # pinned invariant
    assert summary.refined_count == 1


def test_applied_refinement_composes_detector_version_for_provenance(ctx: WorkerContext) -> None:
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1, start_ms=0, end_ms=4000, contact_ms=1900)
    add_event(
        ctx, session_id, 2, start_ms=4000, end_ms=8000, contact_ms=5900, source=EventSource.MANUAL
    )
    onsets = [crack(2005.0), crack(6100.0)]

    refine_session_contacts(ctx, session_id, onsets)

    assert read_event(ctx, session_id, 1).detector_version == "test-d1+audio-refine-1.0.0"
    assert read_event(ctx, session_id, 2).detector_version == "test-d1"  # human row untouched
    assert read_event(ctx, session_id, 1).source is EventSource.AUTO  # not a human correction

    # Re-running replaces the refiner suffix instead of stacking (stable provenance).
    refine_session_contacts(ctx, session_id, onsets)
    assert read_event(ctx, session_id, 1).detector_version == "test-d1+audio-refine-1.0.0"


def test_refined_row_without_detector_version_records_bare_refiner(ctx: WorkerContext) -> None:
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1, start_ms=0, end_ms=4000, contact_ms=None, detector_version="")

    refine_session_contacts(ctx, session_id, [crack(2100.0)])

    assert read_event(ctx, session_id, 1).detector_version == "audio-refine-1.0.0"
