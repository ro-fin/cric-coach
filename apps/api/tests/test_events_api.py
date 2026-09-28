"""US-D4 acceptance: event corrections are versioned ground truth; numbering is stable.

Events are seeded directly in the DB (the detector is story d1's); every
correction path is asserted against both the API response and the
:class:`EventCorrection` rows it must leave behind. The numbering-stability
tests join downstream ``BallTag``/``BounceMark`` rows on ``(session_id,
ball_no)`` before and after corrections — the core US-D4 AC is that those
joins never orphan.
"""

import hashlib
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from cricai_data.enums import (
    BowlingVariation,
    Contact,
    EventSource,
    Footwork,
    Length,
    Line,
    MetricPhase,
    Outcome,
    Shot,
)
from cricai_data.models import (
    EVENT_DEPENDENT_TABLES,
    AuditLog,
    BallEvent,
    BallMetrics,
    BallTag,
    BallTrack,
    BounceEstimate,
    BounceMark,
    Clip,
    CoachNote,
    DeliveryLabel,
    EventCorrection,
    FrameSample,
    PoseTrack,
    ReferenceBall,
    Session,
)
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session as DbSession

from cricai_testing.apptest import (
    COACH_TOKEN,
    PARENT_TOKEN,
    PLAYER_TOKEN,
    auth,
    make_sqlite_engine,
    make_test_app,
)

UNKNOWN_SESSION = "00000000-0000-0000-0000-000000000000"
UNKNOWN_EVENT = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def engine() -> Engine:
    return make_sqlite_engine()


@pytest.fixture
def client(engine: Engine, tmp_path: Path) -> TestClient:
    return TestClient(make_test_app(tmp_path, engine=engine))


def _create_session(client: TestClient, *, is_guest: bool = False) -> str:
    player = client.post(
        "/players",
        json={
            "name": "Visitor" if is_guest else "Arjun",
            "birthdate": "2014-11-20",
            "handedness": "right",
            "is_guest": is_guest,
        },
        headers=auth(PARENT_TOKEN),
    ).json()
    session = client.post(
        "/sessions",
        json={
            "player_id": player["id"],
            "date": "2026-07-07",
            "session_type": "batting",
            "bowler_source": "coach",
        },
        headers=auth(PARENT_TOKEN),
    ).json()
    session_id: str = session["id"]
    return session_id


def _seed_event(
    engine: Engine,
    session_id: str,
    ball_no: int,
    *,
    confidence: float = 0.8,
    source: EventSource = EventSource.AUTO,
    valid: bool = True,
    detector_version: str = "det-1",
    with_contact: bool = True,
) -> str:
    """Insert one event with timings derived from ball_no (base = ball_no * 10_000)."""
    base = ball_no * 10_000
    with DbSession(engine) as db:
        event = BallEvent(
            session_id=uuid.UUID(session_id),
            ball_no=ball_no,
            start_ms=base,
            release_ms=base + 500,
            contact_ms=base + 900 if with_contact else None,
            end_ms=base + 2000,
            confidence=confidence,
            source=source,
            valid=valid,
            detector_version=detector_version,
        )
        db.add(event)
        db.commit()
        return str(event.id)


def _seed_tag(engine: Engine, session_id: str, ball_no: int) -> None:
    with DbSession(engine) as db:
        db.add(
            BallTag(
                session_id=uuid.UUID(session_id),
                ball_no=ball_no,
                line=Line.OFF,
                length=Length.GOOD,
                shot=Shot.DRIVE,
                footwork=Footwork.FRONT,
                contact=Contact.MIDDLE,
                outcome=Outcome.CONTROLLED_GROUND_SHOT,
                control=True,
                source="manual",
                ground_truth_eligible=True,
                created_by="parent",
            )
        )
        db.commit()


def _seed_bounce_mark(engine: Engine, session_id: str, ball_no: int) -> None:
    with DbSession(engine) as db:
        db.add(
            BounceMark(
                session_id=uuid.UUID(session_id),
                ball_no=ball_no,
                camera_id="C3",
                frame_no=42,
                px_x=600.0,
                px_y=172.5,
                pitch_x=6.0,
                pitch_y=0.2,
            )
        )
        db.commit()


def _seed_clip(engine: Engine, session_id: str, ball_no: int) -> None:
    with DbSession(engine) as db:
        db.add(
            Clip(
                session_id=uuid.UUID(session_id),
                ball_no=ball_no,
                camera_id="C1",
                start_ms=0,
                end_ms=3000,
            )
        )
        db.commit()


