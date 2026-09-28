"""US-B2 acceptance: resumable multi-camera upload, checksum verify, probe, dedupe."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from cricai_api.routers import videos as videos_router
from cricai_data.lifecycle import SessionState
from cricai_data.models import Session, UploadSession
from cricai_data.probe import ProbeError, ProbeUnavailableError, VideoProbe
from cricai_data.storage import FsObjectStore, sha256_hex
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session as DbSession

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, PLAYER_TOKEN, auth

PROBED = VideoProbe(
    fps=120.0, width=1920, height=1080, resolution="1920x1080", codec="h264", duration_s=60.0
)
CONTENT = b"cricai-frame-payload-" * 500  # 10.5 KB split across parts
OTHER_CONTENT = b"a-different-camera-take" * 400


@pytest.fixture
def fake_probe(monkeypatch: pytest.MonkeyPatch) -> VideoProbe:
    monkeypatch.setattr(videos_router, "probe_video", lambda _source: PROBED)
    return PROBED


def _store(client: TestClient) -> FsObjectStore:
    app = cast(FastAPI, client.app)
    store: FsObjectStore = app.state.store
    return store


@contextmanager
def _db(client: TestClient) -> Iterator[DbSession]:
    app = cast(FastAPI, client.app)
    session: DbSession = app.state.session_factory()
    try:
        yield session
        session.commit()
    finally:
        session.close()


def _create_session(client: TestClient) -> str:
    player = client.post(
        "/players",
        json={"name": "Arjun", "birthdate": "2014-11-20"},
        headers=auth(PARENT_TOKEN),
    ).json()
    response = client.post(
        "/sessions",
        json={
            "player_id": player["id"],
            "date": "2026-07-07",
            "session_type": "batting",
            "bowler_source": "coach",
        },
        headers=auth(PARENT_TOKEN),
    )
    session_id: str = response.json()["id"]
    return session_id


def _upload_payload(content: bytes = CONTENT, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "camera_id": "C1",
        "filename": "front.mp4",
        "declared_checksum": sha256_hex(content),
        "declared_size": len(content),
    }
    payload.update(overrides)
    return payload


def _begin(
    client: TestClient, session_id: str, content: bytes = CONTENT, **overrides: Any
) -> tuple[str, str]:
    response = client.post(
        f"/sessions/{session_id}/videos/uploads",
        json=_upload_payload(content, **overrides),
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 201
    body = response.json()
    upload_id: str = body["upload_id"]
    store_upload_id: str = body["store_upload_id"]
    return upload_id, store_upload_id


def _put_part(
    client: TestClient,
    upload_id: str,
    store_upload_id: str,
    part_no: int,
    data: bytes,
    token: str = PARENT_TOKEN,
) -> httpx.Response:
    return client.put(
        f"/videos/uploads/{upload_id}/parts/{part_no}",
        params={"store_upload_id": store_upload_id},
        content=data,
        headers=auth(token),
    )


def _get_status(
    client: TestClient, upload_id: str, store_upload_id: str, token: str = PARENT_TOKEN
) -> httpx.Response:
    return client.get(
        f"/videos/uploads/{upload_id}",
        params={"store_upload_id": store_upload_id},
        headers=auth(token),
    )


def _complete(
    client: TestClient,
    upload_id: str,
    store_upload_id: str,
    part_count: int,
    token: str = PARENT_TOKEN,
) -> httpx.Response:
    return client.post(
        f"/videos/uploads/{upload_id}/complete",
        json={"store_upload_id": store_upload_id, "part_count": part_count},
        headers=auth(token),
    )


def _chunks(content: bytes, size: int = 4000) -> list[bytes]:
    return [content[i : i + size] for i in range(0, len(content), size)]


def _upload_all(
    client: TestClient, session_id: str, content: bytes = CONTENT, **overrides: Any
) -> httpx.Response:
    upload_id, store_upload_id = _begin(client, session_id, content, **overrides)
    parts = _chunks(content)
    for part_no, data in enumerate(parts, start=1):
        assert _put_part(client, upload_id, store_upload_id, part_no, data).status_code == 200
    return _complete(client, upload_id, store_upload_id, len(parts))


def _list_videos(client: TestClient, session_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/sessions/{session_id}/videos", headers=auth(PARENT_TOKEN))
    assert response.status_code == 200
    videos: list[dict[str, Any]] = response.json()
    return videos


def test_full_multipart_upload_happy_path(client: TestClient, fake_probe: VideoProbe) -> None:
    session_id = _create_session(client)
    upload_id, store_upload_id = _begin(
        client,
        session_id,
        claimed_fps=120.0,
        claimed_resolution="1920x1080",
        claimed_duration_s=60.0,
    )
    parts = _chunks(CONTENT)
    for part_no, data in enumerate(parts, start=1):
        response = _put_part(client, upload_id, store_upload_id, part_no, data)
        assert response.status_code == 200
        assert response.json() == {
            "part_no": part_no,
            "size_bytes": len(data),
            "checksum_sha256": sha256_hex(data),
        }
    response = _complete(client, upload_id, store_upload_id, len(parts))
    assert response.status_code == 200
    body = response.json()
    assert body["deduplicated"] is False
    video = body["video"]
    assert video["status"] == "probed"
    assert video["checksum_sha256"] == sha256_hex(CONTENT)
    assert video["size_bytes"] == len(CONTENT)
    assert video["object_key"] == f"sessions/{session_id}/C1/front.mp4"
    assert video["codec"] == "h264"
    assert video["probe"]["fps"] == 120.0
    assert video["error"] is None
    assert _store(client).get(video["object_key"]) == CONTENT

    listed = client.get(f"/sessions/{session_id}/videos", headers=auth(COACH_TOKEN))
    assert listed.status_code == 200
    assert [(v["filename"], v["status"]) for v in listed.json()] == [("front.mp4", "probed")]


def test_store_upload_id_persisted_on_creation(client: TestClient) -> None:
    """US-L3 purge needs the store's multipart id on the row, not just client echoes."""
    session_id = _create_session(client)
    upload_id, store_upload_id = _begin(client, session_id)
    with _db(client) as db:
        row = db.get(UploadSession, uuid.UUID(upload_id))
        assert row is not None
        assert row.store_upload_id == store_upload_id


