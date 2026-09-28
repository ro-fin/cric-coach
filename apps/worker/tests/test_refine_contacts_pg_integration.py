"""IT (US-D3): CONTACT REFINEMENT RULE enforcement against real PostgreSQL."""

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

pytestmark = pytest.mark.integration


@pytest.fixture
def pg_ctx(pg_url: str, tmp_path: Path) -> WorkerContext:
    engine = make_engine(pg_url)
    create_all(engine)
    return WorkerContext(
        session_factory=make_session_factory(engine),
        store=FsObjectStore(tmp_path / "store"),
    )


def _seed(pg_ctx: WorkerContext) -> uuid.UUID:
    with pg_ctx.session_factory() as db:
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
        db.flush()
        db.add_all(
            [
                BallEvent(
                    session_id=session.id,
                    ball_no=1,
                    start_ms=0,
                    release_ms=100,
                    contact_ms=1900,
                    end_ms=4000,
                    confidence=0.8,
                    source=EventSource.AUTO,
                    detector_version="test-d1",
                ),
                BallEvent(
                    session_id=session.id,
                    ball_no=2,
                    start_ms=4000,
                    release_ms=4100,
                    contact_ms=5900,
                    end_ms=8000,
                    confidence=1.0,
                    source=EventSource.CORRECTED,
                ),
            ]
        )
        db.commit()
        return session.id


def test_rule_enforced_on_postgres_and_missing_audio_flags(pg_ctx: WorkerContext) -> None:
    session_id = _seed(pg_ctx)
    onsets = [
        Onset(time_ms=2005.0, strength=0.9, kind=OnsetKind.BAT_CRACK),
        Onset(time_ms=6100.0, strength=0.9, kind=OnsetKind.BAT_CRACK),
    ]

    summary = refine_session_contacts(pg_ctx, session_id, onsets)
    assert summary.refined_count == 1
    assert summary.skipped_human == 1
    with pg_ctx.session_factory() as db:
        rows = db.execute(select(BallEvent).order_by(BallEvent.ball_no)).scalars().all()
        assert rows[0].contact_ms == 2005  # AUTO refined
        assert rows[0].detector_version == "test-d1+audio-refine-1.0.0"  # provenance recorded
        assert rows[1].contact_ms == 5900  # human correction untouched
        assert rows[1].detector_version == ""  # human row provenance untouched

    missing = refine_session_contacts(pg_ctx, session_id, None)
    assert missing.audio_available is False
    assert missing.flagged_missing_audio == 1
    with pg_ctx.session_factory() as db:
        rows = db.execute(select(BallEvent).order_by(BallEvent.ball_no)).scalars().all()
        assert [row.contact_ms for row in rows] == [2005, 5900]