def _seed_pose_track(engine: Engine, session_id: str, ball_no: int) -> None:
    with DbSession(engine) as db:
        db.add(
            PoseTrack(
                session_id=uuid.UUID(session_id),
                ball_no=ball_no,
                camera_id="C1",
                model_name="fake-pose",
                model_version="1",
                landmarks_key=f"sessions/{session_id}/balls/{ball_no}/pose-C1.json",
                frame_count=120,
                availability=0.99,
                subject_confidence=0.97,
            )
        )
        db.commit()


def _seed_ball_metrics(engine: Engine, session_id: str, ball_no: int) -> None:
    with DbSession(engine) as db:
        db.add(
            BallMetrics(
                session_id=uuid.UUID(session_id),
                ball_no=ball_no,
                phase=MetricPhase.PRE_RELEASE,
                metrics={},
            )
        )
        db.commit()


def _seed_reference_ball(engine: Engine, session_id: str, ball_no: int) -> None:
    with DbSession(engine) as db:
        db.add(
            ReferenceBall(
                session_id=uuid.UUID(session_id),
                ball_no=ball_no,
                label="model cover drive",
                marked_by="coach",
            )
        )
        db.commit()


def _seed_ball_track(engine: Engine, session_id: str, ball_no: int) -> None:
    with DbSession(engine) as db:
        db.add(
            BallTrack(
                session_id=uuid.UUID(session_id),
                ball_no=ball_no,
                camera_id="C1",
                tracker_version="kalman-1.0.0",
                points_key=f"sessions/{session_id}/balls/{ball_no}/track-C1.json",
                coverage=0.95,
                confidence=0.9,
            )
        )
        db.commit()


def _seed_bounce_estimate(engine: Engine, session_id: str, ball_no: int) -> None:
    with DbSession(engine) as db:
        db.add(
            BounceEstimate(
                session_id=uuid.UUID(session_id),
                ball_no=ball_no,
                pitch_x=6.0,
                pitch_y=0.2,
                confidence=0.8,
                tracker_version="kalman-1.0.0",
            )
        )
        db.commit()


def _seed_frame_sample(engine: Engine, session_id: str, ball_no: int) -> None:
    with DbSession(engine) as db:
        db.add(
            FrameSample(
                session_id=uuid.UUID(session_id),
                ball_no=ball_no,
                camera_id="C1",
                frame_no=ball_no * 10,
                ts_ms=ball_no * 1000,
                object_key=f"sessions/{session_id}/balls/{ball_no}/frame-C1.jpg",
                stratum={},
                sampler_version="frame-sampler-1.0.0",
            )
        )
        db.commit()


def _seed_delivery_label(engine: Engine, session_id: str, ball_no: int) -> None:
    with DbSession(engine) as db:
        db.add(
            DeliveryLabel(
                session_id=uuid.UUID(session_id),
                ball_no=ball_no,
                variation_intent=BowlingVariation.LEG_BREAK,
                labeler="coach",
            )
        )
        db.commit()


def _seed_coach_note(engine: Engine, session_id: str, ball_no: int) -> None:
    with DbSession(engine) as db:
        player_id = db.scalar(select(Session.player_id).where(Session.id == uuid.UUID(session_id)))
        db.add(
            CoachNote(
                player_id=player_id,
                session_id=uuid.UUID(session_id),
                ball_no=ball_no,
                body="watch the seam position on this one",
                author="coach",
            )
        )
        db.commit()


#: Keyed by the canonical ``EVENT_DEPENDENT_TABLES`` labels. The 409 guard test
#: parametrizes over the canonical list itself, so a table added there without
#: a seeder here fails loudly (KeyError) instead of silently going untested.
DEPENDENT_SEEDERS: dict[str, Callable[[Engine, str, int], None]] = {
    "ball tags": _seed_tag,
    "bounce marks": _seed_bounce_mark,
    "clips": _seed_clip,
    "pose tracks": _seed_pose_track,
    "ball metrics": _seed_ball_metrics,
    "reference balls": _seed_reference_ball,
    "ball tracks": _seed_ball_track,
    "bounce estimates": _seed_bounce_estimate,
    "frame samples": _seed_frame_sample,
    "delivery labels": _seed_delivery_label,
    "coach notes": _seed_coach_note,
}