def test_mismatched_store_upload_id_conflicts(client: TestClient) -> None:
    """The client-echoed store id must match the persisted one on every call."""
    session_id = _create_session(client)
    upload_id, _store_upload_id = _begin(client, session_id)
    _other_id, other_store_id = _begin(client, session_id, filename="other.mp4")

    assert _put_part(client, upload_id, other_store_id, 1, b"x").status_code == 409
    assert _get_status(client, upload_id, other_store_id).status_code == 409
    assert _complete(client, upload_id, other_store_id, 1).status_code == 409
    assert _put_part(client, upload_id, "bogus", 1, b"x").status_code == 409
    assert _get_status(client, upload_id, "bogus").status_code == 409
    assert _complete(client, upload_id, "bogus", 1).status_code == 409
    # nothing leaked into the other upload's staging area
    assert _store(client).list_parts(other_store_id) == {}


def test_upload_without_persisted_store_id_conflicts(client: TestClient) -> None:
    """A row missing its persisted multipart id can never be resumed silently."""
    session_id = _create_session(client)
    with _db(client) as db:
        legacy = UploadSession(
            session_id=uuid.UUID(session_id),
            camera_id="C1",
            filename="legacy.mp4",
            declared_checksum=sha256_hex(CONTENT),
            declared_size=len(CONTENT),
            store_upload_id=None,
        )
        db.add(legacy)
        db.flush()
        legacy_id = str(legacy.id)
    assert _put_part(client, legacy_id, "anything", 1, b"x").status_code == 409


