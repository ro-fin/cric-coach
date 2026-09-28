"""IT (US-E1): ball event -> pose track linkage + re-run idempotency on real PostgreSQL."""

import datetime
import json
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cricai_data.db import make_engine, make_session_factory
from cricai_data.enums import BowlerSource, EventSource, SessionType
from cricai_data.models import BallEvent, Player, Session
from cricai_data.models import PoseTrack as PoseTrackRow
from cricai_data.storage import FsObjectStore
from cricai_vision.pose import FakePoseProvider, track_from_payload
from cricai_worker.context import WorkerContext
from cricai_worker.extract_pose import extract_session_pose, pose_key
from sqlalchemy import select

pytestmark = pytest.mark.integration

DATA_PKG = Path(__file__).resolve().parents[3] / "packages" / "data"


def test_extraction_links_events_and_reruns_idempotently_on_postgres(
    pg_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CRICAI_DATABASE_URL", pg_url)
    command.upgrade(Config(str(DATA_PKG / "alembic.ini")), "head")
    ctx = WorkerContext(
        session_factory=make_session_factory(make_engine(pg_url)),
        store=FsObjectStore(tmp_path / "store"),
    )
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=datetime.date(2014, 11, 20))
        session = Session(
            player=player,
            session_date=datetime.date(2026, 7, 7),
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
        )
        db.add_all([player, session])
        db.flush()
        db.add(
            BallEvent(
                session_id=session.id,
                ball_no=1,
                start_ms=10_000,
                release_ms=10_400,
                end_ms=14_000,
                confidence=0.9,
                source=EventSource.AUTO,
                detector_version="det-1",
            )
        )
        db.commit()
        session_id = session.id

    def resolver(session_id: uuid.UUID, ball_no: int, camera_id: str) -> tuple[list[bytes], float]:
        return ([b"frame"] * 6, 120.0)

    first = extract_session_pose(
        ctx, session_id, provider=FakePoseProvider(), frames_resolver=resolver
    )
    second = extract_session_pose(
        ctx, session_id, provider=FakePoseProvider(), frames_resolver=resolver
    )
    assert first.extracted == second.extracted == 2  # C1 + C3
    with ctx.session_factory() as db:
        rows = db.execute(select(PoseTrackRow).order_by(PoseTrackRow.camera_id)).scalars().all()
    assert [(r.ball_no, r.camera_id) for r in rows] == [(1, "C1"), (1, "C3")]
    for row in rows:
        assert row.session_id == session_id
        assert row.landmarks_key == pose_key(session_id, 1, row.camera_id)
        track = track_from_payload(json.loads(ctx.store.get(row.landmarks_key)))
        assert track.frame_count == 6 == row.frame_count