def _events(
    client: TestClient, session_id: str, *, token: str = PARENT_TOKEN, **params: Any
) -> list[dict[str, Any]]:
    response = client.get(f"/sessions/{session_id}/events", params=params, headers=auth(token))
    assert response.status_code == 200
    events: list[dict[str, Any]] = response.json()
    return events


def _accept(
    client: TestClient, session_id: str, event_id: str, *, token: str = PARENT_TOKEN
) -> httpx.Response:
    return client.post(f"/sessions/{session_id}/events/{event_id}/accept", headers=auth(token))


def _adjust(
    client: TestClient,
    session_id: str,
    event_id: str,
    payload: dict[str, Any],
    *,
    token: str = PARENT_TOKEN,
) -> httpx.Response:
    return client.post(
        f"/sessions/{session_id}/events/{event_id}/adjust", json=payload, headers=auth(token)
    )


def _reject(
    client: TestClient, session_id: str, event_id: str, *, token: str = PARENT_TOKEN
) -> httpx.Response:
    return client.post(f"/sessions/{session_id}/events/{event_id}/reject", headers=auth(token))


def _add(
    client: TestClient, session_id: str, payload: dict[str, Any], *, token: str = PARENT_TOKEN
) -> httpx.Response:
    return client.post(f"/sessions/{session_id}/events/add", json=payload, headers=auth(token))


def _add_payload(position: int, *, base: int = 100_000) -> dict[str, Any]:
    return {
        "start_ms": base,
        "release_ms": base + 500,
        "contact_ms": base + 900,
        "end_ms": base + 2000,
        "position": position,
    }


def _bulk_accept(
    client: TestClient, session_id: str, min_confidence: float, *, token: str = PARENT_TOKEN
) -> httpx.Response:
    return client.post(
        f"/sessions/{session_id}/events/bulk-accept",
        json={"min_confidence": min_confidence},
        headers=auth(token),
    )


def _corrections(engine: Engine, event_id: str) -> list[EventCorrection]:
    with DbSession(engine) as db:
        return list(
            db.scalars(
                select(EventCorrection)
                .where(EventCorrection.event_id == uuid.UUID(event_id))
                .order_by(EventCorrection.at)
            ).all()
        )


def _db_ball_numbers(engine: Engine, session_id: str) -> dict[int, str]:
    """(ball_no -> event id) for every event of the session, valid or not."""
    with DbSession(engine) as db:
        events = db.scalars(
            select(BallEvent).where(BallEvent.session_id == uuid.UUID(session_id))
        ).all()
        return {event.ball_no: str(event.id) for event in events}


# ---------------------------------------------------------------- list & filters


def test_list_defaults_to_valid_events_ordered_by_ball_no(
    client: TestClient, engine: Engine
) -> None:
    session_id = _create_session(client)
    _seed_event(engine, session_id, 2, confidence=0.9)
    _seed_event(engine, session_id, 1, confidence=0.6)
    _seed_event(engine, session_id, 3, valid=False)  # rejected: hidden by default

    events = _events(client, session_id)
    assert [e["ball_no"] for e in events] == [1, 2]
    first = events[0]
    assert first["start_ms"] == 10_000
    assert first["release_ms"] == 10_500
    assert first["contact_ms"] == 10_900
    assert first["end_ms"] == 12_000
    assert first["confidence"] == pytest.approx(0.6)
    assert first["source"] == "auto"
    assert first["detector_version"] == "det-1"
    assert first["valid"] is True