def test_resume_lists_received_parts_then_completes(
    client: TestClient, fake_probe: VideoProbe
) -> None:
    session_id = _create_session(client)
    upload_id, store_upload_id = _begin(client, session_id)
    parts = _chunks(CONTENT)
    _put_part(client, upload_id, store_upload_id, 1, parts[0])
    _put_part(client, upload_id, store_upload_id, 3, parts[2])

    state = _get_status(client, upload_id, store_upload_id).json()
    assert state["completed"] is False
    assert state["declared_size"] == len(CONTENT)
    assert [p["part_no"] for p in state["received_parts"]] == [1, 3]

    _put_part(client, upload_id, store_upload_id, 2, parts[1])
    response = _complete(client, upload_id, store_upload_id, 3)
    assert response.json()["video"]["status"] == "probed"

    # after completion staging is gone; the endpoint reports parts from the DB
    done = _get_status(client, upload_id, store_upload_id).json()
    assert done["completed"] is True
    assert [p["part_no"] for p in done["received_parts"]] == [1, 2, 3]


def test_reuploaded_part_replaces_previous(client: TestClient, fake_probe: VideoProbe) -> None:
    session_id = _create_session(client)
    content = b"the-final-cut"
    upload_id, store_upload_id = _begin(client, session_id, content=content)
    _put_part(client, upload_id, store_upload_id, 1, b"interrupted-garbage")
    _put_part(client, upload_id, store_upload_id, 1, content)

    state = _get_status(client, upload_id, store_upload_id).json()
    assert state["received_parts"] == [
        {"part_no": 1, "size_bytes": len(content), "checksum_sha256": sha256_hex(content)}
    ]
    response = _complete(client, upload_id, store_upload_id, 1)
    assert response.json()["video"]["status"] == "probed"
    assert _store(client).get(f"sessions/{session_id}/C1/front.mp4") == content


def test_status_reports_only_parts_present_in_store(client: TestClient) -> None:
    session_id = _create_session(client)
    upload_id, store_upload_id = _begin(client, session_id)
    _put_part(client, upload_id, store_upload_id, 1, b"part-one")
    _put_part(client, upload_id, store_upload_id, 2, b"part-two")
    # part 1's bytes vanish from staging out-of-band — only part 2 is resumable
    (_store(client)._uploads / store_upload_id / "000001.part").unlink()

    state = _get_status(client, upload_id, store_upload_id).json()
    assert [p["part_no"] for p in state["received_parts"]] == [2]


def test_lost_staging_reports_unknown_store_upload(client: TestClient) -> None:
    session_id = _create_session(client)
    upload_id, store_upload_id = _begin(client, session_id)
    _put_part(client, upload_id, store_upload_id, 1, b"part-one")
    _store(client).abort_multipart(store_upload_id)  # staging lost out-of-band

    assert _put_part(client, upload_id, store_upload_id, 2, b"x").status_code == 404
    assert _get_status(client, upload_id, store_upload_id).status_code == 404
    assert _complete(client, upload_id, store_upload_id, 1).status_code == 404


def test_corrupted_upload_marked_failed_with_readable_error(client: TestClient) -> None:
    session_id = _create_session(client)
    upload_id, store_upload_id = _begin(client, session_id)  # declares checksum of CONTENT
    _put_part(client, upload_id, store_upload_id, 1, b"corrupted-bytes")
    response = _complete(client, upload_id, store_upload_id, 1)
    assert response.status_code == 200
    body = response.json()
    assert body["deduplicated"] is False
    video = body["video"]
    assert video["status"] == "failed"
    assert "checksum mismatch" in video["error"]
    assert "upload the file again" in video["error"]
    assert video["checksum_sha256"] == sha256_hex(b"corrupted-bytes")
    assert not _store(client).exists(f"sessions/{session_id}/C1/front.mp4")
    assert not _store(client).exists(video["object_key"])  # corrupt object removed

    assert [v["status"] for v in _list_videos(client, session_id)] == ["failed"]


