"""US-G2 rules API: versioned CRUD with approval gate, per-player overrides with
audit rows, and the run-rules-for-session endpoint over canonical BallRecords.
"""

import uuid
from typing import Any

from cricai_data.enums import Contact, Footwork, Length, Line, Outcome, Shot
from cricai_data.models import AuditLog, BallTag, CoachingRule
from fastapi.testclient import TestClient
from sqlalchemy import select

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, PLAYER_TOKEN, auth


def _edge_rule() -> dict[str, Any]:
    return {
        "metric": "contact_quality",
        "op": "eq",
        "value": "edge",
        "condition": {"line": ["outside_off"]},
        "min_n": 10,
        "severity": "major",
        "text_data": {"finding": "The ball outside off is finding your edge too often."},
    }


def _create_player(client: TestClient) -> str:
    player = client.post(
        "/players",
        json={"name": "Arjun", "birthdate": "2014-11-20"},
        headers=auth(PARENT_TOKEN),
    ).json()
    return str(player["id"])


def _create_session(client: TestClient, player_id: str) -> str:
    response = client.post(
        "/sessions",
        json={
            "player_id": player_id,
            "date": "2026-07-07",
            "session_type": "batting",
            "bowler_source": "coach",
        },
        headers=auth(PARENT_TOKEN),
    )
    return str(response.json()["id"])


def _seed_edge_tags(client: TestClient, session_id: str, n: int) -> None:
    factory = client.app.state.session_factory  # type: ignore[union-attr]
    db = factory()
    try:
        for ball_no in range(1, n + 1):
            db.add(
                BallTag(
                    session_id=uuid.UUID(session_id),
                    ball_no=ball_no,
                    line=Line.OUTSIDE_OFF,
                    length=Length.FULL,
                    shot=Shot.DEFEND,
                    footwork=Footwork.FRONT,
                    contact=Contact.EDGE,
                    outcome=Outcome.EDGED,
                    control=False,
                    created_by="coach",
                )
            )
        db.commit()
    finally:
        db.close()


def _create_rule(client: TestClient, **overrides: Any) -> Any:
    payload: dict[str, Any] = {
        "rule_key": "edges_outside_off",
        "definition": _edge_rule(),
        "author": "coach",
        "rationale": "repeated edges outside off earn a leave-or-smother block",
        "approved_by": "coach",
        "enabled": True,
    }
    payload.update(overrides)
    return client.post("/rules", json=payload, headers=auth(COACH_TOKEN))


# --------------------------------------------------------------------------
# Create / list / versions
# --------------------------------------------------------------------------


class TestCreateAndList:
    def test_create_then_list_and_versions(self, client: TestClient) -> None:
        first = _create_rule(client)
        assert first.status_code == 201
        assert first.json()["version"] == 1
        second = _create_rule(client, rationale="tightened after review")
        assert second.json()["version"] == 2  # append-only

        listing = client.get("/rules", headers=auth(COACH_TOKEN)).json()
        assert [r["rule_key"] for r in listing] == ["edges_outside_off"]
        assert listing[0]["version"] == 2  # latest only

        versions = client.get("/rules/edges_outside_off", headers=auth(COACH_TOKEN)).json()
        assert [v["version"] for v in versions] == [1, 2]

    def test_create_rejects_invalid_definition(self, client: TestClient) -> None:
        response = _create_rule(client, definition={**_edge_rule(), "op": "ne"})
        assert response.status_code == 422
        assert "unknown op" in response.json()["detail"]

    def test_enabled_without_approval_is_conflict(self, client: TestClient) -> None:
        response = _create_rule(client, approved_by=None, enabled=True)
        assert response.status_code == 409
        assert "approved_by" in response.json()["detail"]

    def test_get_versions_unknown_rule_404(self, client: TestClient) -> None:
        assert client.get("/rules/nope", headers=auth(COACH_TOKEN)).status_code == 404

    def test_player_role_forbidden(self, client: TestClient) -> None:
        assert client.get("/rules", headers=auth(PLAYER_TOKEN)).status_code == 403


# --------------------------------------------------------------------------
# Enable / disable
# --------------------------------------------------------------------------


class TestEnableDisable:
    def test_enable_requires_approval(self, client: TestClient) -> None:
        _create_rule(client, approved_by=None, enabled=False)
        response = client.post("/rules/edges_outside_off/enable", headers=auth(COACH_TOKEN))
        assert response.status_code == 409
        assert "approval is required" in response.json()["detail"]

    def test_enable_then_disable(self, client: TestClient) -> None:
        _create_rule(client, enabled=False)
        enabled = client.post("/rules/edges_outside_off/enable", headers=auth(COACH_TOKEN))
        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True
        disabled = client.post("/rules/edges_outside_off/disable", headers=auth(COACH_TOKEN))
        assert disabled.json()["enabled"] is False

    def test_enable_unknown_rule_404(self, client: TestClient) -> None:
        assert client.post("/rules/nope/enable", headers=auth(COACH_TOKEN)).status_code == 404


# --------------------------------------------------------------------------
# Overrides
# --------------------------------------------------------------------------


