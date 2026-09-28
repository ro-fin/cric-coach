"""US-F1 export CLI: frozen dataset -> YOLO directory layout, loud refusals.

Runs ``scripts/export_dataset.py`` as a subprocess against a file-backed SQLite
DB + tmp FsObjectStore (no external services), mirroring how the operator runs
it with ``CRICAI_*`` env vars.
"""

import datetime
import os
import subprocess
import sys
import uuid
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
from cricai_data.models import Annotation, DatasetMember, FrameSample, Player, Session
from cricai_data.storage import FsObjectStore
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "export_dataset.py"

#: Same env names the worker context reads (kept literal: the script is the seam).
ENV_DATABASE_URL = "CRICAI_DATABASE_URL"
ENV_STORAGE_ROOT = "CRICAI_STORAGE_ROOT"


class Lab:
    """One tmp deployment: file SQLite DB + FsObjectStore + seeded dataset."""

    def __init__(self, tmp_path: Path) -> None:
        self.db_path = tmp_path / "cricai.sqlite"
        self.storage_root = tmp_path / "storage"
        self.out = tmp_path / "export"
        self.url = f"sqlite:///{self.db_path}"
        self.engine = make_engine(self.url)
        create_all(self.engine)
        self.factory = make_session_factory(self.engine)
        self.store = FsObjectStore(self.storage_root)

    def env(self) -> dict[str, str]:
        return {**os.environ, ENV_DATABASE_URL: self.url, ENV_STORAGE_ROOT: str(self.storage_root)}

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        argv = [sys.executable, str(SCRIPT), *args] if args else [sys.executable, str(SCRIPT)]
        return subprocess.run(argv, capture_output=True, text=True, check=False, env=self.env())

    def export(self, version: str = "v1") -> subprocess.CompletedProcess[str]:
        return self.run("--version", version, "--out", str(self.out))


def _seed_session(db: OrmSession) -> uuid.UUID:
    player = Player(name="Arjun", birthdate=datetime.date(2014, 11, 20))
    session = Session(
        player=player,
        session_date=datetime.date(2026, 7, 8),
        session_type=SessionType.BATTING,
        bowler_source=BowlerSource.COACH,
    )
    db.add_all([player, session])
    db.flush()
    return session.id


def _seed_frame(
    db: OrmSession, store: FsObjectStore, session_id: uuid.UUID, *, image: bool = True
) -> tuple[uuid.UUID, str]:
    frame = FrameSample(
        session_id=session_id,
        ball_no=1,
        camera_id="C1",
        frame_no=42,
        ts_ms=336,
        object_key=f"sessions/{session_id}/frames/C1/frame-000042.jpg",
        stratum={"lighting": "daylight"},
        sampler_version="frame-sampler-1",
    )
    db.add(frame)
    db.flush()
    if image:
        store.put(frame.object_key, f"jpeg-{session_id}".encode())
    return frame.id, f"{session_id}-C1-000042"


def _seed_frozen_dataset(lab: Lab, *, freeze: bool = True) -> tuple[str, str]:
    """Seed a disjoint two-session dataset; returns the (train, test) stems."""
    with lab.factory() as db:
        train_session, test_session = _seed_session(db), _seed_session(db)
        train_frame, train_stem = _seed_frame(db, lab.store, train_session)
        test_frame, test_stem = _seed_frame(db, lab.store, test_session)
        db.add(
            Annotation(
                frame_id=train_frame,
                label_class=LabelClass.BALL,
                cx=0.5,
                cy=0.4,
                w=0.02,
                h=0.03,
                annotator="mira",
                source=AnnotationSource.MANUAL,
            )
        )
        dataset = create_dataset(db, version="v1")
        add_members(
            db,
            dataset,
            [(train_frame, DatasetSplit.TRAIN), (test_frame, DatasetSplit.TEST)],
        )
        if freeze:
            freeze_dataset(db, dataset)
        db.commit()
    return train_stem, test_stem