def test_retry_after_failure_succeeds(client: TestClient, fake_probe: VideoProbe) -> None:
    session_id = _create_session(client)
    upload_id, store_upload_id = _begin(client, session_id)
    _put_part(client, upload_id, store_upload_id, 1, b"corrupted-bytes")
    assert _complete(client, upload_id, store_upload_id, 1).json()["video"]["status"] == "failed"

    response = _upload_all(client, session_id)  # same filename, now with intact bytes
    video = response.json()["video"]
    assert video["status"] == "probed"
    assert video["object_key"] == f"sessions/{session_id}/C1/front.mp4"

    statuses = {v["status"] for v in _list_videos(client, session_id)}
    assert statuses == {"failed", "probed"}


def test_reupload_after_bad_declaration_stores_content(
    client: TestClient, fake_probe: VideoProbe
) -> None:
    """A FAILED attempt must never swallow the corrective re-upload as a dedupe."""
    session_id = _create_session(client)
    wrong = sha256_hex(b"not-what-the-file-hashes-to")
    failed = _upload_all(client, session_id, declared_checksum=wrong).json()["video"]
    assert failed["status"] == "failed"
    assert failed["checksum_sha256"] == sha256_hex(CONTENT)  # assembled hash recorded

    response = _upload_all(client, session_id)  # same content, correct declaration
    assert response.status_code == 200
    body = response.json()
    assert body["deduplicated"] is False
    video = body["video"]
    assert video["status"] == "probed"
    assert video["object_key"] == f"sessions/{session_id}/C1/front.mp4"
    assert _store(client).get(video["object_key"]) == CONTENT
    assert video["id"] == failed["id"]  # the failed row is refreshed, not duplicated
    assert [v["status"] for v in _list_videos(client, session_id)] == ["probed"]


def test_repeated_corrupted_upload_refreshes_failed_row(client: TestClient) -> None:
    session_id = _create_session(client)
    declared = sha256_hex(CONTENT)
    first = _upload_all(
        client, session_id, content=b"corrupted-bytes", declared_checksum=declared
    ).json()["video"]
    assert first["status"] == "failed"

    second = _upload_all(
        client, session_id, content=b"corrupted-bytes", declared_checksum=declared
    ).json()
    assert second["deduplicated"] is False
    assert second["video"]["status"] == "failed"
    assert second["video"]["id"] == first["id"]  # same content, same row — refreshed
    assert [v["status"] for v in _list_videos(client, session_id)] == ["failed"]


def test_duplicate_content_deduplicated(client: TestClient, fake_probe: VideoProbe) -> None:
    session_id = _create_session(client)
    first = _upload_all(client, session_id).json()["video"]

    # same content under a different filename
    second = _upload_all(client, session_id, filename="copy.mp4")
    assert second.status_code == 200
    assert second.json()["deduplicated"] is True
    assert second.json()["video"]["id"] == first["id"]
    assert not _store(client).exists(f"sessions/{session_id}/C1/copy.mp4")

    # same content under the same filename
    third = _upload_all(client, session_id)
    assert third.json()["deduplicated"] is True
    assert _store(client).get(first["object_key"]) == CONTENT  # original untouched

    assert len(_list_videos(client, session_id)) == 1  # never double-stored


def test_reupload_restores_purged_object(client: TestClient, fake_probe: VideoProbe) -> None:
    """A row whose object was purged is no dedupe target — content is re-stored."""
    session_id = _create_session(client)
    first = _upload_all(client, session_id).json()["video"]
    assert _store(client).delete(first["object_key"]) is True  # e.g. retention purge

    response = _upload_all(client, session_id)  # the exact same file again
    body = response.json()
    assert body["deduplicated"] is False  # nothing verifiable left to dedupe against
    assert body["video"]["id"] == first["id"]
    assert body["video"]["status"] == "probed"
    assert _store(client).get(first["object_key"]) == CONTENT