def test_list_filters_source_valid_and_min_confidence(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    _seed_event(engine, session_id, 1, confidence=0.5)
    _seed_event(engine, session_id, 2, confidence=0.95)
    _seed_event(engine, session_id, 3, source=EventSource.MANUAL, confidence=1.0)
    _seed_event(engine, session_id, 4, valid=False, confidence=0.99)

    assert [e["ball_no"] for e in _events(client, session_id, source="auto")] == [1, 2]
    assert [e["ball_no"] for e in _events(client, session_id, min_confidence=0.9)] == [2, 3]
    assert [e["ball_no"] for e in _events(client, session_id, valid=False)] == [4]
    assert [e["ball_no"] for e in _events(client, session_id, valid=True)] == [1, 2, 3]
    combined = _events(client, session_id, source="auto", min_confidence=0.9)
    assert [e["ball_no"] for e in combined] == [2]


# ---------------------------------------------------------------------- accept


def test_accept_flips_source_and_writes_snapshot_correction(
    client: TestClient, engine: Engine
) -> None:
    session_id = _create_session(client)
    event_id = _seed_event(engine, session_id, 1, confidence=0.7)

    response = _accept(client, session_id, event_id, token=COACH_TOKEN)
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "corrected"
    assert body["ball_no"] == 1  # numbering never mutates on accept
    assert body["confidence"] == pytest.approx(0.7)

    (correction,) = _corrections(engine, event_id)
    assert correction.action == "accept"
    assert correction.actor == "coach"
    assert correction.before is not None
    assert correction.after is not None
    assert correction.before["source"] == "auto"
    assert correction.after["source"] == "corrected"
    assert correction.before["ball_no"] == correction.after["ball_no"] == 1


def test_accept_non_auto_event_is_conflict(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    manual_id = _seed_event(engine, session_id, 1, source=EventSource.MANUAL)
    auto_id = _seed_event(engine, session_id, 2)

    manual = _accept(client, session_id, manual_id)
    assert manual.status_code == 409
    assert "already 'manual' ground truth" in manual.json()["detail"]

    assert _accept(client, session_id, auto_id).status_code == 200
    again = _accept(client, session_id, auto_id)  # now corrected: accepting again conflicts
    assert again.status_code == 409
    assert "'corrected'" in again.json()["detail"]
    assert len(_corrections(engine, auto_id)) == 1  # the conflict wrote nothing


def test_accept_rejected_event_is_conflict(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    event_id = _seed_event(engine, session_id, 1, valid=False)
    response = _accept(client, session_id, event_id)
    assert response.status_code == 409
    assert "rejected" in response.json()["detail"]


# ---------------------------------------------------------------------- adjust


def test_adjust_updates_fields_with_diff_only_correction(
    client: TestClient, engine: Engine
) -> None:
    session_id = _create_session(client)
    event_id = _seed_event(engine, session_id, 1)  # start 10000, release 10500, contact 10900

    response = _adjust(client, session_id, event_id, {"start_ms": 9800, "contact_ms": 10950})
    assert response.status_code == 200
    body = response.json()
    assert body["start_ms"] == 9800
    assert body["contact_ms"] == 10950
    assert body["release_ms"] == 10_500  # untouched fields keep stored values
    assert body["end_ms"] == 12_000
    assert body["source"] == "corrected"
    assert body["ball_no"] == 1

    (correction,) = _corrections(engine, event_id)
    assert correction.action == "adjust"
    assert correction.actor == "parent"
    # Diffs only: untouched fields never appear in the correction row.
    assert correction.before == {"start_ms": 10_000, "contact_ms": 10_900}
    assert correction.after == {"start_ms": 9800, "contact_ms": 10_950}


def test_adjust_contact_explicit_null_clears_contact(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    event_id = _seed_event(engine, session_id, 1)

    response = _adjust(client, session_id, event_id, {"contact_ms": None})
    assert response.status_code == 200
    assert response.json()["contact_ms"] is None

    (correction,) = _corrections(engine, event_id)
    assert correction.before == {"contact_ms": 10_900}
    assert correction.after == {"contact_ms": None}


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"start_ms": 10_600}, "start_ms (10600) > release_ms (10500)"),
        ({"release_ms": 11_000}, "release_ms (11000) > contact_ms (10900)"),
        ({"contact_ms": 12_500}, "contact_ms (12500) > end_ms (12000)"),
        ({"contact_ms": None, "release_ms": 12_500}, "release_ms (12500) > end_ms (12000)"),
    ],
)
def test_adjust_ordering_violations_are_422(
    client: TestClient, engine: Engine, payload: dict[str, Any], message: str
) -> None:
    session_id = _create_session(client)
    event_id = _seed_event(engine, session_id, 1)

    response = _adjust(client, session_id, event_id, payload)
    assert response.status_code == 422
    assert message in response.json()["detail"]
    assert _corrections(engine, event_id) == []
    assert _events(client, session_id)[0]["start_ms"] == 10_000  # nothing persisted


