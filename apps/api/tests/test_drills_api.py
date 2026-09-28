"""US-J3 drills API: drill CRUD, plan fetch/generate/edit, SAF bowling blocks.

SAF (US-H1/H5): the drill-plan endpoints derive the bowling allowance and the
SafetyVerdict SERVER-SIDE from the player's real ledger/wellness rows — a
client-supplied verdict is ignored entirely and a client allowance can only
restrict, never expand, so the adversarial-review bypass (parent injecting a
fat allowance plus a forged inactive verdict while pain/ceiling is active) is
structurally closed."""

import hashlib
import uuid
from collections.abc import Iterator
from typing import Any, cast

import httpx
import pytest
from cricai_api.deps import get_db
from cricai_data.models import AuditLog, DrillPlan, Finding, SafetyConfig
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import insert, select
from sqlalchemy.orm import Session as OrmSession

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, PLAYER_TOKEN, auth

UNKNOWN_ID = "00000000-0000-0000-0000-000000000000"
PLAN_DATE = "2026-07-11"


def _player(client: TestClient, *, name: str = "Arjun") -> str:
    response = client.post(
        "/players",
        json={"name": name, "birthdate": "2014-11-20"},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 201
    return str(response.json()["id"])


def _session(client: TestClient, player_id: str) -> str:
    response = client.post(
        "/sessions",
        json={
            "player_id": player_id,
            "date": "2026-07-10",
            "session_type": "batting",
            "bowler_source": "coach",
        },
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 201
    return str(response.json()["id"])


def _drill_body(
    *,
    name: str = "Front-foot drive channel",
    intent: str = "technical",
    metric: str = "control_pct",
    machine_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "setup": "Machine to the off channel; drive along the ground.",
        "machine_settings": (
            machine_settings
            if machine_settings is not None
            else {"speed_kph": 90, "line": "outside_off", "length": "full"}
        ),
        "ball_count": 30,
        "target_metric": metric,
        "intent": intent,
    }


def _create_drill(client: TestClient, **kwargs: Any) -> httpx.Response:
    return client.post("/drills", json=_drill_body(**kwargs), headers=auth(COACH_TOKEN))


def _seed_finding(
    client: TestClient, session_id: str, *, metric: str = "control_pct", severity: str = "major"
) -> str:
    factory = client.app.state.session_factory  # type: ignore[union-attr]
    db = factory()
    try:
        finding = Finding(
            session_id=uuid.UUID(session_id),
            agent="analysis",
            rule_key=None,
            kind="zone_contrast",
            severity=severity,
            metric=metric,
            condition={"line": "outside_off", "length": "full"},
            n=20,
            effect_size=-0.3,
            confidence=0.9,
            ball_ids=[1, 2],
            evidence={},
            payload={"text_data": {"summary": "template text"}},
        )
        db.add(finding)
        db.commit()
        return str(finding.id)
    finally:
        db.close()


def _seed_safety_config(client: TestClient, config: dict[str, Any]) -> None:
    factory = client.app.state.session_factory  # type: ignore[union-attr]
    db = factory()
    try:
        db.add(SafetyConfig(version=1, config=config, approved_by="coach", reason="test seed"))
        db.commit()
    finally:
        db.close()


def _verdict(
    *codes: str, active: bool = True, text: str = "Manage the workload."
) -> dict[str, Any]:
    return {
        "active": active,
        "codes": list(codes),
        "text": text,
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
    }


def _inactive_server_verdict() -> dict[str, Any]:
    """The canonical server-derived verdict for a healthy player (US-H5)."""
    return {
        "active": False,
        "codes": [],
        "text": "",
        "sha256": hashlib.sha256(b"").hexdigest(),
    }


def _pain_checkin(client: TestClient, player_id: str, *, checkin_date: str = "2026-07-10") -> str:
    response = client.post(
        f"/wellness/{player_id}/checkins",
        json={"checkin_date": checkin_date, "pain": True},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 201
    return str(response.json()["id"])


def _ledger_entry(client: TestClient, player_id: str, *, balls: int, entry_date: str) -> None:
    response = client.post(
        f"/workload/players/{player_id}/ledger",
        json={"entry_date": entry_date, "balls": balls, "intensity": "pace_intent"},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 201


def _bowling_blocks(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [block for block in body["blocks"] if block["intent"] == "bowling"]


def _audit_actions(client: TestClient) -> set[str]:
    factory = client.app.state.session_factory  # type: ignore[union-attr]
    db = factory()
    try:
        return {row.action for row in db.scalars(select(AuditLog)).all()}
    finally:
        db.close()


class TestDrillCrud:
    def test_coach_creates_and_lists_drill(self, client: TestClient) -> None:
        created = _create_drill(client)
        assert created.status_code == 201
        body = created.json()
        assert body["name"] == "Front-foot drive channel"
        assert body["author"] == "coach"
        assert body["enabled"] is True
        assert body["intent"] == "technical"

        listed = client.get("/drills", headers=auth(PLAYER_TOKEN))
        assert listed.status_code == 200
        assert [d["id"] for d in listed.json()] == [body["id"]]
        assert "drill_create" in _audit_actions(client)

    def test_create_requires_coach_role(self, client: TestClient) -> None:
        for token in (PARENT_TOKEN, PLAYER_TOKEN):
            response = client.post("/drills", json=_drill_body(), headers=auth(token))
            assert response.status_code == 403

    def test_create_rejects_out_of_envelope_settings(self, client: TestClient) -> None:
        response = _create_drill(client, machine_settings={"speed_kph": 200})
        assert response.status_code == 422
        assert "outside the machine envelope" in response.json()["detail"]

    def test_duplicate_name_conflicts(self, client: TestClient) -> None:
        assert _create_drill(client).status_code == 201
        duplicate = _create_drill(client)
        assert duplicate.status_code == 409
        assert "already exists" in duplicate.json()["detail"]

    def test_coach_edits_drill_fields(self, client: TestClient) -> None:
        drill_id = _create_drill(client).json()["id"]
        response = client.patch(
            f"/drills/{drill_id}",
            json={"setup": "new setup", "machine_settings": {"speed_kph": 70}},
            headers=auth(COACH_TOKEN),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["setup"] == "new setup"
        assert body["machine_settings"] == {"speed_kph": 70}
        assert "drill_edit" in _audit_actions(client)

    def test_edit_ignores_explicitly_null_fields(self, client: TestClient) -> None:
        drill_id = _create_drill(client).json()["id"]
        response = client.patch(
            f"/drills/{drill_id}", json={"enabled": None}, headers=auth(COACH_TOKEN)
        )
        assert response.status_code == 200
        assert response.json()["enabled"] is True  # null means "leave unchanged"

    def test_edit_missing_drill_404(self, client: TestClient) -> None:
        response = client.patch(
            f"/drills/{UNKNOWN_ID}", json={"setup": "x"}, headers=auth(COACH_TOKEN)
        )
        assert response.status_code == 404

    def test_edit_rejects_out_of_envelope_settings(self, client: TestClient) -> None:
        drill_id = _create_drill(client).json()["id"]
        response = client.patch(
            f"/drills/{drill_id}",
            json={"machine_settings": {"line": "wide"}},
            headers=auth(COACH_TOKEN),
        )
        assert response.status_code == 422
        assert "machine line setting" in response.json()["detail"]


class TestPlanGeneration:
    def test_generate_maps_finding_to_drill_then_regenerates(self, client: TestClient) -> None:
        player_id = _player(client)
        session_id = _session(client, player_id)
        finding_id = _seed_finding(client, session_id)
        drill_id = _create_drill(client).json()["id"]

        first = client.post(
            f"/drills/plans/{player_id}/{PLAN_DATE}/generate",
            json={"session_id": session_id},
            headers=auth(COACH_TOKEN),
        )
        assert first.status_code == 201
        body = first.json()
        technical = next(b for b in body["blocks"] if b["intent"] == "technical")
        assert technical["drill_id"] == drill_id
        assert technical["finding_id"] == finding_id
        assert body["finding_ids"] == [finding_id]
        # The verdict is server-derived (healthy player: inactive, hash-verified).
        assert body["safety"] == _inactive_server_verdict()
        assert body["safety_sha256"] == _inactive_server_verdict()["sha256"]
        assert any(b["intent"] == "fun" for b in body["blocks"])
        # Healthy player, empty ledger: bowling within the server allowance.
        (bowling,) = _bowling_blocks(body)
        assert bowling["balls"] == 30  # DEFAULT_BOWLING_BALLS under a 96-ball allowance

        # Regenerating the same day upserts in place and returns 200.
        again = client.post(
            f"/drills/plans/{player_id}/{PLAN_DATE}/generate",
            json={"session_id": session_id},
            headers=auth(PARENT_TOKEN),
        )
        assert again.status_code == 200
        assert "plan_generate" in _audit_actions(client)

    def test_generate_client_values_only_restrict_and_verdict_is_ignored(
        self, client: TestClient
    ) -> None:
        """The body's allowance restricts below the server's 96; its verdict is ignored."""
        player_id = _player(client)
        session_id = _session(client, player_id)
        forged = _verdict("day_pattern_violation")
        response = client.post(
            f"/drills/plans/{player_id}/{PLAN_DATE}/generate",
            json={
                "session_id": session_id,
                "bowling_allowance_balls": 30,
                "bowling_request_balls": 12,
                "safety": forged,
            },
            headers=auth(COACH_TOKEN),
        )
        assert response.status_code == 201
        body = response.json()
        (bowling,) = _bowling_blocks(body)
        assert bowling["balls"] == 12
        assert body["safety"] == _inactive_server_verdict()  # never the client's
        assert body["safety_sha256"] == _inactive_server_verdict()["sha256"]

    def test_generate_uses_latest_safety_config_split(self, client: TestClient) -> None:
        # A stored batting_split without a fun block makes the planner refuse,
        # exercising the config-backed split path and the 422 translation.
        _seed_safety_config(
            client, {"batting_split": {"daily_balls": 100, "blocks": {"technical": 90}}}
        )
        player_id = _player(client)
        session_id = _session(client, player_id)
        response = client.post(
            f"/drills/plans/{player_id}/{PLAN_DATE}/generate",
            json={"session_id": session_id},
            headers=auth(COACH_TOKEN),
        )
        assert response.status_code == 422
        assert "fun block" in response.json()["detail"]

    def test_generate_unknown_player_404(self, client: TestClient) -> None:
        response = client.post(
            f"/drills/plans/{UNKNOWN_ID}/{PLAN_DATE}/generate",
            json={"session_id": UNKNOWN_ID},
            headers=auth(COACH_TOKEN),
        )
        assert response.status_code == 404
        assert "player not found" in response.json()["detail"]

    def test_generate_unknown_session_404(self, client: TestClient) -> None:
        player_id = _player(client)
        response = client.post(
            f"/drills/plans/{player_id}/{PLAN_DATE}/generate",
            json={"session_id": UNKNOWN_ID},
            headers=auth(COACH_TOKEN),
        )
        assert response.status_code == 404
        assert "session not found" in response.json()["detail"]

    def test_generate_rejects_session_of_another_player(self, client: TestClient) -> None:
        owner = _player(client, name="Owner")
        other = _player(client, name="Other")
        session_id = _session(client, owner)
        response = client.post(
            f"/drills/plans/{other}/{PLAN_DATE}/generate",
            json={"session_id": session_id},
            headers=auth(COACH_TOKEN),
        )
        assert response.status_code == 422
        assert "another player" in response.json()["detail"]

    def test_generate_requires_guardian_or_coach(self, client: TestClient) -> None:
        player_id = _player(client)
        session_id = _session(client, player_id)
        response = client.post(
            f"/drills/plans/{player_id}/{PLAN_DATE}/generate",
            json={"session_id": session_id},
            headers=auth(PLAYER_TOKEN),
        )
        assert response.status_code == 403


class TestPlanFetch:
    def test_get_plan_after_generation(self, client: TestClient) -> None:
        player_id = _player(client)
        session_id = _session(client, player_id)
        client.post(
            f"/drills/plans/{player_id}/{PLAN_DATE}/generate",
            json={"session_id": session_id},
            headers=auth(COACH_TOKEN),
        )
        response = client.get(f"/drills/plans/{player_id}/{PLAN_DATE}", headers=auth(PLAYER_TOKEN))
        assert response.status_code == 200
        assert response.json()["plan_date"] == PLAN_DATE

    def test_get_plan_unknown_player_404(self, client: TestClient) -> None:
        response = client.get(f"/drills/plans/{UNKNOWN_ID}/{PLAN_DATE}", headers=auth(PARENT_TOKEN))
        assert response.status_code == 404
        assert "player not found" in response.json()["detail"]

    def test_get_plan_missing_404(self, client: TestClient) -> None:
        player_id = _player(client)
        response = client.get(f"/drills/plans/{player_id}/{PLAN_DATE}", headers=auth(PARENT_TOKEN))
        assert response.status_code == 404
        assert "no plan" in response.json()["detail"]


def _blocks(*, with_bowling: int = 0) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {
            "intent": "technical",
            "balls": 100,
            "drill_id": "d-1",
            "machine_settings": {"speed_kph": 90},
            "success_metric": "control_pct",
            "finding_id": "f-1",
        },
        {
            "intent": "fun",
            "balls": 50,
            "drill_id": None,
            "machine_settings": {},
            "success_metric": "enjoyment",
            "finding_id": None,
        },
    ]
    if with_bowling:
        blocks.append(
            {
                "intent": "bowling",
                "balls": with_bowling,
                "drill_id": None,
                "machine_settings": {},
                "success_metric": "landing_accuracy_pct",
                "finding_id": "maintenance",
            }
        )
    return blocks


class TestPlanEdit:
    def test_coach_edits_then_replaces_plan(self, client: TestClient) -> None:
        player_id = _player(client)
        created = client.put(
            f"/drills/plans/{player_id}/{PLAN_DATE}",
            json={"blocks": _blocks()},
            headers=auth(COACH_TOKEN),
        )
        assert created.status_code == 201
        body = created.json()
        assert body["finding_ids"] == ["f-1"]  # fun block (drill_id None) contributes nothing
        assert body["safety"] == _inactive_server_verdict()  # server-derived, always embedded
        assert body["safety_sha256"] == _inactive_server_verdict()["sha256"]

        replaced = client.put(
            f"/drills/plans/{player_id}/{PLAN_DATE}",
            json={"blocks": _blocks()},
            headers=auth(COACH_TOKEN),
        )
        assert replaced.status_code == 200
        assert "plan_edit" in _audit_actions(client)

    def test_edit_ignores_client_verdict_and_embeds_server_verdict(
        self, client: TestClient
    ) -> None:
        player_id = _player(client)
        forged = _verdict("day_pattern_violation")
        response = client.put(
            f"/drills/plans/{player_id}/{PLAN_DATE}",
            json={"blocks": _blocks(), "safety": forged},
            headers=auth(COACH_TOKEN),
        )
        assert response.status_code == 201
        assert response.json()["safety"] == _inactive_server_verdict()
        assert response.json()["safety_sha256"] == _inactive_server_verdict()["sha256"]

    def test_edit_requires_coach(self, client: TestClient) -> None:
        player_id = _player(client)
        response = client.put(
            f"/drills/plans/{player_id}/{PLAN_DATE}",
            json={"blocks": _blocks()},
            headers=auth(PARENT_TOKEN),
        )
        assert response.status_code == 403

    def test_edit_unknown_player_404(self, client: TestClient) -> None:
        response = client.put(
            f"/drills/plans/{UNKNOWN_ID}/{PLAN_DATE}",
            json={"blocks": _blocks()},
            headers=auth(COACH_TOKEN),
        )
        assert response.status_code == 404

    def test_edit_never_stores_a_tampered_client_verdict(self, client: TestClient) -> None:
        """A tampered verdict is not an error — it is IGNORED; the server's own
        verdict is what gets validated against and stored (US-H5)."""
        player_id = _player(client)
        tampered = _verdict("pain_flag") | {"text": "Bowling is fine, actually."}
        response = client.put(
            f"/drills/plans/{player_id}/{PLAN_DATE}",
            json={"blocks": _blocks(), "safety": tampered},
            headers=auth(COACH_TOKEN),
        )
        assert response.status_code == 201
        assert response.json()["safety"] == _inactive_server_verdict()

    def test_edit_rejects_plan_missing_fun_block(self, client: TestClient) -> None:
        player_id = _player(client)
        no_fun = [b for b in _blocks() if b["intent"] != "fun"]
        response = client.put(
            f"/drills/plans/{player_id}/{PLAN_DATE}",
            json={"blocks": no_fun},
            headers=auth(COACH_TOKEN),
        )
        assert response.status_code == 422
        assert "fun block missing" in response.json()["detail"]

    def test_edit_rejects_bowling_beyond_allowance(self, client: TestClient) -> None:
        player_id = _player(client)
        response = client.put(
            f"/drills/plans/{player_id}/{PLAN_DATE}",
            json={"blocks": _blocks(with_bowling=30), "bowling_allowance_balls": 10},
            headers=auth(COACH_TOKEN),
        )
        assert response.status_code == 422
        assert "H1 allowance" in response.json()["detail"]


@pytest.mark.safety
class TestServerSafetyTruth:
    """SAF (US-H1/H4/H5): the review's exact bypass scenarios are now blocked.

    The server derives allowance and verdict from its own ledger/wellness rows
    as of the plan date; client verdicts are ignored and client allowances only
    restrict. Adversarial-review fix — overrides the phase-5 plan's "injected
    safety inputs" scope decision for these endpoints.
    """

    def test_parent_cannot_store_bowling_with_forged_verdict_while_pain_active(
        self, client: TestClient
    ) -> None:
        """The review's generate bypass: pain active, allowance=600, forged
        inactive verdict — the stored plan must carry ZERO bowling and the
        server's ACTIVE pain verdict."""
        player_id = _player(client)
        session_id = _session(client, player_id)
        _pain_checkin(client, player_id)
        forged = _verdict(active=False, text="x")
        response = client.post(
            f"/drills/plans/{player_id}/{PLAN_DATE}/generate",
            json={
                "session_id": session_id,
                "bowling_allowance_balls": 600,
                "bowling_request_balls": 48,
                "safety": forged,
            },
            headers=auth(PARENT_TOKEN),
        )
        assert response.status_code == 201
        body = response.json()
        assert _bowling_blocks(body) == []
        assert body["safety"]["active"] is True
        assert body["safety"]["codes"] == ["pain_flag"]
        text: str = body["safety"]["text"]
        assert body["safety"]["sha256"] == hashlib.sha256(text.encode()).hexdigest()
        stored = client.get(f"/drills/plans/{player_id}/{PLAN_DATE}", headers=auth(COACH_TOKEN))
        assert _bowling_blocks(stored.json()) == []
        assert stored.json()["safety"]["codes"] == ["pain_flag"]

    def test_coach_put_bowling_while_pain_active_is_422_never_stored(
        self, client: TestClient
    ) -> None:
        """The review's PUT bypass: explicit bowling blocks while the server's
        own wellness state says pain is active are rejected, never stored."""
        player_id = _player(client)
        _pain_checkin(client, player_id)
        response = client.put(
            f"/drills/plans/{player_id}/{PLAN_DATE}",
            json={
                "blocks": _blocks(with_bowling=30),
                "bowling_allowance_balls": 600,
                "safety": _verdict(active=False, text="x"),
            },
            headers=auth(COACH_TOKEN),
        )
        assert response.status_code == 422
        assert "H1 allowance" in response.json()["detail"]
        fetched = client.get(f"/drills/plans/{player_id}/{PLAN_DATE}", headers=auth(COACH_TOKEN))
        assert fetched.status_code == 404  # nothing was stored

    def test_put_bowling_while_ceiling_blown_is_422(self, client: TestClient) -> None:
        player_id = _player(client)
        _ledger_entry(client, player_id, balls=96, entry_date="2026-07-10")  # 16 overs
        response = client.put(
            f"/drills/plans/{player_id}/{PLAN_DATE}",
            json={"blocks": _blocks(with_bowling=6), "bowling_allowance_balls": 600},
            headers=auth(COACH_TOKEN),
        )
        assert response.status_code == 422
        assert "H1 allowance" in response.json()["detail"]

    def test_generate_caps_bowling_at_the_server_remaining_allowance(
        self, client: TestClient
    ) -> None:
        player_id = _player(client)
        session_id = _session(client, player_id)
        _ledger_entry(client, player_id, balls=90, entry_date="2026-07-09")  # 15 of 16 overs
        response = client.post(
            f"/drills/plans/{player_id}/{PLAN_DATE}/generate",
            json={
                "session_id": session_id,
                "bowling_allowance_balls": 600,
                "bowling_request_balls": 600,
            },
            headers=auth(PARENT_TOKEN),
        )
        assert response.status_code == 201
        (bowling,) = _bowling_blocks(response.json())
        assert bowling["balls"] == 6  # 96-ball ceiling minus the 90 already bowled

    def test_adult_clearance_restores_the_bowling_allowance(self, client: TestClient) -> None:
        player_id = _player(client)
        session_id = _session(client, player_id)
        checkin_id = _pain_checkin(client, player_id)
        client.post(
            f"/wellness/{player_id}/checkins/{checkin_id}/clearance",
            json={"note": "physio cleared"},
            headers=auth(COACH_TOKEN),
        )
        response = client.post(
            f"/drills/plans/{player_id}/{PLAN_DATE}/generate",
            json={"session_id": session_id, "bowling_request_balls": 12},
            headers=auth(PARENT_TOKEN),
        )
        assert response.status_code == 201
        body = response.json()
        (bowling,) = _bowling_blocks(body)
        assert bowling["balls"] == 12
        assert body["safety"] == _inactive_server_verdict()

    def test_no_band_ceiling_means_no_machine_derived_allowance(self, client: TestClient) -> None:
        """A player above every configured age band has no ceiling; the server
        allowance is then conservatively zero (mirrors the worker planner)."""
        created = client.post(
            "/players",
            json={"name": "Older", "birthdate": "2005-01-01"},
            headers=auth(PARENT_TOKEN),
        )
        older_id = str(created.json()["id"])
        session = client.post(
            "/sessions",
            json={
                "player_id": older_id,
                "date": "2026-07-10",
                "session_type": "batting",
                "bowler_source": "coach",
            },
            headers=auth(PARENT_TOKEN),
        )
        response = client.post(
            f"/drills/plans/{older_id}/{PLAN_DATE}/generate",
            json={"session_id": str(session.json()["id"]), "bowling_request_balls": 30},
            headers=auth(COACH_TOKEN),
        )
        assert response.status_code == 201
        assert _bowling_blocks(response.json()) == []


def test_generate_plan_upsert_race_is_409(client: TestClient) -> None:
    """A concurrent writer (the pipeline planner stage or another request) wins
    the (player_id, plan_date) uq race between _upsert_plan's select-miss and its
    INSERT flush; the loser gets a clear 409, not an opaque 500 (finding 6, API
    half). Mirrors the workload safety-config version-race test."""
    player_id = _player(client)
    session_id = _session(client, player_id)
    _seed_finding(client, session_id)
    _create_drill(client)

    app = cast(FastAPI, client.app)
    factory = app.state.session_factory

    def racing_get_db() -> Iterator[OrmSession]:
        """A competing transaction commits the SAME (player_id, plan_date) plan
        between this request's select-miss and its INSERT flush."""
        db: OrmSession = factory()
        real_flush = db.flush

        def flush(objects: Any = None) -> None:
            pending = [obj for obj in db.new if isinstance(obj, DrillPlan)]
            if pending:
                db.flush = real_flush  # type: ignore[method-assign]  # race only the first INSERT
                target = pending[0]
                with db.no_autoflush:
                    db.execute(
                        insert(DrillPlan).values(
                            player_id=target.player_id,
                            plan_date=target.plan_date,
                            blocks=[],
                            finding_ids=[],
                            safety={},
                            safety_sha256=None,
                            created_by="other",
                        )
                    )
            real_flush(objects)

        db.flush = flush  # type: ignore[method-assign]
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    app.dependency_overrides[get_db] = racing_get_db
    try:
        response = client.post(
            f"/drills/plans/{player_id}/{PLAN_DATE}/generate",
            json={"session_id": session_id},
            headers=auth(COACH_TOKEN),
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 409
    assert response.json()["detail"] == "plan already exists for this date, retry"
    # The losing request rolled back cleanly: no plan_generate audit row survives.
    assert "plan_generate" not in _audit_actions(client)
