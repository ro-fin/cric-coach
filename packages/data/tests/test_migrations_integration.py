"""IT (US-B1): migrations upgrade/downgrade against real PostgreSQL, schema parity."""

import datetime
import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from cricai_data.db import make_session_factory, session_scope
from cricai_data.enums import BowlerSource, DeliveryIntensity, SessionType
from cricai_data.models import Base, BowlingLedgerEntry, Player
from cricai_data.models import Session as SessionRow
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.integration

PKG_DIR = Path(__file__).resolve().parents[1]


def _alembic_config(url: str) -> Config:
    cfg = Config(str(PKG_DIR / "alembic.ini"))
    os.environ["CRICAI_DATABASE_URL"] = url
    return cfg


def test_upgrade_head_matches_models_and_downgrade_is_clean(pg_url: str) -> None:
    cfg = _alembic_config(pg_url)
    command.upgrade(cfg, "head")

    engine = create_engine(pg_url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
        assert diff == [], f"migration drift vs models: {diff}"

    command.downgrade(cfg, "base")
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
    assert tables == {"alembic_version"}
    engine.dispose()


def _ledger_entry(
    player_id: uuid.UUID, session_id: uuid.UUID, intensity: DeliveryIntensity, source: str
) -> BowlingLedgerEntry:
    return BowlingLedgerEntry(
        player_id=player_id,
        entry_date=datetime.date(2026, 7, 9),
        balls=24,
        intensity=intensity,
        session_id=session_id,
        source=source,
        created_by="backfill_workload",
    )


def test_ledger_auto_backfill_backstop_is_db_enforced(pg_url: str) -> None:
    """US-H1 backstop: the migrated PostgreSQL schema rejects a second
    ``auto_backfill`` ledger row for one (session, intensity) — the invariant
    ``backfill_workload`` guarantees only at the application level, which two
    overlapping runs can race — via ``uq_ledger_auto_backfill_session_intensity``.
    Manual rows (and other intensities) never hit the partial index."""
    command.upgrade(_alembic_config(pg_url), "head")

    engine = create_engine(pg_url)
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        player = Player(name="Arjun", birthdate=datetime.date(2014, 11, 20))
        session = SessionRow(
            player=player,
            session_date=datetime.date(2026, 7, 9),
            session_type=SessionType.BOWLING,
            bowler_source=BowlerSource.MACHINE,
        )
        db.add_all([player, session])
        db.flush()
        player_id, session_id = player.id, session.id
        db.add_all(
            [
                _ledger_entry(
                    player_id, session_id, DeliveryIntensity.PACE_INTENT, "auto_backfill"
                ),
                # Partial: another intensity for the same session coexists...
                _ledger_entry(player_id, session_id, DeliveryIntensity.SPIN, "auto_backfill"),
                # ...and manual rows stay unconstrained, even when duplicated.
                _ledger_entry(player_id, session_id, DeliveryIntensity.PACE_INTENT, "manual"),
                _ledger_entry(player_id, session_id, DeliveryIntensity.PACE_INTENT, "manual"),
            ]
        )

    with (
        pytest.raises(IntegrityError, match="uq_ledger_auto_backfill_session_intensity"),
        session_scope(factory) as db,
    ):
        db.add(_ledger_entry(player_id, session_id, DeliveryIntensity.PACE_INTENT, "auto_backfill"))
    engine.dispose()
