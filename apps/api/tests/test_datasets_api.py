"""US-F1 datasets API: versions+counts, membership, freeze gates, provenance, RBAC.

Frames and annotations are seeded directly in the DB (the sampling job is a
worker concern; the same shortcut test_ball_metrics_api.py uses for tags).
"""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from cricai_data.db import create_all
from cricai_data.enums import AnnotationSource, LabelClass
from cricai_data.models import Annotation, FrameSample
from cricai_data.storage import FsObjectStore
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session as DbSession

from cricai_testing.apptest import (
    COACH_TOKEN,
    PARENT_TOKEN,
    PLAYER_TOKEN,
    auth,
    make_sqlite_engine,
    make_test_app,
)

UNKNOWN = "00000000-0000-0000-0000-000000000000"

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPORT_TASKS_SCRIPT = REPO_ROOT / "scripts" / "export_label_tasks.py"


@pytest.fixture
def engine() -> Engine:
    return make_sqlite_engine()


@pytest.fixture
def client(engine: Engine, tmp_path: Path) -> TestClient:
    return TestClient(make_test_app(tmp_path, engine=engine))


def _create_session(client: TestClient) -> str:
    player = client.post(
        "/players",
        json={"name": "Arjun", "birthdate": "2014-11-20", "handedness": "right"},
        headers=auth(PARENT_TOKEN),
    ).json()
    session = client.post(
        "/sessions",
        json={
            "player_id": player["id"],
            "date": "2026-07-08",
            "session_type": "batting",
            "bowler_source": "coach",  # machine sessions must declare settings
        },
        headers=auth(PARENT_TOKEN),
    ).json()
    session_id: str = session["id"]
    return session_id


def _seed_frame(engine: Engine, session_id: str, frame_no: int) -> str:
    with DbSession(engine) as db:
        frame = FrameSample(
            session_id=uuid.UUID(session_id),
            ball_no=1,
            camera_id="C1",
            frame_no=frame_no,
            ts_ms=frame_no * 8,
            object_key=f"sessions/{session_id}/frames/C1/frame-{frame_no:06d}.jpg",
            stratum={"lighting": "daylight", "speed_band": "medium", "intent": "technical"},
            sampler_version="frame-sampler-1",
        )
        db.add(frame)
        db.commit()
        return str(frame.id)


def _seed_annotation(engine: Engine, frame_id: str, label: LabelClass) -> str:
    with DbSession(engine) as db:
        annotation = Annotation(
            frame_id=uuid.UUID(frame_id),
            label_class=label,
            cx=0.5,
            cy=0.4,
            w=0.02,
            h=0.03,
            annotator="mira",
            source=AnnotationSource.MANUAL,
        )
        db.add(annotation)
        db.commit()
        return str(annotation.id)


def _dataset(client: TestClient, version: str = "v1") -> dict[str, Any]:
    response = client.post("/datasets", json={"version": version}, headers=auth(PARENT_TOKEN))
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _add(
    client: TestClient, dataset_id: str, members: list[dict[str, str]], *, token: str = PARENT_TOKEN
) -> Any:
    return client.post(f"/datasets/{dataset_id}/members", json=members, headers=auth(token))


# --- create + list ------------------------------------------------------------------


def test_create_dataset_returns_zero_counts(client: TestClient) -> None:
    body = _dataset(client)
    assert body["version"] == "v1"
    assert body["frozen"] is False
    assert body["manifest_digest"] is None
    assert body["counts"] == {
        "train": {"frames": 0, "classes": {}},
        "val": {"frames": 0, "classes": {}},
        "test": {"frames": 0, "classes": {}},
    }


def test_duplicate_version_is_409(client: TestClient) -> None:
    _dataset(client)
    response = client.post("/datasets", json={"version": "v1"}, headers=auth(COACH_TOKEN))
    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


