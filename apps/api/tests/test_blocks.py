"""US-B3 acceptance: block CRUD, overlap rejection, gap reporting, ball->block
assignment — plus the US-A5 checklist gate on machine blocks."""

import httpx
import pytest
from cricai_api.routers.checklists import REQUIRED_MACHINE_CHECKLIST
from cricai_api.services.block_assign import assign_block
from fastapi.testclient import TestClient

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, PLAYER_TOKEN, auth

MISSING_ID = "00000000-0000-0000-0000-000000000000"


def _ack_checklist(client: TestClient, session_id: str) -> None:
    response = client.post(
        f"/sessions/{session_id}/checklist-ack",
        json={
            "items": {item_id: True for item_id, _ in REQUIRED_MACHINE_CHECKLIST},
            "acked_by": "parent-dinesh",
        },
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 201


def _create_session(client: TestClient, bowler_source: str = "machine", ack: bool = True) -> str:
    player = client.post(
        "/players",
        json={"name": "Arjun", "birthdate": "2014-11-20"},
        headers=auth(PARENT_TOKEN),
    ).json()
    payload: dict[str, object] = {
        "player_id": player["id"],
        "date": "2026-07-07",
        "session_type": "batting",
        "bowler_source": bowler_source,
    }
    if bowler_source == "machine":
        payload["machine_settings"] = {"speed_kph": 85, "length": "good"}
    session = client.post("/sessions", json=payload, headers=auth(PARENT_TOKEN)).json()
    session_id: str = session["id"]
    if ack:  # machine blocks require the safety checklist (US-A5)
        _ack_checklist(client, session_id)
    return session_id


def _block(start_s: float, end_s: float | None = None, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "start_s": start_s,
        "end_s": end_s,
        "bowler_source": "machine",
        "machine_settings": {"speed_kph": 85, "length": "good"},
        "intent": "technical",
    }
    payload.update(overrides)
    return payload


def _post(
    client: TestClient, session_id: str, payload: dict[str, object], token: str = PARENT_TOKEN
) -> httpx.Response:
    return client.post(f"/sessions/{session_id}/blocks", json=payload, headers=auth(token))


def _patch(
    client: TestClient,
    session_id: str,
    block_no: int,
    payload: dict[str, object],
    token: str = PARENT_TOKEN,
) -> httpx.Response:
    return client.patch(
        f"/sessions/{session_id}/blocks/{block_no}", json=payload, headers=auth(token)
    )


def _list(client: TestClient, session_id: str, token: str = PARENT_TOKEN) -> httpx.Response:
    return client.get(f"/sessions/{session_id}/blocks", headers=auth(token))


def _assign(
    client: TestClient, session_id: str, t_s: float, token: str = PARENT_TOKEN
) -> httpx.Response:
    return client.get(
        f"/sessions/{session_id}/blocks/assign", params={"t_s": t_s}, headers=auth(token)
    )


# ---------------------------------------------------------------- creation


def test_create_first_block_returns_201(client: TestClient) -> None:
    session_id = _create_session(client)
    response = _post(client, session_id, _block(0, 60))
    assert response.status_code == 201
    body = response.json()
    assert body["block_no"] == 1
    assert body["session_id"] == session_id
    assert body["start_s"] == 0.0
    assert body["end_s"] == 60.0
    assert body["bowler_source"] == "machine"
    assert body["machine_settings"] == {"speed_kph": 85.0, "length": "good", "variation": None}
    assert body["intent"] == "technical"


def test_block_numbers_auto_increment_and_touching_is_allowed(client: TestClient) -> None:
    session_id = _create_session(client)
    first = _post(client, session_id, _block(0, 60)).json()
    # new.start_s == prev.end_s: touching boundaries never overlap
    second = _post(client, session_id, _block(60, 120, bowler_source="coach", intent="decision"))
    assert second.status_code == 201
    assert first["block_no"] == 1
    assert second.json()["block_no"] == 2


def test_non_machine_block_needs_no_settings(client: TestClient) -> None:
    session_id = _create_session(client)
    payload = _block(0, 30, bowler_source="human", intent="fun")
    del payload["machine_settings"]
    response = _post(client, session_id, payload)
    assert response.status_code == 201
    assert response.json()["machine_settings"] is None


@pytest.mark.safety
def test_machine_block_on_unacked_session_is_rejected(client: TestClient) -> None:
    session_id = _create_session(client, ack=False)
    blocked = _post(client, session_id, _block(0, 10))
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "safety checklist not acknowledged"
    assert _list(client, session_id).json()["blocks"] == []

    _ack_checklist(client, session_id)
    assert _post(client, session_id, _block(0, 10)).status_code == 201


@pytest.mark.safety
def test_machine_block_inside_coach_session_requires_ack(client: TestClient) -> None:
    """US-A5 gates the ball's bowler_source, not the session's: machine balls
    inside mixed/coach sessions never bypass the checklist."""
    session_id = _create_session(client, bowler_source="coach", ack=False)
    blocked = _post(client, session_id, _block(0, 10))
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "safety checklist not acknowledged"

    _ack_checklist(client, session_id)
    assert _post(client, session_id, _block(0, 10)).status_code == 201


@pytest.mark.safety
def test_coach_and_human_blocks_need_no_checklist(client: TestClient) -> None:
    session_id = _create_session(client, bowler_source="coach", ack=False)
    coach = _block(0, 10, bowler_source="coach", intent="decision")
    del coach["machine_settings"]
    human = _block(10, 20, bowler_source="human", intent="fun")
    del human["machine_settings"]
    assert _post(client, session_id, coach).status_code == 201
    assert _post(client, session_id, human).status_code == 201


def test_overlap_matrix_against_closed_block(client: TestClient) -> None:
    session_id = _create_session(client)
    assert _post(client, session_id, _block(10, 20)).status_code == 201

    overlapping = [
        _block(15, 25),  # straddles the right edge
        _block(5, 15),  # straddles the left edge
        _block(12, 18),  # contained
        _block(5, 25),  # containing
        _block(10, 20),  # identical
        _block(15, None),  # open block starting inside
    ]
    for payload in overlapping:
        response = _post(client, session_id, payload)
        assert response.status_code == 409
        assert response.json()["detail"]["overlapping_block_no"] == 1

    assert _post(client, session_id, _block(0, 10)).status_code == 201  # touch left
    assert _post(client, session_id, _block(20, 30)).status_code == 201  # touch right


def test_open_block_occupies_timeline_until_closed(client: TestClient) -> None:
    session_id = _create_session(client)
    assert _post(client, session_id, _block(0, None)).status_code == 201
    # An open block covers [start_s, inf): anything starting later collides.
    response = _post(client, session_id, _block(500, 600))
    assert response.status_code == 409
    assert response.json()["detail"]["overlapping_block_no"] == 1


def test_auto_close_open_closes_at_new_start(client: TestClient) -> None:
    session_id = _create_session(client)
    _post(client, session_id, _block(0, None))
    response = _post(client, session_id, _block(50, None, auto_close_open=True, intent="fun"))
    assert response.status_code == 201
    assert response.json()["block_no"] == 2
    listed = _list(client, session_id).json()
    assert [b["end_s"] for b in listed["blocks"]] == [50.0, None]
    assert listed["gaps"] == []


def test_auto_close_requires_strictly_later_start(client: TestClient) -> None:
    session_id = _create_session(client)
    _post(client, session_id, _block(10, None))
    # Same start: closing at 10 would make the open block empty -> still a conflict.
    same_start = _post(client, session_id, _block(10, 40, auto_close_open=True))
    assert same_start.status_code == 409
    # Earlier start overlapping the open block cannot be fixed by auto-close either.
    earlier = _post(client, session_id, _block(5, 20, auto_close_open=True))
    assert earlier.status_code == 409
    assert earlier.json()["detail"]["overlapping_block_no"] == 1


def test_block_entirely_before_open_block_is_allowed(client: TestClient) -> None:
    session_id = _create_session(client)
    _post(client, session_id, _block(50, None))
    # Fits before the open block: no conflict, flag or not; the open block stays open.
    assert _post(client, session_id, _block(0, 10)).status_code == 201
    assert _post(client, session_id, _block(10, 20, auto_close_open=True)).status_code == 201
    listed = _list(client, session_id).json()
    assert [(b["block_no"], b["end_s"]) for b in listed["blocks"]] == [
        (2, 10.0),
        (3, 20.0),
        (1, None),
    ]


def test_create_validation_422(client: TestClient) -> None:
    session_id = _create_session(client)
    assert _post(client, session_id, _block(-1, 10)).status_code == 422
    equal = _post(client, session_id, _block(10, 10))
    assert equal.status_code == 422
    assert "end_s must be greater than start_s" in str(equal.json()["detail"])
    assert _post(client, session_id, _block(10, 5)).status_code == 422
    assert _post(client, session_id, _block(0, 10, intent="yoga")).status_code == 422
    bad_speed = _block(0, 10, machine_settings={"speed_kph": 999, "length": "good"})
    assert _post(client, session_id, bad_speed).status_code == 422


def test_unknown_session_is_404_on_every_endpoint(client: TestClient) -> None:
    assert _post(client, MISSING_ID, _block(0, 10)).status_code == 404
    assert _list(client, MISSING_ID).status_code == 404
    assert _assign(client, MISSING_ID, 1.0).status_code == 404
    assert _patch(client, MISSING_ID, 1, {"end_s": 10}).status_code == 404


# ---------------------------------------------------------------- patch


def test_patch_closes_open_block(client: TestClient) -> None:
    session_id = _create_session(client)
    _post(client, session_id, _block(0, None))
    response = _patch(client, session_id, 1, {"end_s": 90})
    assert response.status_code == 200
    assert response.json()["end_s"] == 90.0
    # The timeline after 90 s is free again.
    assert _post(client, session_id, _block(90, 120)).status_code == 201


def test_patch_end_s_validation(client: TestClient) -> None:
    session_id = _create_session(client)
    _post(client, session_id, _block(10, None))
    null_end = _patch(client, session_id, 1, {"end_s": None})
    assert null_end.status_code == 422
    assert "cannot be reopened" in null_end.json()["detail"]
    too_early = _patch(client, session_id, 1, {"end_s": 10})
    assert too_early.status_code == 422
    assert "end_s must be greater than start_s" in too_early.json()["detail"]


def test_patch_cannot_extend_into_next_block(client: TestClient) -> None:
    session_id = _create_session(client)
    _post(client, session_id, _block(0, 10))
    _post(client, session_id, _block(20, 30))
    response = _patch(client, session_id, 1, {"end_s": 25})
    assert response.status_code == 409
    assert response.json()["detail"]["overlapping_block_no"] == 2
    touching = _patch(client, session_id, 1, {"end_s": 20})
    assert touching.status_code == 200
    assert touching.json()["end_s"] == 20.0


def test_patch_cannot_extend_into_open_block(client: TestClient) -> None:
    session_id = _create_session(client)
    _post(client, session_id, _block(0, 5))
    _post(client, session_id, _block(10, None))
    response = _patch(client, session_id, 1, {"end_s": 12})
    assert response.status_code == 409
    assert response.json()["detail"]["overlapping_block_no"] == 2
    # Shrinking is always safe and simply widens the gap.
    assert _patch(client, session_id, 1, {"end_s": 3}).json()["end_s"] == 3.0


def test_patch_machine_settings_and_intent(client: TestClient) -> None:
    session_id = _create_session(client)
    _post(client, session_id, _block(0, 60))

    updated = _patch(
        client,
        session_id,
        1,
        {"machine_settings": {"speed_kph": 100, "length": "short"}, "intent": "decision"},
    ).json()
    assert updated["machine_settings"] == {
        "speed_kph": 100.0,
        "length": "short",
        "variation": None,
    }
    assert updated["intent"] == "decision"

    cleared = _patch(client, session_id, 1, {"machine_settings": None}).json()
    assert cleared["machine_settings"] is None
    assert cleared["intent"] == "decision"

    # Explicit null intent is a no-op; an empty patch changes nothing.
    assert _patch(client, session_id, 1, {"intent": None}).json()["intent"] == "decision"
    untouched = _patch(client, session_id, 1, {}).json()
    assert untouched["intent"] == "decision"
    assert untouched["end_s"] == 60.0

    bad = _patch(client, session_id, 1, {"machine_settings": {"speed_kph": 999, "length": "good"}})
    assert bad.status_code == 422


def test_patch_unknown_block_404(client: TestClient) -> None:
    session_id = _create_session(client)
    response = _patch(client, session_id, 7, {"end_s": 10})
    assert response.status_code == 404
    assert response.json()["detail"] == "block not found"


# ---------------------------------------------------------------- listing & gaps


def test_list_empty_session(client: TestClient) -> None:
    session_id = _create_session(client)
    assert _list(client, session_id).json() == {"blocks": [], "gaps": []}


def test_single_and_touching_blocks_have_no_gaps(client: TestClient) -> None:
    session_id = _create_session(client)
    _post(client, session_id, _block(0, 10))
    assert _list(client, session_id).json()["gaps"] == []
    _post(client, session_id, _block(10, 20))
    assert _list(client, session_id).json()["gaps"] == []


def test_gaps_reported_between_sorted_blocks(client: TestClient) -> None:
    session_id = _create_session(client)
    _post(client, session_id, _block(0, 10))
    _post(client, session_id, _block(15, 25))
    _post(client, session_id, _block(40, None))
    listed = _list(client, session_id).json()
    assert [b["block_no"] for b in listed["blocks"]] == [1, 2, 3]
    assert listed["gaps"] == [
        {"after_block_no": 1, "from_s": 10.0, "to_s": 15.0},
        {"after_block_no": 2, "from_s": 25.0, "to_s": 40.0},
    ]


def test_leading_gap_reported_when_first_block_starts_after_zero(client: TestClient) -> None:
    session_id = _create_session(client)
    _post(client, session_id, _block(5, 10))
    _post(client, session_id, _block(15, None))
    listed = _list(client, session_id).json()
    assert listed["gaps"] == [
        {"after_block_no": None, "from_s": 0.0, "to_s": 5.0},  # leading gap
        {"after_block_no": 1, "from_s": 10.0, "to_s": 15.0},
    ]


def test_gaps_follow_timeline_order_not_creation_order(client: TestClient) -> None:
    session_id = _create_session(client)
    _post(client, session_id, _block(20, 30))  # block 1, later on the timeline
    _post(client, session_id, _block(0, 10))  # block 2, earlier on the timeline
    listed = _list(client, session_id).json()
    assert [b["block_no"] for b in listed["blocks"]] == [2, 1]
    assert listed["gaps"] == [{"after_block_no": 2, "from_s": 10.0, "to_s": 20.0}]


# ---------------------------------------------------------------- assignment


def test_assign_endpoint_boundary_cases(client: TestClient) -> None:
    session_id = _create_session(client)
    _post(client, session_id, _block(5, 10))
    _post(client, session_id, _block(10, 20))
    _post(client, session_id, _block(30, None))

    expectations = {
        0.0: None,  # before the first block
        5.0: 1,  # exactly on a start -> that block
        7.5: 1,  # inside a closed block
        10.0: 2,  # shared boundary -> the LATER block
        20.0: None,  # exactly on an end with a gap after
        25.0: None,  # inside a gap
        30.0: 3,  # open block start
        9999.0: 3,  # open block extends to infinity
    }
    for t_s, block_no in expectations.items():
        assert _assign(client, session_id, t_s).json() == {"block_no": block_no}

    assert (
        client.get(f"/sessions/{session_id}/blocks/assign", headers=auth(PARENT_TOKEN)).status_code
        == 422
    )  # t_s is required
    assert _assign(client, session_id, -1.0).status_code == 422


def test_assign_block_pure_function() -> None:
    assert assign_block([], 5.0) is None
    spans = [(1, 0.0, 10.0), (2, 10.0, 20.0), (3, 30.0, None)]
    assert assign_block(spans, 0.0) == 1
    assert assign_block(spans, 10.0) == 2  # boundary belongs to the LATER block
    assert assign_block(spans, 20.0) is None
    assert assign_block(spans, 45.0) == 3


# ---------------------------------------------------------------- RBAC


def test_rbac_writes_restricted_reads_open(client: TestClient) -> None:
    session_id = _create_session(client)
    assert _post(client, session_id, _block(0, 10), token=PLAYER_TOKEN).status_code == 403
    assert _post(client, session_id, _block(0, 10), token=COACH_TOKEN).status_code == 201
    assert _patch(client, session_id, 1, {"intent": "fun"}, token=PLAYER_TOKEN).status_code == 403
    assert _patch(client, session_id, 1, {"intent": "fun"}, token=COACH_TOKEN).status_code == 200
    assert _list(client, session_id, token=PLAYER_TOKEN).status_code == 200
    assert _assign(client, session_id, 5.0, token=PLAYER_TOKEN).status_code == 200
    unauthenticated = client.get(f"/sessions/{session_id}/blocks")
    assert unauthenticated.status_code == 401