def test_export_writes_yolo_layout(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    train_stem, test_stem = _seed_frozen_dataset(lab)
    result = lab.export()
    assert result.returncode == 0, result.stderr
    assert "exported dataset v1" in result.stdout
    assert "test=1, train=1" in result.stdout
    train_image = lab.out / "images" / "train" / f"{train_stem}.jpg"
    assert train_image.read_bytes().startswith(b"jpeg-")
    labels = (lab.out / "labels" / "train" / f"{train_stem}.txt").read_text()
    assert labels == "0 0.5 0.4 0.02 0.03\n"  # class 0 == ball, repr floats
    # The unlabeled test frame still exports with an empty label file.
    assert (lab.out / "labels" / "test" / f"{test_stem}.txt").read_text() == ""
    data_yaml = (lab.out / "data.yaml").read_text()
    assert "0: ball" in data_yaml
    assert "5: helmet" in data_yaml
    assert "train: images/train" in data_yaml
    assert "manifest digest" in data_yaml


def test_data_yaml_is_directory_stable(tmp_path: Path) -> None:
    """data.yaml must carry no machine-specific absolute path: the f2 trainer's
    dataset_digest hashes every exported byte, so two exports of the same frozen
    version — any directory, any host — must produce identical data.yaml."""
    lab = Lab(tmp_path)
    _seed_frozen_dataset(lab)
    out_a, out_b = tmp_path / "export-a", tmp_path / "export-b"
    result_a = lab.run("--version", "v1", "--out", str(out_a))
    result_b = lab.run("--version", "v1", "--out", str(out_b))
    assert result_a.returncode == 0, result_a.stderr
    assert result_b.returncode == 0, result_b.stderr
    yaml_a = (out_a / "data.yaml").read_text()
    assert yaml_a == (out_b / "data.yaml").read_text()
    assert str(out_a.resolve()) not in yaml_a
    # No `path` key at all: ultralytics resolves `path: .` against the TRAINER'S
    # CWD (Path('.').exists() is always True), not this yaml. With the key absent
    # the dataset root falls back to the yaml's own directory — correct from any
    # CWD and still byte-stable.
    assert not any(line.startswith("path:") for line in yaml_a.splitlines())


def test_export_is_rerunnable(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    _seed_frozen_dataset(lab)
    assert lab.export().returncode == 0
    result = lab.export()  # idempotent overwrite, no complaint
    assert result.returncode == 0, result.stderr


def test_unknown_version_exits_2(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    result = lab.export("ghost")
    assert result.returncode == 2
    assert "dataset version not found: ghost" in result.stderr


def test_unfrozen_dataset_is_refused(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    _seed_frozen_dataset(lab, freeze=False)
    result = lab.export()
    assert result.returncode == 2
    assert "refusing to export unfrozen dataset v1" in result.stderr
    assert not lab.out.exists()  # nothing half-written


def test_digest_mismatch_is_refused(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    _seed_frozen_dataset(lab)
    with lab.factory() as db:  # tamper behind the freeze, bypassing the service
        member = db.scalars(
            select(DatasetMember).where(DatasetMember.split == DatasetSplit.TRAIN)
        ).one()
        member.split = DatasetSplit.VAL
        db.commit()
    result = lab.export()
    assert result.returncode == 2
    assert "manifest digest mismatch" in result.stderr
    assert "membership mutated after freeze" in result.stderr


def test_missing_frame_image_is_loud(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    with lab.factory() as db:
        session_a, session_b = _seed_session(db), _seed_session(db)
        frame_a, _ = _seed_frame(db, lab.store, session_a)
        frame_b, _ = _seed_frame(db, lab.store, session_b, image=False)
        dataset = create_dataset(db, version="v1")
        add_members(db, dataset, [(frame_a, DatasetSplit.TRAIN), (frame_b, DatasetSplit.TEST)])
        freeze_dataset(db, dataset)
        db.commit()
    result = lab.export()
    assert result.returncode == 2
    assert "frame image missing from store" in result.stderr


def test_missing_required_args_exit_nonzero(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    result = lab.run()
    assert result.returncode == 2  # argparse usage error
    assert "--version" in result.stderr