def test_wrong_declaration_of_already_stored_content_dedupes(
    client: TestClient, fake_probe: VideoProbe
) -> None:
    """Assembled bytes matching a verified row byte-for-byte are a safe dedupe."""
    session_id = _create_session(client)
    first = _upload_all(client, session_id).json()["video"]

    wrong = sha256_hex(b"a-mistyped-checksum")
    response = _upload_all(client, session_id, filename="copy.mp4", declared_checksum=wrong)
    body = response.json()
    assert body["deduplicated"] is True
    assert body["video"]["id"] == first["id"]
    assert not _store(client).exists(f"sessions/{session_id}/C1/copy.mp4")
    assert len(_list_videos(client, session_id)) == 1


def test_claim_mismatch_flags_metadata_conflict(client: TestClient, fake_probe: VideoProbe) -> None:
    session_id = _create_session(client)
    response = _upload_all(
        client,
        session_id,
        claimed_fps=240.0,
        claimed_resolution="1280x720",
        claimed_duration_s=10.0,
    )
    video = response.json()["video"]
    assert video["status"] == "metadata_conflict"
    assert "fps: claimed 240.0, probed 120.0" in video["error"]
    assert "resolution: claimed 1280x720, probed 1920x1080" in video["error"]
    assert "duration_s: claimed 10.0, probed 60.0" in video["error"]
    assert video["probe"]["resolution"] == "1920x1080"  # probe stored for debugging


def test_claims_within_tolerance_pass(client: TestClient, fake_probe: VideoProbe) -> None:
    session_id = _create_session(client)
    response = _upload_all(
        client,
        session_id,
        claimed_fps=119.0,  # 0.84% off — within the 1% tolerance
        claimed_resolution="1920x1080",
        claimed_duration_s=59.0,  # 1.7% off — within the 2% tolerance
    )
    assert response.json()["video"]["status"] == "probed"


