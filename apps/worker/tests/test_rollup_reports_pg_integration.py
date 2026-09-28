"""IT (US-G5/J5, finding [13]): the rollup upsert locks the report row.

On real PostgreSQL (READ COMMITTED) the old check-then-write upsert let a
concurrent coach publish commit BETWEEN the rollup's read and its write — the
publish was silently overwritten with no ``report_regenerated`` audit. The
upsert now reads the row ``FOR UPDATE``: a mid-window publish attempt blocks
until the rollup commits (or the publish lands first and the demotion is
audited) — the silent-overwrite window is closed.
"""

import datetime
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from cricai_data.db import make_engine, make_session_factory
from cricai_data.enums import ReportKind, ReportStatus
from cricai_data.models import MetricBaseline, Player, Report
from cricai_data.storage import FsObjectStore
from cricai_worker.context import WorkerContext
from cricai_worker.rollup_reports import rollup_reports
from sqlalchemy import event, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as OrmSession

pytestmark = pytest.mark.integration

DATA_PKG = Path(__file__).resolve().parents[3] / "packages" / "data"

AS_OF = date(2026, 6, 17)


def _ctx(pg_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WorkerContext:
    monkeypatch.setenv("CRICAI_DATABASE_URL", pg_url)
    command.upgrade(Config(str(DATA_PKG / "alembic.ini")), "head")
    return WorkerContext(
        session_factory=make_session_factory(make_engine(pg_url)),
        store=FsObjectStore(tmp_path / "store"),
    )


def _seed_player_with_trend(ctx: WorkerContext) -> uuid.UUID:
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=datetime.date(2014, 11, 20))
        db.add(player)
        db.flush()
        for day, value in ((3, 0.5), (10, 0.6), (17, 0.7)):
            db.add(
                MetricBaseline(
                    player_id=player.id,
                    metric="control_pct",
                    zone_key="all",
                    window="session",
                    snapshot_date=date(2026, 6, day),
                    value=value,
                    n=40,
                    payload={"session_id": None, "session_ids": []},
                )
            )
        db.commit()
        return player.id


def test_rollup_upsert_holds_the_row_lock_across_its_write_window(
    pg_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent publish can no longer slip between the rollup's read and
    its write: with the row read FOR UPDATE, the mid-window UPDATE blocks
    (here: times out) instead of being silently overwritten (finding [13])."""
    ctx = _ctx(pg_url, tmp_path, monkeypatch)
    player_id = _seed_player_with_trend(ctx)
    summary = rollup_reports(ctx, player_id, as_of=AS_OF)
    weekly_id = uuid.UUID(summary.weekly_report_id)

    with ctx.session_factory() as db:  # a coach blocked this week's report
        report = db.get(Report, weekly_id)
        assert report is not None
        report.status = ReportStatus.BLOCKED
        db.commit()

    engine = make_engine(pg_url)
    outcome: dict[str, str] = {}

    def concurrent_publish(session: OrmSession, _flush_context: Any, _instances: Any) -> None:
        """Mid-window coach publish: fires on the flush that writes the weekly row."""
        if outcome or not any(
            isinstance(obj, Report) and obj.id == weekly_id for obj in session.dirty
        ):
            return
        conn = engine.connect()
        try:
            conn.execute(text("SET lock_timeout = '500ms'"))
            conn.execute(
                text("UPDATE reports SET status = 'published' WHERE id = :id"),
                {"id": weekly_id},
            )
            conn.commit()
            outcome["publish"] = "published_mid_window"  # the pre-fix silent race
        except OperationalError:
            conn.rollback()
            outcome["publish"] = "lock_blocked"
        finally:
            conn.close()

    real_factory = ctx.session_factory

    def hooked_factory() -> OrmSession:
        db = real_factory()
        event.listen(db, "before_flush", concurrent_publish)
        return db

    hooked_ctx = WorkerContext(session_factory=hooked_factory, store=ctx.store)  # type: ignore[arg-type]
    rollup_reports(hooked_ctx, player_id, as_of=AS_OF)

    assert outcome.get("publish") == "lock_blocked", (
        f"concurrent publish outcome: {outcome} — the rollup upsert must hold "
        "the row lock across its read->write window"
    )
    with ctx.session_factory() as db:
        stored = db.scalar(
            select(Report).where(Report.id == weekly_id, Report.kind == ReportKind.WEEKLY)
        )
        assert stored is not None
        assert stored.status is ReportStatus.DRAFT  # relanded; no publish was lost
    engine.dispose()
