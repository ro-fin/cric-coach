"""US-I6 delivery-label API: guardian-only intent CRUD, worker-only detection.

Pins the seam split: every endpoint here writes manual intent only —
``variation_detected`` is untouchable through HTTP (the classifier worker job
is its only writer) — and the export/import round trip is audit-logged and
lossless through the labelio delivery-task channel.
"""

import uuid
from typing import Any

import pytest
from cricai_api.routers import labels as labels_module
from cricai_data.enums import BowlingVariation
from cricai_data.models import AuditLog, DeliveryLabel
from fastapi.testclient import TestClient
from sqlalchemy import select

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, PLAYER_TOKEN, auth

UNKNOWN_ID = str(uuid.uuid4())


def _create_session(client: TestClient, *, is_guest: bool = False) -> str:
    player = client.post(
        "/players",
        json={
            "name": "Visitor" if is_guest else "Arjun",
            "birthdate": "2014-11-20",
            "is_guest": is_guest,
        },
        headers=auth(PARENT_TOKEN),
    ).json()
    response = client.post(
        "/sessions",
        json={
            "player_id": player["id"],
            "date": "2026-07-10",
            "session_type": "bowling",
            "bowler_source": "human",
        },
        headers=auth(PARENT_TOKEN),
    )
    session_id: str = response.json()["id"]
    return session_id


def _url(session_id: str) -> str:
    return f"/sessions/{session_id}/labels"


def _seed_row(
    client: TestClient,
    session_id: str,
    ball_no: int,
    *,
    intent: str = "leg_break",
    detected: str | None = None,
    source: str = "manual",
    labeler: str = "coach",
) -> None:
    """Seed a row directly (the worker seam / a model pre-label, not the API)."""
    factory = client.app.state.session_factory  # type: ignore[union-attr]
    with factory() as db:
        db.add(
            DeliveryLabel(
                session_id=uuid.UUID(session_id),
                ball_no=ball_no,
                variation_intent=BowlingVariation(intent),
                variation_detected=None if detected is None else BowlingVariation(detected),
                labeler=labeler,
                source=source,
            )
        )
        db.commit()


def _row(client: TestClient, session_id: str, ball_no: int) -> dict[str, Any]:
    factory = client.app.state.session_factory  # type: ignore[union-attr]
    with factory() as db:
        label = db.scalars(
            select(DeliveryLabel).where(
                DeliveryLabel.session_id == uuid.UUID(session_id),
                DeliveryLabel.ball_no == ball_no,
            )
        ).one()
        return {
            "variation_intent": label.variation_intent.value,
            "variation_detected": (
                None if label.variation_detected is None else label.variation_detected.value
            ),
            "labeler": label.labeler,
            "source": label.source,
        }


def _edit_audits(client: TestClient) -> list[dict[str, Any] | None]:
    factory = client.app.state.session_factory  # type: ignore[union-attr]
    with factory() as db:
        rows = db.scalars(select(AuditLog).where(AuditLog.action == "delivery_label_edit")).all()
        return [row.detail for row in rows]


# --- create ------------------------------------------------------------------------


