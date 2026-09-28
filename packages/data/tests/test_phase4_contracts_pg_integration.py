"""IT (US-F1/F2): Phase-4 schema contracts on real PostgreSQL (migrated schema).

Two DB-level invariants only the raw database can prove:

* Enum-typed Phase-4 columns store the canonical ``cricai_data.enums`` string
  VALUES on disk, never Python member names. An ORM round-trip is
  self-consistent under either representation, so these tests read the raw
  column text — the vocabulary every raw-SQL consumer, export and
  cross-service reader sees.
* ``uq_model_versions_one_production`` (partial unique index) backstops the
  US-F2 invariant "exactly one production version per model name" against
  concurrent promotions that both pass the router's read-then-write gate.
"""

import datetime
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cricai_data.db import make_engine, make_session_factory, session_scope
from cricai_data.enums import (
    AnnotationSource,
    BowlerSource,
    DatasetSplit,
    LabelClass,
    ModelStage,
    SessionType,
    TrainingStatus,
)
from cricai_data.models import (
    Annotation,
    Dataset,
    DatasetMember,
    FrameSample,
    ModelRun,
    ModelVersion,
    Player,
)
from cricai_data.models import Session as SessionRow
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.integration

PKG_DIR = Path(__file__).resolve().parents[1]


def _migrated_engine(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> Engine:
    monkeypatch.setenv("CRICAI_DATABASE_URL", pg_url)
    command.upgrade(Config(str(PKG_DIR / "alembic.ini")), "head")
    return make_engine(pg_url)


def _model_run(dataset_id: uuid.UUID, status: TrainingStatus) -> ModelRun:
    return ModelRun(
        model_name="ball-detector",
        dataset_id=dataset_id,
        status=status,
        trainer_version="fake-train-1",
    )


def test_phase4_enum_columns_store_canonical_values_on_disk(
    pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The five Phase-4 enums (DatasetSplit/LabelClass/AnnotationSource/
    ModelStage/TrainingStatus) must land as canonical values ('train', not
    'TRAIN') in the Alembic-migrated columns production writes."""
    engine = _migrated_engine(pg_url, monkeypatch)
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        player = Player(name="Arjun", birthdate=datetime.date(2014, 11, 20))
        session = SessionRow(
            player=player,
            session_date=datetime.date(2026, 7, 9),
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
        )
        db.add_all([player, session])
        db.flush()
        frame = FrameSample(
            session_id=session.id,
            ball_no=1,
            camera_id="C1",
            frame_no=42,
            ts_ms=1680,
            object_key=f"sessions/{session.id}/frames/C1/frame-000042.jpg",
            stratum={},
            sampler_version="frame-sampler-1",
        )
        dataset = Dataset(version="pg-enums-v1")
        db.add_all([frame, dataset])
        db.flush()
        db.add(
            Annotation(
                frame_id=frame.id,
                label_class=LabelClass.BALL,
                cx=0.5,
                cy=0.4,
                w=0.02,
                h=0.03,
                annotator="mira",
                source=AnnotationSource.MANUAL,
            )
        )
        db.add(DatasetMember(dataset_id=dataset.id, frame_id=frame.id, split=DatasetSplit.TRAIN))
        run = _model_run(dataset.id, TrainingStatus.PENDING)
        db.add(run)
        db.flush()
        db.add(
            ModelVersion(
                model_name="ball-detector",
                version="1.0.0",
                run_id=run.id,
                stage=ModelStage.PRODUCTION,
            )
        )

    columns = {
        "dataset_members.split": DatasetSplit.TRAIN,
        "annotations.label_class": LabelClass.BALL,
        "annotations.source": AnnotationSource.MANUAL,
        "model_runs.status": TrainingStatus.PENDING,
        "model_versions.stage": ModelStage.PRODUCTION,
    }
    with engine.connect() as conn:
        raw = {
            qualified: conn.execute(
                text(f"SELECT {qualified.split('.')[1]} FROM {qualified.split('.')[0]}")
            ).scalar_one()
            for qualified in columns
        }
    engine.dispose()
    assert raw == {qualified: member.value for qualified, member in columns.items()}


def test_one_production_version_per_model_is_db_enforced(
    pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """US-F2 backstop: a second production row for the same model name dies on
    ``uq_model_versions_one_production`` even when application logic raced."""
    engine = _migrated_engine(pg_url, monkeypatch)
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        dataset = Dataset(version="pg-registry-v1")
        db.add(dataset)
        db.flush()
        run = _model_run(dataset.id, TrainingStatus.SUCCEEDED)
        db.add(run)
        db.flush()
        run_id = run.id
        db.add_all(
            [
                ModelVersion(
                    model_name="ball-detector",
                    version="1.0.0",
                    run_id=run_id,
                    stage=ModelStage.PRODUCTION,
                ),
                # Partial: non-production stages of the same model coexist...
                ModelVersion(
                    model_name="ball-detector",
                    version="1.1.0",
                    run_id=run_id,
                    stage=ModelStage.STAGING,
                ),
                ModelVersion(
                    model_name="ball-detector",
                    version="1.2.0",
                    run_id=run_id,
                    stage=ModelStage.CANDIDATE,
                ),
                # ...and other model names keep their own production row.
                ModelVersion(
                    model_name="stump-detector",
                    version="0.1.0",
                    run_id=run_id,
                    stage=ModelStage.PRODUCTION,
                ),
            ]
        )

    with (
        pytest.raises(IntegrityError, match="uq_model_versions_one_production"),
        session_scope(factory) as db,
    ):
        db.add(
            ModelVersion(
                model_name="ball-detector",
                version="2.0.0",
                run_id=run_id,
                stage=ModelStage.PRODUCTION,
            )
        )
    engine.dispose()
