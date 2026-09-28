"""US-F1 labeling-task export CLI: session frames -> Label Studio tasks file.

Runs ``scripts/export_label_tasks.py`` as a subprocess against a file-backed
SQLite DB + tmp FsObjectStore (no external services), mirroring how the
operator runs it with ``CRICAI_*`` env vars. This is the export half of the
labeling round trip; the write half is ``POST /annotations/import``.
"""

import datetime
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from cricai_data.db import create_all, make_engine, make_session_factory
from cricai_data.enums import AnnotationSource, BowlerSource, LabelClass, SessionType
from cricai_data.models import Annotation, FrameSample, Player, Session
from cricai_data.storage import FsObjectStore
from sqlalchemy.orm import Session as OrmSession

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "export_label_tasks.py"

#: Same env names the worker context reads (kept literal: the script is the seam).
ENV_DATABASE_URL = "CRICAI_DATABASE_URL"
ENV_STORAGE_ROOT = "CRICAI_STORAGE_ROOT"


class Lab:
    """One tmp deployment: file SQLite DB + FsObjectStore."""

    def __init__(self, tmp_path: Path) -> None:
        self.db_path = tmp_path / "cricai.sqlite"
        self.storage_root = tmp_path / "storage"
        self.out = tmp_path / "tasks.json"
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

    def export(self, session_id: str, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.run("--session-id", session_id, "--out", str(self.out), *extra)

    def tasks(self) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = json.loads(self.out.read_text())
        return payload


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
    db: OrmSession,
    store: FsObjectStore,
    session_id: uuid.UUID,
    *,
    frame_no: int = 42,
    image: bool = True,
) -> tuple[uuid.UUID, str]:
    key = f"sessions/{session_id}/frames/C1/frame-{frame_no:06d}.jpg"
    frame = FrameSample(
        session_id=session_id,
        ball_no=1,
        camera_id="C1",
        frame_no=frame_no,
        ts_ms=frame_no * 8,
        object_key=key,
        stratum={"lighting": "daylight"},
        sampler_version="frame-sampler-1",
    )
    db.add(frame)
    db.flush()
    if image:
        store.put(key, f"jpeg-{frame_no}".encode())
    return frame.id, key


def _seed_annotation(db: OrmSession, frame_id: uuid.UUID, *, cy: float = 0.4) -> None:
    db.add(
        Annotation(
            frame_id=frame_id,
            label_class=LabelClass.BALL,
            cx=0.5,
            cy=cy,
            w=0.02,
            h=0.03,
            annotator="mira",
            source=AnnotationSource.MANUAL,
        )
    )


def test_export_writes_tasks_with_provenance_and_prelabels(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    with lab.factory() as db:
        session_id = _seed_session(db)
        labeled, labeled_key = _seed_frame(db, lab.store, session_id, frame_no=42)
        unlabeled, _ = _seed_frame(db, lab.store, session_id, frame_no=43)
        _seed_annotation(db, labeled)
        db.commit()
    result = lab.export(str(session_id))
    assert result.returncode == 0, result.stderr
    assert f"exported 2 labeling tasks for session {session_id}" in result.stdout
    first, second = lab.tasks()
    assert first["data"]["image"] == labeled_key  # default: the raw object key
    assert first["data"]["cricai"] == {
        "frame_id": str(labeled),
        "session_id": str(session_id),
        "ball_no": 1,
        "camera_id": "C1",
        "frame_no": 42,
        "ts_ms": 336,
        "object_key": labeled_key,
    }
    (box,) = first["annotations"][0]["result"]
    assert box["value"]["rectanglelabels"] == ["ball"]
    assert box["meta"]["norm"] == {"cx": 0.5, "cy": 0.4, "w": 0.02, "h": 0.03}
    assert box["meta"]["annotator"] == "mira"
    # The unlabeled frame exports as a task with an empty pre-label result.
    assert second["data"]["cricai"]["frame_id"] == str(unlabeled)
    assert second["annotations"] == [{"result": []}]


def test_image_url_prefix_maps_keys_to_the_serving_root(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    with lab.factory() as db:
        session_id = _seed_session(db)
        _, key = _seed_frame(db, lab.store, session_id)
        db.commit()
    result = lab.export(str(session_id), "--image-url-prefix", "/data/local-files/?d=")
    assert result.returncode == 0, result.stderr
    (task,) = lab.tasks()
    assert task["data"]["image"] == f"/data/local-files/?d={key}"


def test_export_is_rerunnable(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    with lab.factory() as db:
        session_id = _seed_session(db)
        _seed_frame(db, lab.store, session_id)
        db.commit()
    assert lab.export(str(session_id)).returncode == 0
    result = lab.export(str(session_id))  # idempotent overwrite, no complaint
    assert result.returncode == 0, result.stderr


def test_unknown_session_exits_2(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    ghost = str(uuid.uuid4())
    result = lab.export(ghost)
    assert result.returncode == 2
    assert f"session not found: {ghost}" in result.stderr
    assert not lab.out.exists()  # nothing half-written


def test_non_uuid_session_id_exits_2(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    result = lab.export("s-1")
    assert result.returncode == 2
    assert "--session-id is not a UUID: s-1" in result.stderr


def test_session_without_sampled_frames_is_refused(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    with lab.factory() as db:
        session_id = _seed_session(db)
        db.commit()
    result = lab.export(str(session_id))
    assert result.returncode == 2
    assert "has no sampled frames" in result.stderr
    assert "sample_frames" in result.stderr  # points at the missing pipeline step


def test_missing_frame_image_is_loud(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    with lab.factory() as db:
        session_id = _seed_session(db)
        _seed_frame(db, lab.store, session_id, image=False)
        db.commit()
    result = lab.export(str(session_id))
    assert result.returncode == 2
    assert "frame image missing from store" in result.stderr


def test_corrupt_stored_box_is_loud(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    with lab.factory() as db:
        session_id = _seed_session(db)
        frame_id, _ = _seed_frame(db, lab.store, session_id)
        _seed_annotation(db, frame_id, cy=-2.0)  # corrupt row behind the service
        db.commit()
    result = lab.export(str(session_id))
    assert result.returncode == 2
    assert "cy must be finite and within [0, 1]" in result.stderr


def test_missing_required_args_exit_nonzero(tmp_path: Path) -> None:
    lab = Lab(tmp_path)
    result = lab.run()
    assert result.returncode == 2  # argparse usage error
    assert "--session-id" in result.stderr
