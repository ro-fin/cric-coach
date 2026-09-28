"""IT (Phase-6 finding 16): the flight-metric merge is lost-update safe on PG.

``_merge_flight_metrics`` is a read-modify-write over one shared
``(session, ball, flight)`` JSON row. Under READ COMMITTED an unlocked merge
that overlaps another writer commits a stale dict over the other writer's
keys with no error — the exact silent clobber this test pins. The fix takes
``SELECT ... FOR UPDATE`` (with ``populate_existing``), so the second writer
blocks until the first commits and then merges onto the committed state:
both writers' keys survive, in either commit order.
"""

from __future__ import annotations

import datetime
import threading
import time
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cricai_data.db import make_engine, make_session_factory
from cricai_data.enums import BowlerSource, MetricPhase, SessionType
from cricai_data.models import BallMetrics, Player
from cricai_data.models import Session as SessionRow
from cricai_worker.bowling_flight import _merge_flight_metrics
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

pytestmark = pytest.mark.integration

DATA_PKG = Path(__file__).resolve().parents[3] / "packages" / "data"


def _payload(value: float, unit: str) -> dict[str, object]:
    return {"value": value, "unit": unit, "confidence": 1.0}


def test_concurrent_flight_merges_never_lose_each_others_keys(
    pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writer A merges the bowling keys and holds its transaction open while
    writer B merges a foreign flight key on the SAME row. B must wait for A's
    row lock and merge onto A's committed state — afterwards BOTH writers'
    keys are present, whichever commit landed last."""
    monkeypatch.setenv("CRICAI_DATABASE_URL", pg_url)
    command.upgrade(Config(str(DATA_PKG / "alembic.ini")), "head")
    factory = make_session_factory(make_engine(pg_url))
    session_id = _seed(factory)

    turn_entry = {"turn_cm": _payload(12.0, "cm")}
    speed_entry = {"speed_kph": _payload(99.0, "kph")}

    db_a = factory()
    _merge_flight_metrics(db_a, session_id, 1, turn_entry)  # A holds the row lock

    b_done = threading.Event()

    def writer_b() -> None:
        with factory() as db_b:
            _merge_flight_metrics(db_b, session_id, 1, speed_entry)
            db_b.commit()
        b_done.set()

    thread = threading.Thread(target=writer_b, daemon=True)
    thread.start()
    time.sleep(0.5)  # give B time to reach (and block on) the locked SELECT
    db_a.commit()  # releases the lock; B re-reads the committed row and merges
    db_a.close()
    assert b_done.wait(timeout=15.0), "writer B never finished (lock leaked?)"
    thread.join(timeout=15.0)

    with factory() as db:
        row = db.execute(
            select(BallMetrics).where(
                BallMetrics.session_id == session_id,
                BallMetrics.ball_no == 1,
                BallMetrics.phase == MetricPhase.FLIGHT,
            )
        ).scalar_one()
        # No silent lost update in either direction (finding 16).
        assert row.metrics["turn_cm"]["value"] == 12.0  # A's bowling key survived B
        assert row.metrics["speed_kph"]["value"] == 99.0  # B's update survived A


def _seed(factory: sessionmaker[OrmSession]) -> uuid.UUID:
    """One bowling session with a committed flight row carrying a foreign key."""
    with factory() as db:
        player = Player(name="Mira", birthdate=datetime.date(2014, 3, 2))
        session = SessionRow(
            player=player,
            session_date=datetime.date(2026, 7, 10),
            session_type=SessionType.BOWLING,
            bowler_source=BowlerSource.HUMAN,
        )
        db.add_all([player, session])
        db.flush()
        db.add(
            BallMetrics(
                session_id=session.id,
                ball_no=1,
                phase=MetricPhase.FLIGHT,
                metrics={"speed_kph": _payload(78.0, "kph")},
            )
        )
        db.commit()
        return session.id
