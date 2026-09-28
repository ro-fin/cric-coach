"""IT (US-F2): concurrent production promotions serialize on real PostgreSQL.

The 'exactly one production version per model name' invariant must hold under
concurrent promote requests. The test drives the exact race: request A reads the
current production row and flushes its demote+promote, then request B runs a full
promotion on a second connection before A commits. Without the promotion path
taking row locks, both requests gate against the same stale production row and
both commit as production.
"""

import threading
import uuid
from typing import Any

import pytest
from cricai_api.routers.models import PromoteIn, promote_version
from cricai_data.db import create_all, make_engine, make_session_factory
from cricai_data.enums import ModelStage, Role, TrainingStatus
from cricai_data.models import Dataset, ModelRun, ModelVersion
from cricai_vision.train import HEADLINE_METRIC_KEYS
from sqlalchemy import select

pytestmark = pytest.mark.integration

GOOD_METRICS: dict[str, Any] = dict.fromkeys(HEADLINE_METRIC_KEYS, 0.9)


def _seed_registry(factory: Any) -> tuple[uuid.UUID, uuid.UUID]:
    """One production version P plus two staging challengers A and B."""
    with factory() as db:
        dataset = Dataset(version="v1", frozen=True, manifest_digest="a" * 64)
        db.add(dataset)
        db.flush()
        version_ids: list[uuid.UUID] = []
        for name, stage in (
            ("p", ModelStage.PRODUCTION),
            ("a", ModelStage.STAGING),
            ("b", ModelStage.STAGING),
        ):
            run = ModelRun(
                model_name="ball-detector",
                dataset_id=dataset.id,
                config={"epochs": 50},
                metrics=dict(GOOD_METRICS),
                report_key=f"models/ball-detector/runs/{name}/eval-report.json",
                status=TrainingStatus.SUCCEEDED,
                trainer_version="fake-trainer-1",
            )
            db.add(run)
            db.flush()
            version = ModelVersion(
                model_name="ball-detector", version=f"{name}-1.0.0", run_id=run.id, stage=stage
            )
            db.add(version)
            db.flush()
            version_ids.append(version.id)
        db.commit()
        return version_ids[1], version_ids[2]


def test_concurrent_promotions_keep_exactly_one_production(pg_url: str) -> None:
    engine = make_engine(pg_url)
    create_all(engine)
    factory = make_session_factory(engine)
    a_id, b_id = _seed_registry(factory)

    session_a = factory()
    session_b = factory()
    errors: list[BaseException] = []
    started = threading.Event()

    def promote_b() -> None:
        started.set()
        try:
            promote_version(
                b_id, PromoteIn(target_stage=ModelStage.PRODUCTION), session_b, Role.COACH
            )
            session_b.commit()
        except BaseException as exc:  # surfaced in the main thread
            errors.append(exc)

    try:
        # A performs its read + gate + demote + promote (flushed, uncommitted) ...
        promote_version(a_id, PromoteIn(target_stage=ModelStage.PRODUCTION), session_a, Role.PARENT)
        # ... while B races a full promotion of the same model name to commit.
        thread = threading.Thread(target=promote_b)
        thread.start()
        assert started.wait(timeout=5)
        thread.join(timeout=2)  # B either finished (unlocked) or waits on A's row locks
        session_a.commit()
        thread.join()
        assert errors == []
    finally:
        session_a.close()
        session_b.close()

    with factory() as db:
        production = db.scalars(
            select(ModelVersion).where(
                ModelVersion.model_name == "ball-detector",
                ModelVersion.stage == ModelStage.PRODUCTION,
            )
        ).all()
        # The invariant, not the winner: exactly one production version survives.
        assert len(production) == 1, [row.version for row in production]
    engine.dispose()