class TestOverrides:
    def test_create_disable_override_writes_audit(self, client: TestClient) -> None:
        _create_rule(client)
        player_id = _create_player(client)
        response = client.post(
            "/rules/edges_outside_off/overrides",
            json={"player_id": player_id, "action": "disable", "reason": "too advanced for now"},
            headers=auth(COACH_TOKEN),
        )
        assert response.status_code == 201
        assert response.json()["actor"] == "coach"

        factory = client.app.state.session_factory  # type: ignore[union-attr]
        with factory() as db:
            audits = db.scalars(
                select(AuditLog).where(AuditLog.action == "rule_override_created")
            ).all()
        assert len(audits) == 1

    def test_adjust_override_revalidates(self, client: TestClient) -> None:
        _create_rule(client)
        player_id = _create_player(client)
        ok = client.post(
            "/rules/edges_outside_off/overrides",
            json={
                "player_id": player_id,
                "action": "adjust",
                "params": {"min_n": 15},
                "reason": "needs more balls for this player",
            },
            headers=auth(COACH_TOKEN),
        )
        assert ok.status_code == 201
        bad = client.post(
            "/rules/edges_outside_off/overrides",
            json={
                "player_id": player_id,
                "action": "adjust",
                "params": {"op": "lt"},
                "reason": "invalid",
            },
            headers=auth(COACH_TOKEN),
        )
        assert bad.status_code == 422

    def test_override_unknown_rule_404(self, client: TestClient) -> None:
        player_id = _create_player(client)
        response = client.post(
            "/rules/nope/overrides",
            json={"player_id": player_id, "action": "disable", "reason": "x"},
            headers=auth(COACH_TOKEN),
        )
        assert response.status_code == 404

    def test_override_unknown_player_404(self, client: TestClient) -> None:
        _create_rule(client)
        response = client.post(
            "/rules/edges_outside_off/overrides",
            json={"player_id": str(uuid.uuid4()), "action": "disable", "reason": "x"},
            headers=auth(COACH_TOKEN),
        )
        assert response.status_code == 404

    def test_list_overrides_filters(self, client: TestClient) -> None:
        _create_rule(client)
        player_id = _create_player(client)
        client.post(
            "/rules/edges_outside_off/overrides",
            json={"player_id": player_id, "action": "disable", "reason": "x"},
            headers=auth(COACH_TOKEN),
        )
        assert len(client.get("/rules/overrides", headers=auth(COACH_TOKEN)).json()) == 1
        by_player = client.get(f"/rules/overrides?player_id={player_id}", headers=auth(COACH_TOKEN))
        assert len(by_player.json()) == 1
        by_rule = client.get(
            "/rules/overrides?rule_key=edges_outside_off", headers=auth(COACH_TOKEN)
        )
        assert len(by_rule.json()) == 1
        assert (
            client.get(
                f"/rules/overrides?player_id={uuid.uuid4()}", headers=auth(COACH_TOKEN)
            ).json()
            == []
        )


# --------------------------------------------------------------------------
# Run rules for a session
# --------------------------------------------------------------------------


class TestRunRules:
    def test_run_fires_the_active_rule(self, client: TestClient) -> None:
        _create_rule(client)  # enabled + approved
        player_id = _create_player(client)
        session_id = _create_session(client, player_id)
        _seed_edge_tags(client, session_id, n=11)
        response = client.post(f"/rules/run/{session_id}", headers=auth(COACH_TOKEN))
        assert response.status_code == 200
        body = response.json()
        assert body["records"] == 11
        assert body["rules_run"] == 1
        assert len(body["findings"]) == 1
        assert body["findings"][0]["rule_key"] == "edges_outside_off"
        assert body["findings"][0]["n"] == 11

    def test_run_respects_player_override(self, client: TestClient) -> None:
        _create_rule(client)
        player_id = _create_player(client)
        session_id = _create_session(client, player_id)
        _seed_edge_tags(client, session_id, n=11)
        client.post(
            "/rules/edges_outside_off/overrides",
            json={"player_id": player_id, "action": "disable", "reason": "not yet"},
            headers=auth(COACH_TOKEN),
        )
        response = client.post(f"/rules/run/{session_id}", headers=auth(COACH_TOKEN))
        assert response.json()["findings"] == []

    def test_run_unknown_session_404(self, client: TestClient) -> None:
        response = client.post(f"/rules/run/{uuid.uuid4()}", headers=auth(COACH_TOKEN))
        assert response.status_code == 404

    def test_run_with_no_rules_is_empty(self, client: TestClient) -> None:
        player_id = _create_player(client)
        session_id = _create_session(client, player_id)
        response = client.post(f"/rules/run/{session_id}", headers=auth(COACH_TOKEN))
        body = response.json()
        assert body["records"] == 0
        assert body["rules_run"] == 0
        assert body["findings"] == []

    def test_run_ignores_unapproved_rule(self, client: TestClient) -> None:
        # a rule row present but never approved must not fire (defence in depth)
        factory = client.app.state.session_factory  # type: ignore[union-attr]
        with factory() as db:
            db.add(
                CoachingRule(
                    rule_key="edges_outside_off",
                    version=1,
                    author="coach",
                    approved_by=None,
                    rationale="draft",
                    definition=_edge_rule(),
                    enabled=True,
                )
            )
            db.commit()
        player_id = _create_player(client)
        session_id = _create_session(client, player_id)
        _seed_edge_tags(client, session_id, n=11)
        response = client.post(f"/rules/run/{session_id}", headers=auth(COACH_TOKEN))
        assert response.json()["findings"] == []
        assert response.json()["rules_run"] == 1  # counted, but inactive