def test_adjust_null_required_field_is_422(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    event_id = _seed_event(engine, session_id, 1)
    response = _adjust(client, session_id, event_id, {"end_ms": None})
    assert response.status_code == 422
    assert "end_ms cannot be null" in response.json()["detail"]


def test_adjust_without_changes_is_a_noop(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    event_id = _seed_event(engine, session_id, 1)

    response = _adjust(client, session_id, event_id, {"start_ms": 10_000})  # same value
    assert response.status_code == 200
    assert response.json()["source"] == "auto"  # provenance untouched: nothing was corrected
    assert _corrections(engine, event_id) == []


def test_adjust_rejected_event_is_conflict(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    event_id = _seed_event(engine, session_id, 1, valid=False)
    response = _adjust(client, session_id, event_id, {"start_ms": 9000})
    assert response.status_code == 409
    assert "rejected" in response.json()["detail"]


# ---------------------------------------------------------------------- reject


def test_reject_marks_invalid_and_keeps_ball_no(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    event_id = _seed_event(engine, session_id, 2)
    _seed_event(engine, session_id, 1)

    response = _reject(client, session_id, event_id, token=COACH_TOKEN)
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert body["ball_no"] == 2  # rejected events keep their number
    assert body["source"] == "auto"  # provenance preserved; validity is the correction

    assert [e["ball_no"] for e in _events(client, session_id)] == [1]
    assert [e["ball_no"] for e in _events(client, session_id, valid=False)] == [2]

    (correction,) = _corrections(engine, event_id)
    assert correction.action == "reject"
    assert correction.actor == "coach"
    assert correction.before is not None
    assert correction.after is not None
    assert correction.before["valid"] is True
    assert correction.after["valid"] is False


def test_reject_already_rejected_is_conflict(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    event_id = _seed_event(engine, session_id, 1, valid=False)
    response = _reject(client, session_id, event_id)
    assert response.status_code == 409
    assert "rejected" in response.json()["detail"]
    assert _corrections(engine, event_id) == []


# ------------------------------------------------------------------------- add


def test_add_into_gap_uses_next_integer(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    _seed_event(engine, session_id, 1)
    _seed_event(engine, session_id, 2)
    _seed_event(engine, session_id, 4)

    response = _add(client, session_id, _add_payload(2), token=COACH_TOKEN)
    assert response.status_code == 201
    body = response.json()
    assert body["ball_no"] == 3  # the gap absorbs the insert: nothing renumbered
    assert body["source"] == "manual"
    assert body["confidence"] == pytest.approx(1.0)
    assert body["valid"] is True
    assert body["start_ms"] == 100_000

    assert [e["ball_no"] for e in _events(client, session_id)] == [1, 2, 3, 4]
    (correction,) = _corrections(engine, body["id"])
    assert correction.action == "add"
    assert correction.actor == "coach"
    assert correction.before is None
    assert correction.after is not None
    assert correction.after["ball_no"] == 3
    assert correction.after["source"] == "manual"


def test_add_at_position_zero_and_beyond_last_ball(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    first = _add(client, session_id, _add_payload(0))
    assert first.status_code == 201
    assert first.json()["ball_no"] == 1

    appended = _add(client, session_id, _add_payload(9, base=200_000))
    assert appended.status_code == 201
    assert appended.json()["ball_no"] == 10  # permissive: position need not exist

    contactless = _add(
        client,
        session_id,
        {"start_ms": 300_000, "release_ms": 300_500, "end_ms": 302_000, "position": 10},
    )
    assert contactless.status_code == 201
    assert contactless.json()["contact_ms"] is None


def test_add_dense_renumbers_dependency_free_tail(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    _seed_event(engine, session_id, 1)
    ball2_id = _seed_event(engine, session_id, 2)
    ball3_id = _seed_event(engine, session_id, 3)

    response = _add(client, session_id, _add_payload(1))
    assert response.status_code == 201
    assert response.json()["ball_no"] == 2

    events = _events(client, session_id)
    assert [e["ball_no"] for e in events] == [1, 2, 3, 4]
    by_id = {e["id"]: e for e in events}
    assert by_id[ball2_id]["ball_no"] == 3  # shifted up by one
    assert by_id[ball3_id]["ball_no"] == 4
    # Shifted rows are untouched auto output: no correction rows, provenance kept.
    assert _corrections(engine, ball2_id) == []
    assert _corrections(engine, ball3_id) == []
    assert by_id[ball2_id]["source"] == "auto"


def test_add_shift_stops_at_first_gap(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    _seed_event(engine, session_id, 1)
    ball2_id = _seed_event(engine, session_id, 2)
    ball3_id = _seed_event(engine, session_id, 3)
    ball5_id = _seed_event(engine, session_id, 5)

    response = _add(client, session_id, _add_payload(1))
    assert response.status_code == 201
    assert response.json()["ball_no"] == 2

    numbers = _db_ball_numbers(engine, session_id)
    assert numbers[3] == ball2_id
    assert numbers[4] == ball3_id
    assert numbers[5] == ball5_id  # beyond the gap: untouched


@pytest.mark.parametrize("table_label", sorted(label for _, label in EVENT_DEPENDENT_TABLES))
def test_add_dense_with_dependent_rows_is_409(
    client: TestClient, engine: Engine, table_label: str
) -> None:
    session_id = _create_session(client)
    _seed_event(engine, session_id, 1)
    _seed_event(engine, session_id, 2)
    DEPENDENT_SEEDERS[table_label](engine, session_id, 2)

    response = _add(client, session_id, _add_payload(1))
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "cannot renumber ball 2" in detail
    assert table_label in detail
    assert "orphan" in detail
    # All-or-nothing: numbering unchanged, no manual event was inserted.
    events = _events(client, session_id)
    assert [e["ball_no"] for e in events] == [1, 2]
    assert [e["source"] for e in events] == ["auto", "auto"]


@pytest.mark.parametrize(
    ("source", "valid", "expected_word"),
    [
        (EventSource.MANUAL, True, "manual"),
        (EventSource.CORRECTED, True, "corrected"),
        (EventSource.AUTO, False, "rejected"),
    ],
)
def test_add_dense_with_pinned_event_in_run_is_409(
    client: TestClient,
    engine: Engine,
    source: EventSource,
    valid: bool,
    expected_word: str,
) -> None:
    session_id = _create_session(client)
    _seed_event(engine, session_id, 1)
    _seed_event(engine, session_id, 2)
    _seed_event(engine, session_id, 3, source=source, valid=valid)

    response = _add(client, session_id, _add_payload(1))
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "cannot renumber ball 3" in detail
    assert expected_word in detail
    assert sorted(_db_ball_numbers(engine, session_id)) == [1, 2, 3]  # untouched


def test_add_ordering_violation_is_422(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    payload = _add_payload(0)
    payload["release_ms"] = payload["start_ms"] - 1
    response = _add(client, session_id, payload)
    assert response.status_code == 422
    assert "start_ms" in response.json()["detail"]
    assert _events(client, session_id) == []


def test_timing_beyond_int32_is_422_at_the_wire(client: TestClient, engine: Engine) -> None:
    """BallEvent timing/number columns are int32 on PostgreSQL: an overflowing
    int must be a 422 at the schema, never a backend ``DataError`` 500 (the
    SQLite test backend would silently accept it, so only the bound gates it).
    """
    session_id = _create_session(client)
    event_id = _seed_event(engine, session_id, 1)
    over = 2**31  # first value the int32 column cannot store

    assert _adjust(client, session_id, event_id, {"start_ms": over}).status_code == 422
    assert _corrections(engine, event_id) == []  # rejected before any write

    payload = _add_payload(0)
    payload["end_ms"] = over  # ordering-valid, so only the upper bound can reject it
    assert _add(client, session_id, payload).status_code == 422
    # position + 1 becomes ball_no, so position is bounded one below INT32 max.
    assert _add(client, session_id, {**_add_payload(0), "position": over - 1}).status_code == 422
    assert len(_events(client, session_id)) == 1  # only the seeded event: nothing inserted


# ----------------------------------------------------------------- bulk-accept


def test_bulk_accept_threshold_and_idempotency(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    high_id = _seed_event(engine, session_id, 1, confidence=0.95)
    edge_id = _seed_event(engine, session_id, 2, confidence=0.9)
    low_id = _seed_event(engine, session_id, 3, confidence=0.5)
    manual_id = _seed_event(engine, session_id, 4, source=EventSource.MANUAL, confidence=1.0)
    rejected_id = _seed_event(engine, session_id, 5, confidence=0.99, valid=False)

    response = _bulk_accept(client, session_id, 0.9, token=COACH_TOKEN)
    assert response.status_code == 200
    assert response.json() == {"accepted": 2}  # threshold is inclusive

    events = {e["id"]: e for e in _events(client, session_id, valid=True)}
    assert events[high_id]["source"] == "corrected"
    assert events[edge_id]["source"] == "corrected"
    assert events[low_id]["source"] == "auto"  # below threshold: untouched
    assert events[manual_id]["source"] == "manual"  # never bulk-flipped
    assert _events(client, session_id, valid=False)[0]["id"] == rejected_id

    for event_id in (high_id, edge_id):
        (correction,) = _corrections(engine, event_id)
        assert correction.action == "accept"
        assert correction.actor == "coach"
    assert _corrections(engine, low_id) == []
    assert _corrections(engine, rejected_id) == []

    # Accepted events are no longer source=auto: a re-run accepts nothing new.
    assert _bulk_accept(client, session_id, 0.9).json() == {"accepted": 0}
    assert len(_corrections(engine, high_id)) == 1


# ------------------------------------------------------- numbering stability


def test_corrections_never_orphan_downstream_joins(client: TestClient, engine: Engine) -> None:
    """Core US-D4 AC: tags/marks keyed by (session_id, ball_no) survive corrections."""
    session_id = _create_session(client)
    accepted_id = _seed_event(engine, session_id, 1)
    adjusted_id = _seed_event(engine, session_id, 2)
    rejected_id = _seed_event(engine, session_id, 3)
    _seed_tag(engine, session_id, 2)
    _seed_bounce_mark(engine, session_id, 2)
    _seed_tag(engine, session_id, 3)

    assert _accept(client, session_id, accepted_id).status_code == 200
    assert _adjust(client, session_id, adjusted_id, {"start_ms": 19_000}).status_code == 200
    assert _reject(client, session_id, rejected_id).status_code == 200

    numbers = _db_ball_numbers(engine, session_id)
    assert numbers == {1: accepted_id, 2: adjusted_id, 3: rejected_id}

    with DbSession(engine) as db:
        sid = uuid.UUID(session_id)
        for ball_no in (2, 3):
            tag = db.scalar(
                select(BallTag).where(BallTag.session_id == sid, BallTag.ball_no == ball_no)
            )
            event = db.scalar(
                select(BallEvent).where(BallEvent.session_id == sid, BallEvent.ball_no == ball_no)
            )
            assert tag is not None
            assert event is not None  # the join still resolves, even for the rejected ball
        mark = db.scalar(
            select(BounceMark).where(BounceMark.session_id == sid, BounceMark.ball_no == 2)
        )
        assert mark is not None
        assert (
            str(
                db.scalar(
                    select(BallEvent.id).where(
                        BallEvent.session_id == sid, BallEvent.ball_no == mark.ball_no
                    )
                )
            )
            == adjusted_id
        )


# ---------------------------------------------------------------------- export


def test_export_returns_history_and_writes_audit(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    accepted_id = _seed_event(engine, session_id, 1, confidence=0.9, detector_version="det-7")
    rejected_id = _seed_event(engine, session_id, 2)
    _accept(client, session_id, accepted_id)
    _adjust(client, session_id, accepted_id, {"end_ms": 12_500}, token=COACH_TOKEN)
    _reject(client, session_id, rejected_id)
    added = _add(client, session_id, _add_payload(2)).json()

    response = client.get(f"/sessions/{session_id}/events/export", headers=auth(COACH_TOKEN))
    assert response.status_code == 200
    rows = response.json()
    # Rejected ball 2 is exported too: a human-labeled false positive is the
    # detector's negative training signal (US-D4), flagged via ``valid``.
    assert [row["ball_no"] for row in rows] == [1, 2, 3]
    assert [row["valid"] for row in rows] == [True, False, True]

    ball1 = rows[0]
    assert ball1["source"] == "corrected"
    assert ball1["detector_version"] == "det-7"  # provenance for training/eval splits
    assert ball1["end_ms"] == 12_500
    assert ball1["confidence"] == pytest.approx(0.9)
    assert [c["action"] for c in ball1["corrections"]] == ["accept", "adjust"]
    accept_row, adjust_row = ball1["corrections"]
    assert accept_row["actor"] == "parent"
    assert accept_row["before"]["source"] == "auto"
    assert adjust_row["actor"] == "coach"
    assert adjust_row["before"] == {"end_ms": 12_000}
    assert adjust_row["after"] == {"end_ms": 12_500}
    assert isinstance(adjust_row["at"], str)

    ball2 = rows[1]  # round trip: reject -> export -> reconstructable negative label
    assert ball2["valid"] is False
    assert ball2["source"] == "auto"  # provenance kept; the rejection is the correction
    assert ball2["start_ms"] == 20_000  # timings survive for eval windows
    assert [c["action"] for c in ball2["corrections"]] == ["reject"]
    (reject_row,) = ball2["corrections"]
    assert reject_row["actor"] == "parent"
    assert reject_row["before"]["valid"] is True
    assert reject_row["after"]["valid"] is False

    ball3 = rows[2]
    assert ball3["source"] == "manual"
    assert ball3["detector_version"] == ""
    assert [c["action"] for c in ball3["corrections"]] == ["add"]
    assert ball3["corrections"][0]["after"]["ball_no"] == added["ball_no"]

    with DbSession(engine) as db:
        (audit,) = db.scalars(select(AuditLog).where(AuditLog.action == "share_export")).all()
        assert audit.actor == "coach"
        assert audit.entity == "session"
        assert audit.entity_id == session_id
        assert audit.detail is not None
        assert audit.detail["format"] == "json"
        assert audit.detail["surface"] == "events_export"
        assert audit.detail["sha256"] == hashlib.sha256(response.content).hexdigest()


def test_export_unknown_format_is_422(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    response = client.get(
        f"/sessions/{session_id}/events/export",
        params={"format": "csv"},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 422


# ------------------------------------------------------------------ RBAC & 404s


def test_player_role_cannot_write_or_export(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    event_id = _seed_event(engine, session_id, 1)

    assert _accept(client, session_id, event_id, token=PLAYER_TOKEN).status_code == 403
    assert (
        _adjust(client, session_id, event_id, {"start_ms": 1}, token=PLAYER_TOKEN).status_code
        == 403
    )
    assert _reject(client, session_id, event_id, token=PLAYER_TOKEN).status_code == 403
    assert _add(client, session_id, _add_payload(1), token=PLAYER_TOKEN).status_code == 403
    assert _bulk_accept(client, session_id, 0.9, token=PLAYER_TOKEN).status_code == 403
    export = client.get(f"/sessions/{session_id}/events/export", headers=auth(PLAYER_TOKEN))
    assert export.status_code == 403
    assert _events(client, session_id)[0]["source"] == "auto"  # nothing leaked through


def test_player_reads_guest_session_as_404(client: TestClient, engine: Engine) -> None:
    guest_session = _create_session(client, is_guest=True)
    family_session = _create_session(client)
    _seed_event(engine, guest_session, 1)

    hidden = client.get(f"/sessions/{guest_session}/events", headers=auth(PLAYER_TOKEN))
    assert hidden.status_code == 404  # US-L3: existence hidden from players

    assert _events(client, family_session, token=PLAYER_TOKEN) == []
    for token in (PARENT_TOKEN, COACH_TOKEN):
        assert len(_events(client, guest_session, token=token)) == 1


def test_unknown_session_is_404_everywhere(client: TestClient, engine: Engine) -> None:
    assert (
        client.get(f"/sessions/{UNKNOWN_SESSION}/events", headers=auth(PARENT_TOKEN)).status_code
        == 404
    )
    assert _accept(client, UNKNOWN_SESSION, UNKNOWN_EVENT).status_code == 404
    assert _adjust(client, UNKNOWN_SESSION, UNKNOWN_EVENT, {"start_ms": 1}).status_code == 404
    assert _reject(client, UNKNOWN_SESSION, UNKNOWN_EVENT).status_code == 404
    assert _add(client, UNKNOWN_SESSION, _add_payload(0)).status_code == 404
    assert _bulk_accept(client, UNKNOWN_SESSION, 0.5).status_code == 404
    assert (
        client.get(
            f"/sessions/{UNKNOWN_SESSION}/events/export", headers=auth(PARENT_TOKEN)
        ).status_code
        == 404
    )


def test_event_of_other_session_is_404(client: TestClient, engine: Engine) -> None:
    session_a = _create_session(client)
    session_b = _create_session(client)
    foreign_id = _seed_event(engine, session_a, 1)

    assert _accept(client, session_b, foreign_id).status_code == 404
    assert _adjust(client, session_b, foreign_id, {"start_ms": 1}).status_code == 404
    assert _reject(client, session_b, foreign_id).status_code == 404
    assert _accept(client, session_a, UNKNOWN_EVENT).status_code == 404
    assert _corrections(engine, foreign_id) == []
