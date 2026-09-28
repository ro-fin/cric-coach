"""IT (US-F1): frame sampling + dataset freeze on real PostgreSQL (migrated schema).

Exercises the real ``uq_frame_session_cam_no`` constraint under re-runs and the
dataset service (enum-typed split/label columns) against the Alembic-migrated
schema — the exact tables production writes.
"""

import datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cricai_data.datasets import add_members, create_dataset, freeze_dataset
from cricai_data.db import make_engine, make_session_factory
from cricai_data.enums import (
    AnnotationSource,
    BowlerSource,
    DatasetSplit,
    EventSource,
    LabelClass,
    SessionType,
    VideoStatus,
)
from cricai_data.models import Annotation, BallEvent, Dataset, FrameSample, Player, Video
from cricai_data.models import Session as SessionRow
from cricai_data.storage import FsObjectStore
from cricai_worker.context import WorkerContext
from cricai_worker.sample_frames import sample_session_frames
from sqlalchemy import select

pytestmark = pytest.mark.integration

DATA_PKG = Path(__file__).resolve().parents[3] / "packages" / "data"


def test_sampling_and_freeze_on_postgres(
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
        session = SessionRow(
            player=player,
            session_date=datetime.date(2026, 7, 8),
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
            machine_settings={"speed_kph": 92},
        )
        db.add_all([player, session])
        db.flush()
        db.add(
            BallEvent(
                session_id=session.id,
                ball_no=1,
                start_ms=10_000,
                release_ms=10_400,
                end_ms=13_000,
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
                checksum_sha256="0" * 64,
                size_bytes=1000,
                status=VideoStatus.PROBED,
                probe={"fps": 120.0},
            )
        )
        db.commit()
        session_id = session.id

    def writer(video: Video, ts_ms: int, key: str) -> None:
        ctx.store.put(key, b"jpeg")

    first = sample_session_frames(ctx, session_id, frame_writer=writer)
    second = sample_session_frames(ctx, session_id, frame_writer=writer)
    assert first.sampled == 3
    assert (second.sampled, second.skipped) == (0, 3)  # real uq survives re-runs
    with ctx.session_factory() as db:
        frames = db.scalars(select(FrameSample).where(FrameSample.session_id == session_id)).all()
        assert len(frames) == 3
        assert frames[0].stratum["speed_band"] == "medium"  # jsonb round trip
        db.add(
            Annotation(
                frame_id=frames[0].id,
                label_class=LabelClass.BALL,
                cx=0.5,
                cy=0.4,
                w=0.02,
                h=0.03,
                annotator="mira",
                source=AnnotationSource.MANUAL,
            )
        )
        # Freeze a train-only + disjoint-test-session dataset on real PG enums.
        train_session = db.get(SessionRow, session_id)
        assert train_session is not None
        test_session = SessionRow(
            player_id=train_session.player_id,
            session_date=datetime.date(2026, 7, 9),
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
        )
        db.add(test_session)
        db.flush()
        test_frame = FrameSample(
            session_id=test_session.id,
            ball_no=1,
            camera_id="C1",
            frame_no=7,
            ts_ms=56,
            object_key=f"sessions/{test_session.id}/frames/C1/frame-000007.jpg",
            stratum={"lighting": "unknown"},
            sampler_version="frame-sampler-1",
        )
        db.add(test_frame)
        db.flush()
        dataset = create_dataset(db, version="pg-v1")
        add_members(
            db,
            dataset,
            [(frames[0].id, DatasetSplit.TRAIN), (test_frame.id, DatasetSplit.TEST)],
        )
        digest = freeze_dataset(db, dataset)
        db.commit()
    with ctx.session_factory() as db:
        frozen = db.scalar(select(Dataset).where(Dataset.version == "pg-v1"))
        assert frozen is not None
        assert frozen.frozen is True
        assert frozen.manifest_digest == digest
