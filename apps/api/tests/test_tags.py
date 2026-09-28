"""US-B4 acceptance: manual tags, audit history, export/import round-trip."""

import hashlib
import uuid
from typing import Any

import pytest
from cricai_api.routers.tags import EXPORT_FIELDS
from cricai_data.enums import BlockIntent, BowlerSource
from cricai_data.models import AuditLog, SessionBlock
from fastapi.testclient import TestClient
from sqlalchemy import select

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, PLAYER_TOKEN, auth

CANONICAL_FIELDS = (
    "ball_no",
    "block_no",
    "line",
    "length",
    "shot",
    "footwork",
    "contact",
    "outcome",
    "control",
    "source",
    "ground_truth_eligible",
)


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


def _create_guest_session(client: TestClient) -> str:
    """A session owned by a guest player (US-L3: parent/coach-only data)."""
    player = client.post(
        "/players",
        json={"name": "Visitor", "birthdate": "2014-11-20", "is_guest": True},
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


def _add_block(client: TestClient, session_id: str, block_no: int = 1) -> None:
    """Seed a block directly (blocks API is another story's placeholder)."""
    factory = client.app.state.session_factory  # type: ignore[union-attr]
    db = factory()
    try:
        db.add(
            SessionBlock(
                session_id=uuid.UUID(session_id),
                block_no=block_no,
                start_s=0.0,
                bowler_source=BowlerSource.COACH,
                intent=BlockIntent.TECHNICAL,
            )
        )
        db.commit()
    finally:
        db.close()


def _tag_payload(ball_no: int = 1, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "ball_no": ball_no,
        "line": "off",
        "length": "good",
        "shot": "drive",
        "footwork": "front",
        "contact": "middle",
        "outcome": "controlled_ground_shot",
        "control": True,
    }
    payload.update(overrides)
    return payload


def _tags_url(session_id: str) -> str:
    return f"/sessions/{session_id}/tags"


def _export_audits(client: TestClient, session_id: str) -> list[tuple[str, dict[str, Any] | None]]:
    """(actor, detail) of every share_export audit row written for the session."""
    factory = client.app.state.session_factory  # type: ignore[union-attr]
    db = factory()
    try:
        rows = db.scalars(
            select(AuditLog).where(
                AuditLog.action == "share_export",
                AuditLog.entity == "session",
                AuditLog.entity_id == session_id,
            )
        ).all()
        return [(row.actor, row.detail) for row in rows]
    finally:
        db.close()


def test_create_tag_is_manual_ground_truth(client: TestClient) -> None:
    session_id = _create_session(client)
    response = client.post(_tags_url(session_id), json=_tag_payload(), headers=auth(PARENT_TOKEN))
    assert response.status_code == 201
    body = response.json()
    assert body["source"] == "manual"
    assert body["ground_truth_eligible"] is True
    assert body["created_by"] == "parent"
    assert body["block_no"] is None
    assert body["audits"] == []

    coached = client.post(
        _tags_url(session_id), json=_tag_payload(ball_no=2), headers=auth(COACH_TOKEN)
    )
    assert coached.json()["created_by"] == "coach"


def test_create_tag_resolves_block_no(client: TestClient) -> None:
    session_id = _create_session(client)
    _add_block(client, session_id, block_no=2)
    response = client.post(
        _tags_url(session_id),
        json=_tag_payload(block_no=2),
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 201
    assert response.json()["block_no"] == 2

    missing = client.post(
        _tags_url(session_id),
        json=_tag_payload(ball_no=2, block_no=9),
        headers=auth(PARENT_TOKEN),
    )
    assert missing.status_code == 404
    assert "block 9" in missing.json()["detail"]


def test_duplicate_ball_no_409(client: TestClient) -> None:
    session_id = _create_session(client)
    client.post(_tags_url(session_id), json=_tag_payload(), headers=auth(PARENT_TOKEN))
    duplicate = client.post(_tags_url(session_id), json=_tag_payload(), headers=auth(COACH_TOKEN))
    assert duplicate.status_code == 409
    assert "use PATCH" in duplicate.json()["detail"]


def test_list_tags_ordered_by_ball_no(client: TestClient) -> None:
    session_id = _create_session(client)
    client.post(_tags_url(session_id), json=_tag_payload(ball_no=2), headers=auth(PARENT_TOKEN))
    client.post(_tags_url(session_id), json=_tag_payload(ball_no=1), headers=auth(PARENT_TOKEN))
    response = client.get(_tags_url(session_id), headers=auth(PLAYER_TOKEN))
    assert response.status_code == 200
    assert [t["ball_no"] for t in response.json()] == [1, 2]


def test_patch_writes_audit_old_to_new(client: TestClient) -> None:
    session_id = _create_session(client)
    client.post(_tags_url(session_id), json=_tag_payload(), headers=auth(PARENT_TOKEN))

    response = client.patch(
        f"{_tags_url(session_id)}/1",
        json={"line": "middle", "control": False},
        headers=auth(COACH_TOKEN),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["line"] == "middle"
    assert body["control"] is False
    audits = {(a["field"], a["old_value"], a["new_value"], a["actor"]) for a in body["audits"]}
    assert audits == {
        ("line", "off", "middle", "coach"),
        ("control", "true", "false", "coach"),
    }
    assert all(a["at"] is not None for a in body["audits"])


def test_multiple_edits_accumulate_full_history(client: TestClient) -> None:
    session_id = _create_session(client)
    client.post(_tags_url(session_id), json=_tag_payload(), headers=auth(PARENT_TOKEN))
    client.patch(f"{_tags_url(session_id)}/1", json={"control": False}, headers=auth(COACH_TOKEN))
    second = client.patch(
        f"{_tags_url(session_id)}/1",
        json={"control": True, "shot": "cut"},
        headers=auth(PARENT_TOKEN),
    )
    audits = second.json()["audits"]
    assert len(audits) == 3
    assert {(a["field"], a["old_value"], a["new_value"], a["actor"]) for a in audits} == {
        ("control", "true", "false", "coach"),
        ("control", "false", "true", "parent"),
        ("shot", "drive", "cut", "parent"),
    }
    listed = client.get(_tags_url(session_id), headers=auth(PLAYER_TOKEN)).json()
    assert len(listed[0]["audits"]) == 3


def test_patch_noop_and_null_fields_write_no_audit(client: TestClient) -> None:
    session_id = _create_session(client)
    client.post(_tags_url(session_id), json=_tag_payload(), headers=auth(PARENT_TOKEN))

    same_value = client.patch(
        f"{_tags_url(session_id)}/1", json={"line": "off"}, headers=auth(PARENT_TOKEN)
    )
    assert same_value.status_code == 200
    assert same_value.json()["audits"] == []

    explicit_null = client.patch(
        f"{_tags_url(session_id)}/1", json={"shot": None}, headers=auth(PARENT_TOKEN)
    )
    assert explicit_null.status_code == 200
    assert explicit_null.json()["shot"] == "drive"
    assert explicit_null.json()["audits"] == []


def test_patch_unknown_tag_404(client: TestClient) -> None:
    session_id = _create_session(client)
    response = client.patch(
        f"{_tags_url(session_id)}/7", json={"line": "leg"}, headers=auth(PARENT_TOKEN)
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "tag not found"


def test_unknown_session_404_on_every_endpoint(client: TestClient) -> None:
    url = _tags_url(str(uuid.uuid4()))
    assert client.post(url, json=_tag_payload(), headers=auth(PARENT_TOKEN)).status_code == 404
    assert client.get(url, headers=auth(PARENT_TOKEN)).status_code == 404
    assert client.get(f"{url}/export", headers=auth(PARENT_TOKEN)).status_code == 404
    assert (
        client.patch(f"{url}/1", json={"line": "leg"}, headers=auth(PARENT_TOKEN)).status_code
        == 404
    )
    assert client.post(f"{url}/import", json=[], headers=auth(PARENT_TOKEN)).status_code == 404


def test_invalid_payloads_422(client: TestClient) -> None:
    session_id = _create_session(client)

    bad_enum = client.post(
        _tags_url(session_id), json=_tag_payload(line="wide"), headers=auth(PARENT_TOKEN)
    )
    assert bad_enum.status_code == 422
    assert any("line" in str(e["loc"]) for e in bad_enum.json()["detail"])

    bad_ball = client.post(
        _tags_url(session_id), json=_tag_payload(ball_no=0), headers=auth(PARENT_TOKEN)
    )
    assert bad_ball.status_code == 422
    assert any("ball_no" in str(e["loc"]) for e in bad_ball.json()["detail"])

    bad_patch = client.patch(
        f"{_tags_url(session_id)}/1", json={"footwork": "tiptoe"}, headers=auth(PARENT_TOKEN)
    )
    assert bad_patch.status_code == 422

    bad_format = client.get(
        f"{_tags_url(session_id)}/export",
        params={"format": "xml"},
        headers=auth(PARENT_TOKEN),
    )
    assert bad_format.status_code == 422


def test_rbac_writes_restricted_reads_open(client: TestClient) -> None:
    session_id = _create_session(client)
    url = _tags_url(session_id)

    assert client.post(url, json=_tag_payload(), headers=auth(PLAYER_TOKEN)).status_code == 403
    client.post(url, json=_tag_payload(), headers=auth(PARENT_TOKEN))
    assert (
        client.patch(f"{url}/1", json={"line": "leg"}, headers=auth(PLAYER_TOKEN)).status_code
        == 403
    )
    assert client.post(f"{url}/import", json=[], headers=auth(COACH_TOKEN)).status_code == 403
    assert client.post(f"{url}/import", json=[], headers=auth(PLAYER_TOKEN)).status_code == 403

    assert client.get(url, headers=auth(PLAYER_TOKEN)).status_code == 200
    assert client.get(f"{url}/export", headers=auth(COACH_TOKEN)).status_code == 200
    assert client.get(url).status_code == 401


@pytest.mark.safety
def test_guest_session_tags_are_hidden_from_players(client: TestClient) -> None:
    """SAF (US-L3, finding [56]): a guest bowler's tags are parent/coach-only.

    tags.py had copied the one sibling reader lacking the guest guard; the
    player role must now get 404 on a guest session's tags — the same hide-
    existence rule events/clips/metrics enforce — while reviewers still read them.
    """
    session_id = _create_guest_session(client)
    url = _tags_url(session_id)
    client.post(url, json=_tag_payload(), headers=auth(PARENT_TOKEN))  # parent writes fine

    assert client.get(url, headers=auth(PLAYER_TOKEN)).status_code == 404
    assert client.get(url, headers=auth(COACH_TOKEN)).status_code == 200
    assert client.get(url, headers=auth(PARENT_TOKEN)).status_code == 200


@pytest.mark.safety
def test_export_forbidden_for_player(client: TestClient) -> None:
    """SAF (US-L3): the tags export surface is closed to the player role."""
    session_id = _create_session(client)
    client.post(_tags_url(session_id), json=_tag_payload(), headers=auth(PARENT_TOKEN))

    for params in ({}, {"format": "json"}, {"format": "csv"}):
        response = client.get(
            f"{_tags_url(session_id)}/export", params=params, headers=auth(PLAYER_TOKEN)
        )
        assert response.status_code == 403
    assert _export_audits(client, session_id) == []


@pytest.mark.safety
def test_export_writes_audit_with_sha256(client: TestClient) -> None:
    """SAF (US-L3): every export writes an audit row hashing the exported bytes."""
    session_id = _create_session(client)
    client.post(_tags_url(session_id), json=_tag_payload(), headers=auth(PARENT_TOKEN))

    json_export = client.get(f"{_tags_url(session_id)}/export", headers=auth(COACH_TOKEN))
    csv_export = client.get(
        f"{_tags_url(session_id)}/export",
        params={"format": "csv"},
        headers=auth(PARENT_TOKEN),
    )
    assert json_export.status_code == 200
    assert csv_export.status_code == 200

    audits = _export_audits(client, session_id)
    assert len(audits) == 2
    by_actor = dict(audits)
    assert by_actor["coach"] == {
        "format": "json",
        "sha256": hashlib.sha256(json_export.content).hexdigest(),
        "surface": "tags_export",
    }
    assert by_actor["parent"] == {
        "format": "csv",
        "sha256": hashlib.sha256(csv_export.content).hexdigest(),
        "surface": "tags_export",
    }


def test_export_json_matches_canonical_schema(client: TestClient) -> None:
    """REG (US-G1): export keys, order and enum values are pinned."""
    assert EXPORT_FIELDS == CANONICAL_FIELDS

    session_id = _create_session(client)
    _add_block(client, session_id, block_no=1)
    client.post(_tags_url(session_id), json=_tag_payload(block_no=1), headers=auth(PARENT_TOKEN))
    response = client.get(f"{_tags_url(session_id)}/export", headers=auth(COACH_TOKEN))
    assert response.status_code == 200
    rows = response.json()
    assert rows == [
        {
            "ball_no": 1,
            "block_no": 1,
            "line": "off",
            "length": "good",
            "shot": "drive",
            "footwork": "front",
            "contact": "middle",
            "outcome": "controlled_ground_shot",
            "control": True,
            "source": "manual",
            "ground_truth_eligible": True,
        }
    ]
    assert tuple(rows[0]) == CANONICAL_FIELDS


def test_export_csv_header_and_values(client: TestClient) -> None:
    session_id = _create_session(client)
    _add_block(client, session_id, block_no=1)
    client.post(
        _tags_url(session_id),
        json=_tag_payload(ball_no=1, control=False),
        headers=auth(PARENT_TOKEN),
    )
    client.post(
        _tags_url(session_id),
        json=_tag_payload(
            ball_no=2,
            block_no=1,
            line="leg",
            length="short",
            shot="pull",
            footwork="back",
            contact="edge",
            outcome="uncontrolled",
        ),
        headers=auth(PARENT_TOKEN),
    )
    response = client.get(
        f"{_tags_url(session_id)}/export",
        params={"format": "csv"},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.text.splitlines() == [
        ",".join(CANONICAL_FIELDS),
        "1,,off,good,drive,front,middle,controlled_ground_shot,false,manual,true",
        "2,1,leg,short,pull,back,edge,uncontrolled,true,manual,true",
    ]


def test_import_round_trip_is_byte_equal(client: TestClient) -> None:
    """IT (US-B4): tag → export → re-import into a second session loses nothing."""
    first = _create_session(client)
    _add_block(client, first, block_no=1)
    client.post(_tags_url(first), json=_tag_payload(ball_no=1), headers=auth(PARENT_TOKEN))
    client.post(
        _tags_url(first),
        json=_tag_payload(ball_no=2, block_no=1, control=False),
        headers=auth(COACH_TOKEN),
    )
    client.post(
        _tags_url(first),
        json=_tag_payload(ball_no=3, shot="sweep", outcome="beaten", contact="miss"),
        headers=auth(PARENT_TOKEN),
    )
    exported = client.get(f"{_tags_url(first)}/export", headers=auth(PARENT_TOKEN))

    second = _create_session(client)
    _add_block(client, second, block_no=1)
    imported = client.post(
        f"{_tags_url(second)}/import", json=exported.json(), headers=auth(PARENT_TOKEN)
    )
    assert imported.status_code == 200
    assert imported.json() == {"created": 3, "updated": 0, "audits_written": 0}

    re_exported = client.get(f"{_tags_url(second)}/export", headers=auth(PARENT_TOKEN))
    assert re_exported.content == exported.content


def test_import_upserts_with_audits_and_forces_manual(client: TestClient) -> None:
    session_id = _create_session(client)
    client.post(_tags_url(session_id), json=_tag_payload(), headers=auth(PARENT_TOKEN))

    tampered = _tag_payload(line="leg") | {"source": "auto", "ground_truth_eligible": False}
    response = client.post(
        f"{_tags_url(session_id)}/import", json=[tampered], headers=auth(PARENT_TOKEN)
    )
    assert response.json() == {"created": 0, "updated": 1, "audits_written": 1}

    [tag] = client.get(_tags_url(session_id), headers=auth(PARENT_TOKEN)).json()
    assert tag["line"] == "leg"
    assert tag["source"] == "manual"  # provenance forced back to manual
    assert tag["ground_truth_eligible"] is True
    assert [(a["field"], a["old_value"], a["new_value"], a["actor"]) for a in tag["audits"]] == [
        ("line", "off", "leg", "parent")
    ]

    again = client.post(
        f"{_tags_url(session_id)}/import", json=[tampered], headers=auth(PARENT_TOKEN)
    )
    assert again.json() == {"created": 0, "updated": 0, "audits_written": 0}


def test_import_audits_block_link_changes(client: TestClient) -> None:
    session_id = _create_session(client)
    _add_block(client, session_id, block_no=1)
    client.post(_tags_url(session_id), json=_tag_payload(), headers=auth(PARENT_TOKEN))

    linked = client.post(
        f"{_tags_url(session_id)}/import",
        json=[_tag_payload(block_no=1)],
        headers=auth(PARENT_TOKEN),
    )
    assert linked.json() == {"created": 0, "updated": 1, "audits_written": 1}
    [tag] = client.get(_tags_url(session_id), headers=auth(PARENT_TOKEN)).json()
    assert tag["block_no"] == 1
    assert (tag["audits"][0]["field"], tag["audits"][0]["old_value"]) == ("block_no", None)
    assert tag["audits"][0]["new_value"] == "1"

    unlinked = client.post(
        f"{_tags_url(session_id)}/import",
        json=[_tag_payload(block_no=None)],
        headers=auth(PARENT_TOKEN),
    )
    assert unlinked.json() == {"created": 0, "updated": 1, "audits_written": 1}
    [tag] = client.get(_tags_url(session_id), headers=auth(PARENT_TOKEN)).json()
    assert tag["block_no"] is None
    assert (tag["audits"][1]["old_value"], tag["audits"][1]["new_value"]) == ("1", None)


def test_import_unknown_block_404_and_atomic(client: TestClient) -> None:
    session_id = _create_session(client)
    payload = [_tag_payload(ball_no=1), _tag_payload(ball_no=2, block_no=5)]
    response = client.post(
        f"{_tags_url(session_id)}/import", json=payload, headers=auth(PARENT_TOKEN)
    )
    assert response.status_code == 404
    # the whole import rolled back: the valid first item was not committed
    assert client.get(_tags_url(session_id), headers=auth(PARENT_TOKEN)).json() == []