def test_create_is_guardian_manual_ground_truth(client: TestClient) -> None:
    session_id = _create_session(client)
    response = client.post(
        _url(session_id),
        json={"ball_no": 1, "variation_intent": "googly"},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 201
    assert response.json() == {
        "ball_no": 1,
        "variation_intent": "googly",
        "variation_detected": None,
        "labeler": "parent",
        "source": "manual",
    }
    coached = client.post(
        _url(session_id),
        json={"ball_no": 2, "variation_intent": "leg_break"},
        headers=auth(COACH_TOKEN),
    )
    assert coached.json()["labeler"] == "coach"


def test_create_rejects_player_role(client: TestClient) -> None:
    session_id = _create_session(client)
    response = client.post(
        _url(session_id),
        json={"ball_no": 1, "variation_intent": "googly"},
        headers=auth(PLAYER_TOKEN),
    )
    assert response.status_code == 403


def test_create_404_and_409_and_validation(client: TestClient) -> None:
    assert (
        client.post(
            _url(UNKNOWN_ID),
            json={"ball_no": 1, "variation_intent": "googly"},
            headers=auth(PARENT_TOKEN),
        ).status_code
        == 404
    )
    session_id = _create_session(client)
    payload = {"ball_no": 1, "variation_intent": "googly"}
    first = client.post(_url(session_id), json=payload, headers=auth(PARENT_TOKEN))
    assert first.status_code == 201
    duplicate = client.post(_url(session_id), json=payload, headers=auth(PARENT_TOKEN))
    assert duplicate.status_code == 409
    assert "use PATCH" in duplicate.json()["detail"]
    bad_ball = client.post(
        _url(session_id),
        json={"ball_no": 0, "variation_intent": "googly"},
        headers=auth(PARENT_TOKEN),
    )
    assert bad_ball.status_code == 422
    bad_variation = client.post(
        _url(session_id),
        json={"ball_no": 2, "variation_intent": "doosra"},
        headers=auth(PARENT_TOKEN),
    )
    assert bad_variation.status_code == 422


# --- list --------------------------------------------------------------------------


def test_list_is_ordered_and_player_readable(client: TestClient) -> None:
    session_id = _create_session(client)
    _seed_row(client, session_id, 3, intent="googly", detected="googly")
    _seed_row(client, session_id, 1, intent="leg_break", detected="top_spinner")
    response = client.get(_url(session_id), headers=auth(PLAYER_TOKEN))
    assert response.status_code == 200
    body = response.json()
    assert [row["ball_no"] for row in body] == [1, 3]
    # Intent and detection are separate fields, shown side by side — never merged.
    assert body[0]["variation_intent"] == "leg_break"
    assert body[0]["variation_detected"] == "top_spinner"
    assert client.get(_url(UNKNOWN_ID), headers=auth(PLAYER_TOKEN)).status_code == 404


# --- patch -------------------------------------------------------------------------


def test_patch_edits_intent_with_audit_and_leaves_detection_alone(client: TestClient) -> None:
    session_id = _create_session(client)
    _seed_row(client, session_id, 1, intent="top_spinner", detected="googly", source="model")
    response = client.patch(
        f"{_url(session_id)}/1",
        json={"variation_intent": "googly"},
        headers=auth(COACH_TOKEN),
    )
    assert response.status_code == 200
    assert response.json() == {
        "ball_no": 1,
        "variation_intent": "googly",
        "variation_detected": "googly",  # untouched: worker-seam property
        "labeler": "coach",
        "source": "manual",  # human assertion forces provenance back to manual
    }
    assert _edit_audits(client) == [
        {
            "ball_no": 1,
            "field": "variation_intent",
            "old_value": "top_spinner",
            "new_value": "googly",
        }
    ]


def test_patch_same_value_confirms_without_audit(client: TestClient) -> None:
    session_id = _create_session(client)
    _seed_row(client, session_id, 1, intent="leg_break", source="model", labeler="prelabeler")
    response = client.patch(
        f"{_url(session_id)}/1",
        json={"variation_intent": "leg_break"},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 200
    assert _edit_audits(client) == []
    row = _row(client, session_id, 1)
    assert row["source"] == "manual"  # confirmed pre-label is now ground truth
    assert row["labeler"] == "parent"


def test_patch_404s(client: TestClient) -> None:
    patch_body = {"variation_intent": "googly"}
    assert (
        client.patch(
            f"{_url(UNKNOWN_ID)}/1", json=patch_body, headers=auth(PARENT_TOKEN)
        ).status_code
        == 404
    )
    session_id = _create_session(client)
    missing = client.patch(f"{_url(session_id)}/9", json=patch_body, headers=auth(PARENT_TOKEN))
    assert missing.status_code == 404
    assert missing.json()["detail"] == "delivery label not found"


# --- export ------------------------------------------------------------------------


def test_export_is_guardian_only_and_audit_logged(client: TestClient) -> None:
    session_id = _create_session(client)
    _seed_row(client, session_id, 1, intent="leg_break", detected="leg_break")
    assert client.get(f"{_url(session_id)}/export", headers=auth(PLAYER_TOKEN)).status_code == 403
    assert client.get(f"{_url(UNKNOWN_ID)}/export", headers=auth(PARENT_TOKEN)).status_code == 404
    response = client.get(f"{_url(session_id)}/export", headers=auth(COACH_TOKEN))
    assert response.status_code == 200
    tasks = response.json()
    assert len(tasks) == 1
    assert tasks[0]["data"]["cricai_delivery"] == {"session_id": session_id, "ball_no": 1}
    factory = client.app.state.session_factory  # type: ignore[union-attr]
    with factory() as db:
        audit = db.scalars(select(AuditLog).where(AuditLog.action == "share_export")).one()
        assert audit.actor == "coach"
        assert audit.entity_id == session_id
        assert audit.detail is not None
        assert audit.detail["surface"] == "labels_export"
        assert len(audit.detail["sha256"]) == 64


# --- import ------------------------------------------------------------------------


def test_import_round_trips_the_export(client: TestClient) -> None:
    session_id = _create_session(client)
    _seed_row(client, session_id, 1, intent="leg_break")
    _seed_row(client, session_id, 2, intent="googly")
    tasks = client.get(f"{_url(session_id)}/export", headers=auth(PARENT_TOKEN)).json()
    # Unchanged re-import: idempotent, nothing counted as an update.
    result = client.post(f"{_url(session_id)}/import", json=tasks, headers=auth(PARENT_TOKEN))
    assert result.status_code == 200
    assert result.json() == {"created": 0, "updated": 0, "audits_written": 0}
    # The tool changed ball 2's intent: one update, one audit row.
    tasks[1]["annotations"][0]["result"][0]["value"]["choices"] = ["top_spinner"]
    result = client.post(f"{_url(session_id)}/import", json=tasks, headers=auth(PARENT_TOKEN))
    assert result.json() == {"created": 0, "updated": 1, "audits_written": 1}
    assert _row(client, session_id, 2)["variation_intent"] == "top_spinner"


def test_import_creates_rows_but_never_writes_detection(client: TestClient) -> None:
    session_id = _create_session(client)
    task = {
        "data": {"cricai_delivery": {"session_id": session_id, "ball_no": 7}},
        "annotations": [
            {
                "result": [
                    {
                        "type": "choices",
                        "from_name": "variation_intent",
                        "to_name": "delivery",
                        "value": {"choices": ["googly"]},
                        "meta": {
                            "labeler": "someone-else",
                            "source": "model",
                            "variation_detected": "leg_break",  # review context only
                        },
                    }
                ]
            }
        ],
    }
    result = client.post(f"{_url(session_id)}/import", json=[task], headers=auth(PARENT_TOKEN))
    assert result.json() == {"created": 1, "updated": 0, "audits_written": 0}
    row = _row(client, session_id, 7)
    assert row == {
        "variation_intent": "googly",
        "variation_detected": None,  # model output enters via the worker seam only
        "labeler": "parent",  # the importing human owns the assertion
        "source": "manual",
    }


def test_import_rejects_foreign_provenance_and_malformed_files(client: TestClient) -> None:
    session_id = _create_session(client)
    other_session = _create_session(client)
    _seed_row(client, other_session, 1, intent="leg_break")
    tasks = client.get(f"{_url(other_session)}/export", headers=auth(PARENT_TOKEN)).json()
    response = client.post(f"{_url(session_id)}/import", json=tasks, headers=auth(PARENT_TOKEN))
    assert response.status_code == 422
    assert "never reassigned" in response.json()["detail"]
    malformed = client.post(
        f"{_url(session_id)}/import",
        json=[{"data": {}}],
        headers=auth(PARENT_TOKEN),
    )
    assert malformed.status_code == 422
    assert "lost its provenance" in malformed.json()["detail"]
    assert (
        client.post(f"{_url(UNKNOWN_ID)}/import", json=[], headers=auth(PARENT_TOKEN)).status_code
        == 404
    )


def test_import_is_parent_only(client: TestClient) -> None:
    session_id = _create_session(client)
    assert (
        client.post(f"{_url(session_id)}/import", json=[], headers=auth(COACH_TOKEN)).status_code
        == 403
    )


# --- concurrency: the uq race surfaces as the documented 409 (finding 17) -----------


def _arm_label_race(monkeypatch: pytest.MonkeyPatch, client: TestClient, ball_no: int) -> None:
    """After ``_get_label`` honestly reports 'no label yet', a rival request
    commits the same (session_id, ball_no) — the exact TOCTOU interleaving of a
    concurrent guardian POST (or a Label Studio import racing a manual label).
    The endpoint code runs unmodified."""
    real_get_label = labels_module._get_label
    factory = client.app.state.session_factory  # type: ignore[union-attr]

    def racing(db: Any, session_id: uuid.UUID, checked_ball_no: int) -> Any:
        result = real_get_label(db, session_id, checked_ball_no)
        if result is None and checked_ball_no == ball_no:
            with factory() as rival:
                rival.add(
                    DeliveryLabel(
                        session_id=session_id,
                        ball_no=checked_ball_no,
                        variation_intent=BowlingVariation.GOOGLY,
                        labeler="coach",
                        source="manual",
                    )
                )
                rival.commit()
        return result

    monkeypatch.setattr(labels_module, "_get_label", racing)


def test_create_race_loser_gets_the_documented_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two guardians label the same ball concurrently: the loser of the
    uq_delivery_label_session_ball race gets this endpoint's own 409, never an
    escaped IntegrityError 500 (finding 17)."""
    session_id = _create_session(client)
    _arm_label_race(monkeypatch, client, 23)
    response = client.post(
        _url(session_id),
        json={"ball_no": 23, "variation_intent": "leg_break"},
        headers=auth(COACH_TOKEN),
    )
    assert response.status_code == 409
    assert "already labeled" in response.json()["detail"]


def test_import_race_loser_gets_a_409_naming_the_ball(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An import racing a manual label for one of its balls surfaces as a 409
    naming the conflicting ball_no, not an anonymous constraint 500; the client
    can simply retry the import (finding 17)."""
    session_id = _create_session(client)

    def _task(ball_no: int) -> dict[str, Any]:
        return {
            "data": {"cricai_delivery": {"session_id": session_id, "ball_no": ball_no}},
            "annotations": [
                {
                    "result": [
                        {
                            "type": "choices",
                            "from_name": "variation_intent",
                            "to_name": "delivery",
                            "value": {"choices": ["top_spinner"]},
                        }
                    ]
                }
            ],
        }

    _arm_label_race(monkeypatch, client, 23)
    response = client.post(
        f"{_url(session_id)}/import", json=[_task(5), _task(23)], headers=auth(PARENT_TOKEN)
    )
    assert response.status_code == 409
    assert "ball 23" in response.json()["detail"]


# --- US-L3 guest scoping (finding 56) ------------------------------------------------


def test_guest_session_labels_are_hidden_from_players(client: TestClient) -> None:
    """US-L3: guest data is parent/coach-only — the labels reader hides a guest
    session from the player role exactly like the metrics/events/clips readers
    (finding 56)."""
    session_id = _create_session(client, is_guest=True)
    _seed_row(client, session_id, 1, intent="leg_break")
    assert client.get(_url(session_id), headers=auth(PLAYER_TOKEN)).status_code == 404
    assert client.get(_url(session_id), headers=auth(COACH_TOKEN)).status_code == 200
    assert client.get(_url(session_id), headers=auth(PARENT_TOKEN)).status_code == 200