def test_probe_receives_streamed_file(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The object is staged to a local file for ffprobe — never loaded as bytes."""
    seen: dict[str, bytes] = {}

    def spy(source: Path) -> VideoProbe:
        seen["staged"] = Path(source).read_bytes()
        return PROBED

    monkeypatch.setattr(videos_router, "probe_video", spy)
    session_id = _create_session(client)
    assert _upload_all(client, session_id).json()["video"]["status"] == "probed"
    assert seen["staged"] == CONTENT


def test_undecodable_video_marked_failed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """US-B2 AC: a file the probe cannot decode is corrupted — FAILED, readable error."""

    def boom(_source: Path) -> VideoProbe:
        raise ProbeError("ffprobe failed (exit 1): moov atom not found")

    monkeypatch.setattr(videos_router, "probe_video", boom)
    session_id = _create_session(client)
    response = _upload_all(client, session_id)
    video = response.json()["video"]
    assert video["status"] == "failed"
    assert "corrupted video" in video["error"]
    assert "moov atom not found" in video["error"]
    assert "re-record or re-upload" in video["error"]
    assert _store(client).exists(video["object_key"])  # bytes kept for diagnosis

    assert [v["status"] for v in _list_videos(client, session_id)] == ["failed"]


def test_probe_unavailable_leaves_status_uploaded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ffprobe on the host is not a corrupted file — keep UPLOADED, retry later."""

    def down(_source: Path) -> VideoProbe:
        raise ProbeUnavailableError("ffprobe not found: install ffmpeg or set FFPROBE")

    monkeypatch.setattr(videos_router, "probe_video", down)
    session_id = _create_session(client)
    response = _upload_all(client, session_id)
    video = response.json()["video"]
    assert video["status"] == "uploaded"
    assert video["error"] == "probe unavailable: ffprobe not found: install ffmpeg or set FFPROBE"
    assert video["probe"] is None
    assert _store(client).get(video["object_key"]) == CONTENT  # bytes kept for re-probe


def test_late_upload_clears_degraded(client: TestClient, fake_probe: VideoProbe) -> None:
    """US-A3 seam: footage landing after stop clears missing_views/degraded."""
    session_id = _create_session(client)
    for camera_id in ("C1", "C2"):
        registered = client.post(
            "/cameras",
            json={
                "camera_id": camera_id,
                "position_label": f"{camera_id} test rig",
                "xyz_offset_m": {"x": 0.0, "y": 3.0, "z": 1.2},
                "height_m": 1.2,
                "fps": 120,
                "resolution": "1920x1080",
            },
            headers=auth(PARENT_TOKEN),
        )
        assert registered.status_code == 201
    start = client.post(
        f"/sessions/{session_id}/start",
        json={"cameras": ["C1", "C2"]},
        headers=auth(PARENT_TOKEN),
    )
    assert start.status_code == 200
    _upload_all(client, session_id)  # C1 footage only

    # Start validated and persisted the roster; stop recomputes from evidence.
    stop = client.post(
        f"/sessions/{session_id}/stop",
        json={},
        headers=auth(PARENT_TOKEN),
    ).json()
    assert stop["degraded"] is True
    assert stop["missing_views"] == ["C2"]

    late = _upload_all(
        client, session_id, content=OTHER_CONTENT, camera_id="C2", filename="side.mp4"
    )
    assert late.json()["video"]["status"] == "probed"

    lifecycle = client.get(f"/sessions/{session_id}/lifecycle", headers=auth(PARENT_TOKEN)).json()
    assert lifecycle["degraded"] is False
    assert lifecycle["missing_views"] == []


def test_late_upload_without_recorded_expectations_is_noop(
    client: TestClient, fake_probe: VideoProbe
) -> None:
    """No persisted camera expectations — the recompute must not invent any."""
    session_id = _create_session(client)
    with _db(client) as db:
        session = db.get(Session, uuid.UUID(session_id))
        assert session is not None
        session.state = SessionState.CAPTURED

    assert _upload_all(client, session_id).json()["video"]["status"] == "probed"
    lifecycle = client.get(f"/sessions/{session_id}/lifecycle", headers=auth(PARENT_TOKEN)).json()
    assert lifecycle["degraded"] is False
    assert lifecycle["missing_views"] == []


def test_part_upload_and_complete_after_completion_conflict(
    client: TestClient, fake_probe: VideoProbe
) -> None:
    session_id = _create_session(client)
    upload_id, store_upload_id = _begin(client, session_id, content=b"tiny")
    _put_part(client, upload_id, store_upload_id, 1, b"tiny")
    assert _complete(client, upload_id, store_upload_id, 1).status_code == 200

    late = _put_part(client, upload_id, store_upload_id, 2, b"late-part")
    assert late.status_code == 409
    again = _complete(client, upload_id, store_upload_id, 1)
    assert again.status_code == 409


def test_complete_with_missing_parts_conflicts(client: TestClient) -> None:
    session_id = _create_session(client)
    upload_id, store_upload_id = _begin(client, session_id)
    parts = _chunks(CONTENT)
    _put_part(client, upload_id, store_upload_id, 1, parts[0])
    _put_part(client, upload_id, store_upload_id, 3, parts[2])
    response = _complete(client, upload_id, store_upload_id, 3)
    assert response.status_code == 409
    assert "missing parts [2]" in response.json()["detail"]


def test_same_filename_different_content_conflicts(
    client: TestClient, fake_probe: VideoProbe
) -> None:
    session_id = _create_session(client)
    _upload_all(client, session_id)  # front.mp4 <- CONTENT, probed
    upload_id, store_upload_id = _begin(client, session_id, content=OTHER_CONTENT)
    _put_part(client, upload_id, store_upload_id, 1, OTHER_CONTENT)
    response = _complete(client, upload_id, store_upload_id, 1)
    assert response.status_code == 409
    assert "already uploaded" in response.json()["detail"]
    assert _store(client).get(f"sessions/{session_id}/C1/front.mp4") == CONTENT  # untouched

    # the conflict fired BEFORE staging was consumed: the upload remains resumable
    state = _get_status(client, upload_id, store_upload_id).json()
    assert state["completed"] is False
    assert [p["part_no"] for p in state["received_parts"]] == [1]


def test_unknown_ids_return_404(client: TestClient) -> None:
    ghost = str(uuid.uuid4())

    response = client.post(
        f"/sessions/{ghost}/videos/uploads", json=_upload_payload(), headers=auth(PARENT_TOKEN)
    )
    assert response.status_code == 404
    assert client.get(f"/sessions/{ghost}/videos", headers=auth(PARENT_TOKEN)).status_code == 404

    assert _put_part(client, ghost, "whatever", 1, b"x").status_code == 404
    assert _get_status(client, ghost, "whatever").status_code == 404
    assert _complete(client, ghost, "whatever", 1).status_code == 404


def test_rbac_player_forbidden_coach_allowed(client: TestClient) -> None:
    session_id = _create_session(client)
    upload_id, store_upload_id = _begin(client, session_id)

    forbidden = client.post(
        f"/sessions/{session_id}/videos/uploads",
        json=_upload_payload(),
        headers=auth(PLAYER_TOKEN),
    )
    assert forbidden.status_code == 403
    late_part = _put_part(client, upload_id, store_upload_id, 1, b"x", token=PLAYER_TOKEN)
    assert late_part.status_code == 403
    assert _get_status(client, upload_id, store_upload_id, token=PLAYER_TOKEN).status_code == 403
    assert _complete(client, upload_id, store_upload_id, 1, token=PLAYER_TOKEN).status_code == 403
    assert (
        client.get(f"/sessions/{session_id}/videos", headers=auth(PLAYER_TOKEN)).status_code == 403
    )
    assert client.get(f"/sessions/{session_id}/videos").status_code == 401  # no token

    coach = client.post(
        f"/sessions/{session_id}/videos/uploads",
        json=_upload_payload(filename="coach.mp4"),
        headers=auth(COACH_TOKEN),
    )
    assert coach.status_code == 201


@pytest.mark.parametrize("camera_id", ["balls", "c1", "C9", "C0", "C11", "CC1", ""])
def test_camera_id_must_match_registry(client: TestClient, camera_id: str) -> None:
    """Only registry cameras C1..C8 may own storage prefixes (retention tiers)."""
    session_id = _create_session(client)
    response = client.post(
        f"/sessions/{session_id}/videos/uploads",
        json=_upload_payload(camera_id=camera_id),
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 422


def test_invalid_upload_declarations_rejected(client: TestClient) -> None:
    session_id = _create_session(client)

    traversal = client.post(
        f"/sessions/{session_id}/videos/uploads",
        json=_upload_payload(filename="../escape.mp4"),
        headers=auth(PARENT_TOKEN),
    )
    assert traversal.status_code == 422
    assert "invalid upload target" in traversal.json()["detail"]

    bad_checksum = _upload_payload()
    bad_checksum["declared_checksum"] = str(bad_checksum["declared_checksum"]).upper()
    assert (
        client.post(
            f"/sessions/{session_id}/videos/uploads",
            json=bad_checksum,
            headers=auth(PARENT_TOKEN),
        ).status_code
        == 422
    )

    assert (
        client.post(
            f"/sessions/{session_id}/videos/uploads",
            json=_upload_payload(declared_size=0),
            headers=auth(PARENT_TOKEN),
        ).status_code
        == 422
    )


def test_part_upload_validation(client: TestClient) -> None:
    session_id = _create_session(client)
    upload_id, store_upload_id = _begin(client, session_id)
    assert _put_part(client, upload_id, store_upload_id, 0, b"x").status_code == 422
    assert _put_part(client, upload_id, store_upload_id, 1, b"").status_code == 422
