"""US-H1: ledger backfill from tagged sessions — idempotency + manual-row safety.

Unit tests on in-memory SQLite (full line+branch coverage of the job): only
BOWLING sessions produce ``auto_backfill`` entries (MIXED contribute via
manual entries only — documented limitation), balls are the conservative max
of valid events and tags, re-runs delete-and-recreate ONLY the job's own auto
rows and never touch manual ones.
"""

import uuid
from datetime import date
from pathlib import Path

import pytest
from cricai_coaching.workload import SOURCE_AUTO_BACKFILL, SOURCE_MANUAL
from cricai_data.db import create_all, make_session_factory, session_scope
from cricai_data.enums import (
    BowlerSource,
    Contact,
    DeliveryIntensity,
    EventSource,
    Footwork,
    Length,
    Line,
    Outcome,
    SessionType,
    Shot,
)
from cricai_data.models import BallEvent, BallTag, BowlingLedgerEntry, Player, Session
from cricai_data.storage import FsObjectStore
from cricai_worker.backfill_workload import JOB_NAME, backfill_workload
from cricai_worker.context import WorkerContext
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

SESSION_DATE = date(2026, 7, 9)


@pytest.fixture
def ctx(tmp_path: Path) -> WorkerContext:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_all(engine)
    return WorkerContext(
        session_factory=make_session_factory(engine),
        store=FsObjectStore(tmp_path / "store"),
    )