def test_list_versions_with_counts_per_split_and_class(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    train_frame = _seed_frame(engine, session_id, 1)
    test_frame = _seed_frame(engine, _create_session(client), 1)
    _seed_annotation(engine, train_frame, LabelClass.BALL)
    _seed_annotation(engine, train_frame, LabelClass.BALL)
    _seed_annotation(engine, train_frame, LabelClass.BAT)
    _seed_annotation(engine, test_frame, LabelClass.STUMPS)
    dataset = _dataset(client)
    _dataset(client, version="v2")
    response = _add(
        client,
        dataset["id"],
        [
            {"frame_id": train_frame, "split": "train"},
            {"frame_id": test_frame, "split": "test"},
        ],
    )
    assert response.status_code == 200
    assert response.json() == {"added": 2}
    listed = client.get("/datasets", headers=auth(COACH_TOKEN))
    assert listed.status_code == 200
    by_version = {d["version"]: d for d in listed.json()}
    assert set(by_version) == {"v1", "v2"}
    assert by_version["v1"]["counts"]["train"] == {
        "frames": 1,
        "classes": {"ball": 2, "bat": 1},
    }
    assert by_version["v1"]["counts"]["test"] == {"frames": 1, "classes": {"stumps": 1}}
    assert by_version["v1"]["counts"]["val"] == {"frames": 0, "classes": {}}
    assert by_version["v2"]["counts"]["train"] == {"frames": 0, "classes": {}}


# --- membership ---------------------------------------------------------------------


def test_add_members_unknown_dataset_and_frame_are_404(client: TestClient, engine: Engine) -> None:
    response = _add(client, UNKNOWN, [])
    assert response.status_code == 404
    assert response.json()["detail"] == "dataset not found"
    dataset = _dataset(client)
    response = _add(client, dataset["id"], [{"frame_id": UNKNOWN, "split": "train"}])
    assert response.status_code == 404
    assert "frame not found" in response.json()["detail"]


def test_add_duplicate_member_is_409(client: TestClient, engine: Engine) -> None:
    frame = _seed_frame(engine, _create_session(client), 1)
    dataset = _dataset(client)
    assert _add(client, dataset["id"], [{"frame_id": frame, "split": "train"}]).status_code == 200
    response = _add(client, dataset["id"], [{"frame_id": frame, "split": "val"}])
    assert response.status_code == 409
    assert "already a member" in response.json()["detail"]


def test_remove_member(client: TestClient, engine: Engine) -> None:
    frame = _seed_frame(engine, _create_session(client), 1)
    dataset = _dataset(client)
    _add(client, dataset["id"], [{"frame_id": frame, "split": "train"}])
    response = client.delete(
        f"/datasets/{dataset['id']}/members/{frame}", headers=auth(PARENT_TOKEN)
    )
    assert response.status_code == 204
    # Gone now: a second delete is a loud 404, and counts drop to zero.
    response = client.delete(
        f"/datasets/{dataset['id']}/members/{frame}", headers=auth(PARENT_TOKEN)
    )
    assert response.status_code == 404
    assert "not a member" in response.json()["detail"]
    listed = client.get("/datasets", headers=auth(PARENT_TOKEN)).json()
    assert listed[0]["counts"]["train"]["frames"] == 0


def test_remove_member_unknown_dataset_is_404(client: TestClient) -> None:
    response = client.delete(f"/datasets/{UNKNOWN}/members/{UNKNOWN}", headers=auth(PARENT_TOKEN))
    assert response.status_code == 404


# --- freeze -------------------------------------------------------------------------


def _disjoint_dataset(client: TestClient, engine: Engine) -> dict[str, Any]:
    train_session = _create_session(client)
    test_session = _create_session(client)
    dataset = _dataset(client)
    response = _add(
        client,
        dataset["id"],
        [
            {"frame_id": _seed_frame(engine, train_session, 1), "split": "train"},
            {"frame_id": _seed_frame(engine, train_session, 2), "split": "val"},
            {"frame_id": _seed_frame(engine, test_session, 1), "split": "test"},
        ],
    )
    assert response.status_code == 200
    return dataset


def test_freeze_pins_digest_and_makes_version_immutable(client: TestClient, engine: Engine) -> None:
    dataset = _disjoint_dataset(client, engine)
    response = client.post(f"/datasets/{dataset['id']}/freeze", headers=auth(PARENT_TOKEN))
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == "v1"
    assert body["frozen"] is True
    assert len(body["manifest_digest"]) == 64
    # Every mutation now refuses with 409 (immutability AC).
    extra = _seed_frame(engine, _create_session(client), 9)
    response = _add(client, dataset["id"], [{"frame_id": extra, "split": "train"}])
    assert response.status_code == 409
    assert "frozen" in response.json()["detail"]
    provenance = client.get(f"/frames/{extra}/provenance", headers=auth(PARENT_TOKEN)).json()
    assert provenance["datasets"] == []  # the refused frame never joined
    listed = client.get("/datasets", headers=auth(PARENT_TOKEN)).json()
    assert listed[0]["manifest_digest"] == body["manifest_digest"]


def test_delete_and_refreeze_on_frozen_dataset_are_409(client: TestClient, engine: Engine) -> None:
    dataset = _disjoint_dataset(client, engine)
    client.post(f"/datasets/{dataset['id']}/freeze", headers=auth(PARENT_TOKEN))
    member = client.get("/datasets", headers=auth(PARENT_TOKEN)).json()[0]
    assert member["frozen"] is True
    some_frame = _seed_frame(engine, _create_session(client), 77)
    response = client.delete(
        f"/datasets/{dataset['id']}/members/{some_frame}", headers=auth(PARENT_TOKEN)
    )
    assert response.status_code == 409  # frozen wins before membership lookup
    response = client.post(f"/datasets/{dataset['id']}/freeze", headers=auth(PARENT_TOKEN))
    assert response.status_code == 409
    assert "frozen" in response.json()["detail"]


def test_freeze_empty_dataset_is_409(client: TestClient) -> None:
    dataset = _dataset(client)
    response = client.post(f"/datasets/{dataset['id']}/freeze", headers=auth(PARENT_TOKEN))
    assert response.status_code == 409
    assert "empty" in response.json()["detail"]


def test_freeze_unknown_dataset_is_404(client: TestClient) -> None:
    response = client.post(f"/datasets/{UNKNOWN}/freeze", headers=auth(PARENT_TOKEN))
    assert response.status_code == 404


def test_freeze_split_leakage_is_409_with_violations(client: TestClient, engine: Engine) -> None:
    """The US-F1 AC: a test split sharing a session with train must not freeze."""
    session_id = _create_session(client)
    dataset = _dataset(client)
    _add(
        client,
        dataset["id"],
        [
            {"frame_id": _seed_frame(engine, session_id, 1), "split": "train"},
            {"frame_id": _seed_frame(engine, session_id, 2), "split": "test"},
        ],
    )
    response = client.post(f"/datasets/{dataset['id']}/freeze", headers=auth(PARENT_TOKEN))
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "test split shares" in detail["error"]
    assert detail["violations"] == [{"session_id": session_id, "splits": ["test", "train"]}]
    # Refused freeze left the dataset mutable and un-digested.
    listed = client.get("/datasets", headers=auth(PARENT_TOKEN)).json()
    assert listed[0]["frozen"] is False
    assert listed[0]["manifest_digest"] is None


# --- provenance ---------------------------------------------------------------------


def test_frame_provenance_traces_session_ball_camera_and_labels(
    client: TestClient, engine: Engine
) -> None:
    session_id = _create_session(client)
    frame = _seed_frame(engine, session_id, 42)
    annotation_id = _seed_annotation(engine, frame, LabelClass.BALL)
    dataset = _dataset(client)
    _add(client, dataset["id"], [{"frame_id": frame, "split": "train"}])
    response = client.get(f"/frames/{frame}/provenance", headers=auth(COACH_TOKEN))
    assert response.status_code == 200
    body = response.json()
    assert body["frame_id"] == frame
    assert body["session_id"] == session_id
    assert body["ball_no"] == 1
    assert body["camera_id"] == "C1"
    assert body["frame_no"] == 42
    assert body["ts_ms"] == 42 * 8
    assert body["object_key"].endswith("frame-000042.jpg")
    assert body["stratum"]["lighting"] == "daylight"
    assert body["sampler_version"] == "frame-sampler-1"
    assert body["annotations"] == [
        {
            "id": annotation_id,
            "label_class": "ball",
            "cx": 0.5,
            "cy": 0.4,
            "w": 0.02,
            "h": 0.03,
            "annotator": "mira",
            "source": "manual",
        }
    ]
    assert body["datasets"] == [{"dataset_version": "v1", "split": "train"}]


def test_frame_provenance_unknown_frame_is_404(client: TestClient) -> None:
    response = client.get(f"/frames/{UNKNOWN}/provenance", headers=auth(PARENT_TOKEN))
    assert response.status_code == 404
    assert response.json()["detail"] == "frame not found"


# --- label import (the write half of the US-F1 round trip) ---------------------------


def _ls_task(
    frame_id: str,
    session_id: str,
    frame_no: int,
    boxes: list[dict[str, Any]],
    *,
    camera_id: str = "C1",
) -> dict[str, Any]:
    """One Label Studio task whose provenance matches a `_seed_frame` row."""
    return {
        "data": {
            "image": "img.jpg",
            "cricai": {
                "frame_id": frame_id,
                "session_id": session_id,
                "ball_no": 1,
                "camera_id": camera_id,
                "frame_no": frame_no,
                "ts_ms": frame_no * 8,
                "object_key": f"sessions/{session_id}/frames/C1/frame-{frame_no:06d}.jpg",
            },
        },
        "annotations": [{"completed_by": 7, "result": boxes}],
    }


def _rect(label: str = "ball", *, x: float = 25.0) -> dict[str, Any]:
    """Percent geometry deriving to cx=(x+5)/100, cy=0.5, w=0.1, h=0.2."""
    return {
        "type": "rectanglelabels",
        "value": {"x": x, "y": 40.0, "width": 10.0, "height": 20.0, "rectanglelabels": [label]},
        "meta": {"annotator": "mira", "source": "manual"},
    }


def _import(client: TestClient, payload: Any, *, token: str = COACH_TOKEN) -> Any:
    return client.post("/annotations/import", json=payload, headers=auth(token))


def test_import_labels_persists_annotation_rows(client: TestClient, engine: Engine) -> None:
    """The US-F1 round trip lands: import file -> Annotation rows in the DB."""
    session_id = _create_session(client)
    frame = _seed_frame(engine, session_id, 42)
    response = _import(client, [_ls_task(frame, session_id, 42, [_rect()])])
    assert response.status_code == 200, response.text
    assert response.json() == {"frames": 1, "annotations": 1, "replaced": 0}
    body = client.get(f"/frames/{frame}/provenance", headers=auth(COACH_TOKEN)).json()
    (annotation,) = body["annotations"]
    assert annotation["label_class"] == "ball"
    assert annotation["cx"] == pytest.approx(0.3)
    assert annotation["cy"] == pytest.approx(0.5)
    assert annotation["w"] == pytest.approx(0.1)
    assert annotation["h"] == pytest.approx(0.2)
    assert annotation["annotator"] == "mira"
    assert annotation["source"] == "manual"


def test_reimport_replaces_a_frames_annotations(client: TestClient, engine: Engine) -> None:
    """Re-importing an edited export replaces the frame's labels — never appends."""
    session_id = _create_session(client)
    frame = _seed_frame(engine, session_id, 42)
    assert _import(client, [_ls_task(frame, session_id, 42, [_rect()])]).status_code == 200
    edited = [_ls_task(frame, session_id, 42, [_rect(), _rect("bat", x=60.0)])]
    response = _import(client, edited)
    assert response.status_code == 200, response.text
    assert response.json() == {"frames": 1, "annotations": 2, "replaced": 1}
    body = client.get(f"/frames/{frame}/provenance", headers=auth(COACH_TOKEN)).json()
    assert sorted(a["label_class"] for a in body["annotations"]) == ["ball", "bat"]


def test_import_accepts_hard_case_choice_tags(client: TestClient, engine: Engine) -> None:
    """Files carrying the guide-mandated frame tags (blur/feed_exit) import fine."""
    session_id = _create_session(client)
    frame = _seed_frame(engine, session_id, 42)
    task = _ls_task(frame, session_id, 42, [_rect()])
    task["annotations"][0]["result"].append(
        {"type": "choices", "from_name": "case", "to_name": "image", "value": {"choices": ["blur"]}}
    )
    response = _import(client, [task])
    assert response.status_code == 200, response.text
    assert response.json() == {"frames": 1, "annotations": 1, "replaced": 0}


def test_import_unknown_frame_is_404(client: TestClient) -> None:
    ghost = str(uuid.uuid4())
    response = _import(client, [_ls_task(ghost, str(uuid.uuid4()), 1, [_rect()])])
    assert response.status_code == 404
    assert f"frame not found: {ghost}" in response.json()["detail"]


def test_import_non_uuid_frame_id_is_422(client: TestClient) -> None:
    response = _import(client, [_ls_task("f-1", str(uuid.uuid4()), 1, [_rect()])])
    assert response.status_code == 422
    assert "not a valid frame id" in response.json()["detail"]


def test_import_provenance_mismatch_is_409(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    frame = _seed_frame(engine, session_id, 42)
    task = _ls_task(frame, session_id, 42, [_rect()], camera_id="C9")  # wrong camera
    response = _import(client, [task])
    assert response.status_code == 409
    assert "provenance mismatch" in response.json()["detail"]


@pytest.mark.parametrize(("field", "stale"), [("ts_ms", 999), ("ball_no", 2)])
def test_import_stale_pixel_identity_is_409(
    client: TestClient, engine: Engine, field: str, stale: int
) -> None:
    """ts_ms/ball_no are pixel identity: a sampler rewrite at the same
    session/camera/frame_no re-extracts different pixels, so a file exported
    before the rewrite must refuse to import (never silently label new pixels)."""
    session_id = _create_session(client)
    frame = _seed_frame(engine, session_id, 42)
    task = _ls_task(frame, session_id, 42, [_rect()])
    task["data"]["cricai"][field] = stale  # stored: ts_ms=336, ball_no=1
    response = _import(client, [task])
    assert response.status_code == 409
    assert "provenance mismatch" in response.json()["detail"]
    body = client.get(f"/frames/{frame}/provenance", headers=auth(COACH_TOKEN)).json()
    assert body["annotations"] == []  # nothing persisted from the stale file


def test_import_duplicate_frame_tasks_are_422(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    frame = _seed_frame(engine, session_id, 42)
    task = _ls_task(frame, session_id, 42, [_rect()])
    response = _import(client, [task, task])
    assert response.status_code == 422
    assert "duplicate task" in response.json()["detail"]


def test_import_malformed_file_is_422(client: TestClient) -> None:
    response = _import(client, [{"data": {"image": "img.jpg"}, "annotations": []}])
    assert response.status_code == 422
    assert "lost its provenance" in response.json()["detail"]


def test_import_into_frozen_dataset_member_is_409(client: TestClient, engine: Engine) -> None:
    """Frozen versions pin label content in their digest, so the shipped write
    path must refuse label changes on member frames (fixes go into a new version)."""
    session_id = _create_session(client)
    frame = _seed_frame(engine, session_id, 1)
    test_frame = _seed_frame(engine, _create_session(client), 1)
    dataset = _dataset(client)
    _add(
        client,
        dataset["id"],
        [{"frame_id": frame, "split": "train"}, {"frame_id": test_frame, "split": "test"}],
    )
    freeze = client.post(f"/datasets/{dataset['id']}/freeze", headers=auth(PARENT_TOKEN))
    assert freeze.status_code == 200
    response = _import(client, [_ls_task(frame, session_id, 1, [_rect()])])
    assert response.status_code == 409
    assert "frozen dataset" in response.json()["detail"]
    assert "v1" in response.json()["detail"]


def test_label_round_trip_export_script_to_import_endpoint(tmp_path: Path) -> None:
    """The whole US-F1 labeling loop over one deployment: sampled frames ->
    tasks file via scripts/export_label_tasks.py -> annotator draws a box ->
    POST /annotations/import -> Annotation rows, provenance intact end to end."""
    db_path = tmp_path / "cricai.sqlite"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    create_all(engine)
    client = TestClient(make_test_app(tmp_path, engine=engine))
    session_id = _create_session(client)
    frame = _seed_frame(engine, session_id, 42)
    object_key = f"sessions/{session_id}/frames/C1/frame-000042.jpg"
    FsObjectStore(tmp_path / "storage").put(object_key, b"jpeg-e2e")
    tasks_path = tmp_path / "tasks.json"
    env = {
        **os.environ,
        "CRICAI_DATABASE_URL": f"sqlite:///{db_path}",
        "CRICAI_STORAGE_ROOT": str(tmp_path / "storage"),
    }
    argv = [sys.executable, str(EXPORT_TASKS_SCRIPT)]
    argv += ["--session-id", session_id, "--out", str(tasks_path)]
    result = subprocess.run(argv, capture_output=True, text=True, check=False, env=env)
    assert result.returncode == 0, result.stderr
    tasks = json.loads(tasks_path.read_text())
    assert [task["data"]["cricai"]["frame_id"] for task in tasks] == [frame]
    # The annotator draws one ball box in Label Studio and exports the file.
    tasks[0]["annotations"] = [{"completed_by": 7, "result": [_rect()]}]
    response = _import(client, tasks)
    assert response.status_code == 200, response.text
    assert response.json() == {"frames": 1, "annotations": 1, "replaced": 0}
    body = client.get(f"/frames/{frame}/provenance", headers=auth(COACH_TOKEN)).json()
    (annotation,) = body["annotations"]
    assert annotation["label_class"] == "ball"
    assert annotation["annotator"] == "mira"
    assert annotation["source"] == "manual"


def test_import_is_atomic_across_tasks(client: TestClient, engine: Engine) -> None:
    """One bad task rejects the whole file: nothing from the good tasks persists."""
    session_id = _create_session(client)
    frame = _seed_frame(engine, session_id, 42)
    good = _ls_task(frame, session_id, 42, [_rect()])
    bad = _ls_task(str(uuid.uuid4()), session_id, 43, [_rect()])
    response = _import(client, [good, bad])
    assert response.status_code == 404
    body = client.get(f"/frames/{frame}/provenance", headers=auth(COACH_TOKEN)).json()
    assert body["annotations"] == []  # the good task rolled back with the bad one


# --- RBAC ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/datasets"),
        ("GET", "/datasets"),
        ("POST", f"/datasets/{UNKNOWN}/members"),
        ("DELETE", f"/datasets/{UNKNOWN}/members/{UNKNOWN}"),
        ("POST", f"/datasets/{UNKNOWN}/freeze"),
        ("POST", "/annotations/import"),
        ("GET", f"/frames/{UNKNOWN}/provenance"),
    ],
)
def test_rbac_requires_token_and_blocks_players(client: TestClient, method: str, path: str) -> None:
    json_body: Any = {"version": "vx"} if path == "/datasets" and method == "POST" else []
    anonymous = client.request(method, path, json=json_body)
    assert anonymous.status_code == 401
    # ML-ops surface: players get 403 even for reads (guest provenance, US-L3).
    player = client.request(method, path, json=json_body, headers=auth(PLAYER_TOKEN))
    assert player.status_code == 403
    coach = client.request(method, path, json=json_body, headers=auth(COACH_TOKEN))
    assert coach.status_code not in (401, 403)
