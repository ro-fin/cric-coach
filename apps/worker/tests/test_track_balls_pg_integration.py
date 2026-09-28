"""IT (US-F3): ball event -> ball track linkage + re-run idempotency on real PostgreSQL."""

import datetime
import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cricai_data.db import make_engine, make_session_factory
from cricai_data.enums import BowlerSource, EventSource, SessionType, VideoStatus
from cricai_data.models import BallEvent, BallTrack, Player, Session, Video
from cricai_data.storage import FsObjectStore
from cricai_vision.track import TRACKER_VERSION
from cricai_worker.context import WorkerContext
from cricai_worker.track_balls import track_key, track_session_balls
from sqlalchemy import select

pytestmark = pytest.mark.integration

DATA_PKG = Path(__file__).resolve().parents[3] / "packages" / "data"


def test_tracking_links_events_and_reruns_idempotently_on_postgres(
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
        db.add(
            Video(
                session_id=session.id,
                camera_id="C1",
                object_key=f"sessions/{session.id}/C1/full.mp4",
                filename="full.mp4",
                checksum_sha256="ab" * 32,
                size_bytes=2048,
                status=VideoStatus.PROBED,
                probe={
                    "fps": 30.0,
                    "width": 1920,
                    "height": 1080,
                    "resolution": "1920x1080",
                    "codec": "h264",
                    "duration_s": 120.0,
                },
            )
        )
        db.commit()
        session_id = session.id

    first = track_session_balls(ctx, session_id)
    second = track_session_balls(ctx, session_id)
    assert first.tracked == second.tracked == 1
    assert first.skipped_cameras == () and first.failed == ()
    with ctx.session_factory() as db:
        row = db.execute(select(BallTrack)).scalars().one()  # upsert: never duplicated
    assert (row.session_id, row.ball_no, row.camera_id) == (session_id, 1, "C1")
    assert row.tracker_version == TRACKER_VERSION
    assert row.points_key == track_key(session_id, 1, "C1")
    payload = json.loads(ctx.store.get(row.points_key))
    assert set(payload) == {"points", "segments", "flags"}
    assert row.coverage == 1.0
    assert row.flags == payload["flags"]
    assert row.segments == payload["segments"]
    assert [s["kind"] for s in payload["segments"]] == ["pre_bounce", "post_bounce"]
    assert payload["flags"]["pitch_mapped"] is False  # no calibration seeded
