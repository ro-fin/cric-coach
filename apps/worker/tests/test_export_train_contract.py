"""fx4 <-> fx5 contract: the real export's data.yaml header parses back in training.

``scripts/export_dataset.py`` writes the provenance header that
``cricai_worker.train_detector`` verifies before any training; both sides pin it
as string literals in their own suites, so this test runs the REAL exporter
(subprocess over a file-backed SQLite DB + FsObjectStore, the same lab shape as
``packages/data/tests/test_export_dataset.py``) and feeds the actual exported
directory through the real worker-side parser and training job. Only the header
contract is asserted — version + manifest digest round-trip — never data.yaml's
byte layout, which the exporter owns. The version contains whitespace on
purpose: the datasets API accepts free-form versions and the export writes them
verbatim, so the parser must tolerate them.
"""

import datetime
import os
import subprocess
import sys
from pathlib import Path

from cricai_data.datasets import add_members, create_dataset, freeze_dataset
from cricai_data.db import create_all, make_engine, make_session_factory
from cricai_data.enums import (
    AnnotationSource,
    BowlerSource,
    DatasetSplit,
    LabelClass,
    SessionType,
)
from cricai_data.models import Annotation, FrameSample, Player, Session
from cricai_data.storage import FsObjectStore
from cricai_vision.train import FakeTrainer
from cricai_worker.context import ENV_DATABASE_URL, ENV_STORAGE_ROOT, WorkerContext
from cricai_worker.train_detector import TrainingSpec, _export_header, run_training
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPORT_SCRIPT = REPO_ROOT / "scripts" / "export_dataset.py"

#: Whitespace on purpose (see module docstring).
VERSION = "release 2026 07"


def _seed_frozen_dataset(factory: sessionmaker[OrmSession], store: FsObjectStore) -> str:
    """Seed one labeled TRAIN frame, freeze VERSION; returns the pinned digest."""
    with factory() as db:
        player = Player(name="Arjun", birthdate=datetime.date(2014, 11, 20))
        session = Session(
            player=player,
            session_date=datetime.date(2026, 7, 8),
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.COACH,
        )
        db.add_all([player, session])
        db.flush()
        frame = FrameSample(
            session_id=session.id,
            ball_no=1,
            camera_id="C1",
            frame_no=42,
            ts_ms=336,
            object_key=f"sessions/{session.id}/frames/C1/frame-000042.jpg",
            stratum={"lighting": "daylight"},
            sampler_version="frame-sampler-1",
        )
        db.add(frame)
        db.flush()
        store.put(frame.object_key, b"jpeg-bytes")
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
        dataset = create_dataset(db, version=VERSION)
        add_members(db, dataset, [(frame.id, DatasetSplit.TRAIN)])
        digest = freeze_dataset(db, dataset)
        db.commit()
    return digest


def test_real_export_header_round_trips_through_training(tmp_path: Path) -> None:
    db_url = f"sqlite:///{tmp_path / 'cricai.sqlite'}"
    engine = make_engine(db_url)
    create_all(engine)
    factory = make_session_factory(engine)
    storage_root = tmp_path / "storage"
    store = FsObjectStore(storage_root)
    digest = _seed_frozen_dataset(factory, store)

    out = tmp_path / "export"
    result = subprocess.run(
        [sys.executable, str(EXPORT_SCRIPT), "--version", VERSION, "--out", str(out)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, ENV_DATABASE_URL: db_url, ENV_STORAGE_ROOT: str(storage_root)},
    )
    assert result.returncode == 0, result.stderr

    # The header contract through the real worker-side parser: version and
    # digest round-trip exactly (no other data.yaml byte is pinned here).
    header = _export_header(out)
    assert header["version"] == VERSION
    assert header["digest"] == digest

    # And the real training job accepts the real export end to end.
    ctx = WorkerContext(session_factory=factory, store=store)
    spec = TrainingSpec(
        model_name="ball-detector",
        dataset_version=VERSION,
        dataset_dir=out,
        config={"imgsz": 640},
    )
    summary = run_training(ctx, spec, FakeTrainer(output_dir=tmp_path / "work"))
    assert summary.status == "succeeded"
    assert summary.dataset_version == VERSION
    engine.dispose()