def _seed_session(
    ctx: WorkerContext,
    *,
    session_type: SessionType = SessionType.BOWLING,
    machine_settings: dict[str, object] | None = None,
    session_date: date = SESSION_DATE,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create (player, session); returns (player_id, session_id)."""
    with session_scope(ctx.session_factory) as db:
        session = Session(
            player=Player(name="Arjun", birthdate=date(2014, 7, 15)),
            session_date=session_date,
            session_type=session_type,
            bowler_source=BowlerSource.HUMAN,
            machine_settings=machine_settings,
        )
        db.add(session)
        db.flush()
        return session.player_id, session.id


def _seed_events(ctx: WorkerContext, session_id: uuid.UUID, count: int, *, valid: bool) -> None:
    with session_scope(ctx.session_factory) as db:
        start = 0 if valid else 100_000
        for index in range(count):
            db.add(
                BallEvent(
                    session_id=session_id,
                    ball_no=start + index + 1,
                    start_ms=(start + index) * 1000,
                    release_ms=(start + index) * 1000 + 100,
                    contact_ms=None,
                    end_ms=(start + index) * 1000 + 500,
                    confidence=0.9,
                    source=EventSource.AUTO,
                    valid=valid,
                )
            )


def _seed_tags(ctx: WorkerContext, session_id: uuid.UUID, count: int) -> None:
    with session_scope(ctx.session_factory) as db:
        for index in range(count):
            db.add(
                BallTag(
                    session_id=session_id,
                    ball_no=index + 1,
                    line=Line.OFF,
                    length=Length.GOOD,
                    shot=Shot.DEFEND,
                    footwork=Footwork.FRONT,
                    contact=Contact.MIDDLE,
                    outcome=Outcome.CONTROLLED_GROUND_SHOT,
                    control=True,
                    created_by="parent",
                )
            )


def _seed_ledger_entry(
    ctx: WorkerContext,
    player_id: uuid.UUID,
    session_id: uuid.UUID | None,
    *,
    source: str,
    balls: int = 12,
) -> uuid.UUID:
    with session_scope(ctx.session_factory) as db:
        entry = BowlingLedgerEntry(
            player_id=player_id,
            entry_date=SESSION_DATE,
            balls=balls,
            intensity=DeliveryIntensity.PACE_INTENT,
            session_id=session_id,
            source=source,
            created_by="parent" if source == SOURCE_MANUAL else JOB_NAME,
        )
        db.add(entry)
        db.flush()
        return entry.id


def _entries(ctx: WorkerContext, session_id: uuid.UUID | None = None) -> list[BowlingLedgerEntry]:
    with ctx.session_factory() as db:
        query = select(BowlingLedgerEntry).order_by(BowlingLedgerEntry.created_at)
        if session_id is not None:
            query = query.where(BowlingLedgerEntry.session_id == session_id)
        return list(db.scalars(query))


def test_bowling_session_backfills_from_event_count(ctx: WorkerContext) -> None:
    player_id, session_id = _seed_session(ctx)
    _seed_events(ctx, session_id, 3, valid=True)
    _seed_tags(ctx, session_id, 2)

    result = backfill_workload(ctx)

    assert result.sessions_processed == 1
    assert result.entries_created == 1
    assert result.entries_deleted == 0
    (entry,) = _entries(ctx)
    assert entry.player_id == player_id
    assert entry.session_id == session_id
    assert entry.entry_date == SESSION_DATE
    assert entry.balls == 3  # max(events=3, tags=2)
    assert entry.intensity is DeliveryIntensity.PACE_INTENT  # conservative default
    assert entry.source == SOURCE_AUTO_BACKFILL
    assert entry.created_by == JOB_NAME
    assert entry.note == "auto backfill: events=3, tags=2"


def test_tag_count_wins_when_larger_and_invalid_events_ignored(ctx: WorkerContext) -> None:
    _, session_id = _seed_session(ctx)
    _seed_events(ctx, session_id, 1, valid=True)
    _seed_events(ctx, session_id, 5, valid=False)  # rejected events never count
    _seed_tags(ctx, session_id, 4)

    backfill_workload(ctx)

    (entry,) = _entries(ctx)
    assert entry.balls == 4  # max(valid events=1, tags=4)


@pytest.mark.parametrize(
    ("machine_settings", "expected"),
    [
        ({"bowling_style": "spin"}, DeliveryIntensity.SPIN),
        ({"bowling_style": "Spin"}, DeliveryIntensity.SPIN),  # case-insensitive
        ({"bowling_style": "pace"}, DeliveryIntensity.PACE_INTENT),
        ({}, DeliveryIntensity.PACE_INTENT),
        (None, DeliveryIntensity.PACE_INTENT),
    ],
)
def test_intensity_from_session_context(
    ctx: WorkerContext,
    machine_settings: dict[str, object] | None,
    expected: DeliveryIntensity,
) -> None:
    _, session_id = _seed_session(ctx, machine_settings=machine_settings)
    _seed_events(ctx, session_id, 6, valid=True)

    backfill_workload(ctx)

    (entry,) = _entries(ctx)
    assert entry.intensity is expected


def test_mixed_and_batting_sessions_are_skipped(ctx: WorkerContext) -> None:
    """MIXED sessions contribute ONLY via manual entries (documented limitation)."""
    _, mixed_id = _seed_session(ctx, session_type=SessionType.MIXED)
    _, batting_id = _seed_session(ctx, session_type=SessionType.BATTING)
    _seed_events(ctx, mixed_id, 10, valid=True)
    _seed_events(ctx, batting_id, 10, valid=True)

    result = backfill_workload(ctx)

    assert result.sessions_processed == 0
    assert result.entries_created == 0
    assert _entries(ctx) == []


def test_rerun_is_idempotent_delete_and_recreate(ctx: WorkerContext) -> None:
    _, session_id = _seed_session(ctx)
    _seed_events(ctx, session_id, 6, valid=True)

    first = backfill_workload(ctx)
    assert (first.entries_created, first.entries_deleted) == (1, 0)

    second = backfill_workload(ctx)
    assert (second.entries_created, second.entries_deleted) == (1, 1)
    assert len(_entries(ctx)) == 1  # no duplicates for the session


def test_manual_rows_are_never_touched(ctx: WorkerContext) -> None:
    player_id, session_id = _seed_session(ctx)
    _seed_events(ctx, session_id, 6, valid=True)
    manual_linked = _seed_ledger_entry(ctx, player_id, session_id, source=SOURCE_MANUAL)
    manual_floating = _seed_ledger_entry(ctx, player_id, None, source=SOURCE_MANUAL)

    backfill_workload(ctx)
    backfill_workload(ctx)

    entries = _entries(ctx)
    manual_ids = {entry.id for entry in entries if entry.source == SOURCE_MANUAL}
    assert manual_ids == {manual_linked, manual_floating}
    auto = [entry for entry in entries if entry.source == SOURCE_AUTO_BACKFILL]
    assert len(auto) == 1


def test_zero_ball_session_creates_nothing_and_cleans_stale_rows(ctx: WorkerContext) -> None:
    player_id, session_id = _seed_session(ctx)  # BOWLING, no events, no tags
    _seed_ledger_entry(ctx, player_id, session_id, source=SOURCE_AUTO_BACKFILL, balls=99)

    result = backfill_workload(ctx)

    assert result.sessions_processed == 1
    assert result.entries_created == 0
    assert result.entries_deleted == 1
    assert _entries(ctx) == []


def test_session_retyped_away_from_bowling_loses_stale_auto_rows(ctx: WorkerContext) -> None:
    """A session corrected BOWLING → MIXED must not keep double-counting."""
    player_id, session_id = _seed_session(ctx, session_type=SessionType.MIXED)
    _seed_ledger_entry(ctx, player_id, session_id, source=SOURCE_AUTO_BACKFILL, balls=42)
    manual = _seed_ledger_entry(ctx, player_id, session_id, source=SOURCE_MANUAL)

    result = backfill_workload(ctx)  # all-mode picks it up via its stale auto row

    assert result.sessions_processed == 1
    assert result.entries_created == 0
    assert result.entries_deleted == 1
    (remaining,) = _entries(ctx)
    assert remaining.id == manual  # the manual correction survives


def test_single_session_targeting(ctx: WorkerContext) -> None:
    _, target_id = _seed_session(ctx)
    _, other_id = _seed_session(ctx)
    _seed_events(ctx, target_id, 6, valid=True)
    _seed_events(ctx, other_id, 6, valid=True)

    result = backfill_workload(ctx, session_id=target_id)

    assert result.sessions_processed == 1
    assert len(_entries(ctx, target_id)) == 1
    assert _entries(ctx, other_id) == []


def test_single_session_targeting_non_bowling_only_cleans(ctx: WorkerContext) -> None:
    player_id, session_id = _seed_session(ctx, session_type=SessionType.BATTING)
    _seed_ledger_entry(ctx, player_id, session_id, source=SOURCE_AUTO_BACKFILL)

    result = backfill_workload(ctx, session_id=session_id)

    assert result.sessions_processed == 1
    assert result.entries_created == 0
    assert result.entries_deleted == 1


def test_unknown_session_raises(ctx: WorkerContext) -> None:
    with pytest.raises(ValueError, match="not found"):
        backfill_workload(ctx, session_id=uuid.uuid4())
