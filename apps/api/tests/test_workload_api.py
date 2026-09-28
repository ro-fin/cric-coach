"""US-H1/H2 API: ledger CRUD with audits, rolling-7 summaries, config approval gate."""

import copy
import hashlib
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from cricai_api.deps import get_db
from cricai_coaching.safety_config import DEFAULT_SAFETY_CONFIG
from cricai_coaching.workload import SOURCE_AUTO_BACKFILL
from cricai_data.enums import DeliveryIntensity
from cricai_data.models import AuditLog, BowlingLedgerEntry, SafetyConfig
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import insert, select
from sqlalchemy.orm import Session as OrmSession

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, PLAYER_TOKEN, auth, make_test_app

#: Age 11 on WINDOW_END; turns 12 on 2026-07-15.
BIRTHDATE = "2014-07-15"
WINDOW_END = "2026-07-10"


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    return make_test_app(tmp_path)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _create_player(client: TestClient, birthdate: str = BIRTHDATE) -> str:
    response = client.post(
        "/players",
        json={"name": "Arjun", "birthdate": birthdate},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 201
    player_id: str = response.json()["id"]
    return player_id


def _create_session(client: TestClient, player_id: str) -> str:
    response = client.post(
        "/sessions",
        json={
            "player_id": player_id,
            "date": WINDOW_END,
            "session_type": "bowling",
            "bowler_source": "human",
        },
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 201
    session_id: str = response.json()["id"]
    return session_id


def _create_entry(
    client: TestClient,
    player_id: str,
    *,
    entry_date: str = WINDOW_END,
    balls: int = 6,
    intensity: str = "pace_intent",
    token: str = PARENT_TOKEN,
    **extra: Any,
) -> Any:
    return client.post(
        f"/workload/players/{player_id}/ledger",
        json={"entry_date": entry_date, "balls": balls, "intensity": intensity, **extra},
        headers=auth(token),
    )


def _audit_actions(app: FastAPI, action: str) -> list[AuditLog]:
    with app.state.session_factory() as db:
        return list(db.scalars(select(AuditLog).where(AuditLog.action == action)))


def _seed_auto_entry(app: FastAPI, player_id: str) -> str:
    with app.state.session_factory() as db:
        entry = BowlingLedgerEntry(
            player_id=uuid.UUID(player_id),
            entry_date=date(2026, 7, 9),
            balls=12,
            intensity=DeliveryIntensity.PACE_INTENT,
            source=SOURCE_AUTO_BACKFILL,
            created_by="backfill_workload",
        )
        db.add(entry)
        db.commit()
        return str(entry.id)


def test_create_manual_entry_writes_audit(app: FastAPI, client: TestClient) -> None:
    player_id = _create_player(client)
    response = _create_entry(client, player_id, note="net session at club")
    assert response.status_code == 201
    body = response.json()
    assert body["player_id"] == player_id
    assert body["balls"] == 6
    assert body["intensity"] == "pace_intent"
    assert body["source"] == "manual"
    assert body["created_by"] == "parent"
    assert body["note"] == "net session at club"
    assert body["session_id"] is None

    (audit,) = _audit_actions(app, "ledger_create")
    assert audit.entity == "bowling_ledger_entry"
    assert audit.entity_id == body["id"]
    assert audit.detail is not None
    assert audit.detail["old"] is None
    assert audit.detail["new"]["balls"] == 6
    assert audit.detail["new"]["source"] == "manual"


def test_create_entry_roles(client: TestClient) -> None:
    player_id = _create_player(client)
    assert _create_entry(client, player_id, token=COACH_TOKEN).status_code == 201
    assert _create_entry(client, player_id, token=PLAYER_TOKEN).status_code == 403


def test_create_entry_unknown_player_404(client: TestClient) -> None:
    assert _create_entry(client, str(uuid.uuid4())).status_code == 404


def test_create_entry_session_linking(client: TestClient) -> None:
    player_id = _create_player(client)
    session_id = _create_session(client, player_id)
    linked = _create_entry(client, player_id, session_id=session_id)
    assert linked.status_code == 201
    assert linked.json()["session_id"] == session_id

    missing = _create_entry(client, player_id, session_id=str(uuid.uuid4()))
    assert missing.status_code == 404

    other_player = _create_player(client)
    mismatched = _create_entry(client, other_player, session_id=session_id)
    assert mismatched.status_code == 409


def test_create_entry_rejects_zero_balls(client: TestClient) -> None:
    player_id = _create_player(client)
    assert _create_entry(client, player_id, balls=0).status_code == 422


def test_list_entries_with_range_filters(client: TestClient) -> None:
    player_id = _create_player(client)
    for entry_date in ("2026-07-06", "2026-07-08", "2026-07-10"):
        assert _create_entry(client, player_id, entry_date=entry_date).status_code == 201

    url = f"/workload/players/{player_id}/ledger"
    everything = client.get(url, headers=auth(PLAYER_TOKEN))  # player may read
    assert everything.status_code == 200
    assert [entry["entry_date"] for entry in everything.json()] == [
        "2026-07-06",
        "2026-07-08",
        "2026-07-10",
    ]
    since = client.get(url, params={"start": "2026-07-08"}, headers=auth(PARENT_TOKEN))
    assert [entry["entry_date"] for entry in since.json()] == ["2026-07-08", "2026-07-10"]
    until = client.get(url, params={"end": "2026-07-08"}, headers=auth(PARENT_TOKEN))
    assert [entry["entry_date"] for entry in until.json()] == ["2026-07-06", "2026-07-08"]
    both = client.get(
        url,
        params={"start": "2026-07-07", "end": "2026-07-09"},
        headers=auth(PARENT_TOKEN),
    )
    assert [entry["entry_date"] for entry in both.json()] == ["2026-07-08"]


def test_list_entries_unknown_player_404(client: TestClient) -> None:
    response = client.get(f"/workload/players/{uuid.uuid4()}/ledger", headers=auth(PARENT_TOKEN))
    assert response.status_code == 404


def test_delete_manual_entry_audited(app: FastAPI, client: TestClient) -> None:
    player_id = _create_player(client)
    entry_id = _create_entry(client, player_id).json()["id"]

    response = client.delete(f"/workload/ledger/{entry_id}", headers=auth(COACH_TOKEN))
    assert response.status_code == 204
    listed = client.get(f"/workload/players/{player_id}/ledger", headers=auth(PARENT_TOKEN))
    assert listed.json() == []

    (audit,) = _audit_actions(app, "ledger_delete")
    assert audit.actor == "coach"
    assert audit.entity_id == entry_id
    assert audit.detail is not None
    assert audit.detail["new"] is None
    assert audit.detail["old"]["balls"] == 6


def test_delete_auto_entry_409(app: FastAPI, client: TestClient) -> None:
    player_id = _create_player(client)
    auto_id = _seed_auto_entry(app, player_id)
    response = client.delete(f"/workload/ledger/{auto_id}", headers=auth(PARENT_TOKEN))
    assert response.status_code == 409
    assert "only manual entries" in response.json()["detail"]


def test_delete_entry_404_and_role_guard(client: TestClient) -> None:
    assert (
        client.delete(f"/workload/ledger/{uuid.uuid4()}", headers=auth(PARENT_TOKEN)).status_code
        == 404
    )
    assert (
        client.delete(f"/workload/ledger/{uuid.uuid4()}", headers=auth(PLAYER_TOKEN)).status_code
        == 403
    )


def test_summary_windows_violations_and_allowance(client: TestClient) -> None:
    """US-H1: the summary shows weighted overs, violations and remaining balls."""
    player_id = _create_player(client)
    # 15 overs across three non-consecutive days, then one more over today.
    for entry_date, balls in (("2026-07-04", 36), ("2026-07-06", 30), ("2026-07-08", 24)):
        created = _create_entry(client, player_id, entry_date=entry_date, balls=balls)
        assert created.status_code == 201

    url = f"/workload/players/{player_id}/summary"
    before = client.get(url, params={"end": WINDOW_END}, headers=auth(PLAYER_TOKEN))
    assert before.status_code == 200
    (window,) = before.json()
    assert window["window_start"] == "2026-07-04"
    assert window["window_end"] == WINDOW_END
    assert window["weighted_overs"] == 15.0
    assert window["band_max_age"] == 11
    assert window["ceiling_overs"] == 16.0
    assert window["violations"] == []
    assert window["remaining_balls"] == 6

    assert _create_entry(client, player_id, entry_date=WINDOW_END, balls=6).status_code == 201
    after = client.get(url, params={"end": WINDOW_END}, headers=auth(PARENT_TOKEN))
    (window,) = after.json()
    assert window["weighted_overs"] == 16.0
    assert window["violations"] == ["workload_ceiling"]
    assert window["remaining_balls"] == 0
    assert window["bowling_days"] == ["2026-07-04", "2026-07-06", "2026-07-08", WINDOW_END]


def test_summary_multiple_windows_newest_first(client: TestClient) -> None:
    player_id = _create_player(client)
    response = client.get(
        f"/workload/players/{player_id}/summary",
        params={"end": WINDOW_END, "days": 3},
        headers=auth(COACH_TOKEN),
    )
    ends = [window["window_end"] for window in response.json()]
    assert ends == ["2026-07-10", "2026-07-09", "2026-07-08"]


def test_summary_defaults_to_today(client: TestClient) -> None:
    player_id = _create_player(client)
    today = datetime.now(tz=UTC).date()
    assert _create_entry(client, player_id, entry_date=today.isoformat()).status_code == 201
    response = client.get(f"/workload/players/{player_id}/summary", headers=auth(PARENT_TOKEN))
    (window,) = response.json()
    assert window["window_end"] == today.isoformat()
    assert window["weighted_balls"] == 6.0


def test_summary_unknown_player_404(client: TestClient) -> None:
    response = client.get(f"/workload/players/{uuid.uuid4()}/summary", headers=auth(PARENT_TOKEN))
    assert response.status_code == 404


def test_summary_uses_latest_config_version(client: TestClient) -> None:
    player_id = _create_player(client)
    lowered = copy.deepcopy(DEFAULT_SAFETY_CONFIG)
    lowered["workload"]["age_bands"][0]["weekly_overs_ceiling"] = 10
    posted = client.post(
        "/workload/safety-config",
        json={"config": lowered, "reason": "pre-season load reduction"},
        headers=auth(PARENT_TOKEN),
    )
    assert posted.status_code == 201

    response = client.get(
        f"/workload/players/{player_id}/summary",
        params={"end": WINDOW_END},
        headers=auth(PARENT_TOKEN),
    )
    (window,) = response.json()
    assert window["ceiling_overs"] == 10.0
    assert window["remaining_balls"] == 60


def test_get_safety_config_unseeded_serves_canonical_defaults(client: TestClient) -> None:
    response = client.get("/workload/safety-config", headers=auth(PARENT_TOKEN))
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 0
    assert body["config"] == DEFAULT_SAFETY_CONFIG
    assert body["approved_by"] == "seed"
    assert body["created_at"] is None

    versions = client.get("/workload/safety-config/versions", headers=auth(COACH_TOKEN))
    assert versions.status_code == 200
    assert versions.json() == []


def test_post_config_parent_may_lower(app: FastAPI, client: TestClient) -> None:
    lowered = copy.deepcopy(DEFAULT_SAFETY_CONFIG)
    lowered["workload"]["age_bands"][0]["weekly_overs_ceiling"] = 14
    response = client.post(
        "/workload/safety-config",
        json={"config": lowered, "reason": "coach advised lighter week"},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["version"] == 1
    assert body["approved_by"] == "parent"

    latest = client.get("/workload/safety-config", headers=auth(PARENT_TOKEN)).json()
    assert latest["version"] == 1
    assert latest["config"]["workload"]["age_bands"][0]["weekly_overs_ceiling"] == 14
    versions = client.get("/workload/safety-config/versions", headers=auth(PARENT_TOKEN))
    assert [item["version"] for item in versions.json()] == [1]

    (audit,) = _audit_actions(app, "safety_config_change")
    assert audit.detail is not None
    assert audit.detail["old_version"] == 0
    assert audit.detail["new_version"] == 1
    assert audit.detail["ceiling_raises"] == []
    assert audit.detail["old_config"] == DEFAULT_SAFETY_CONFIG
    assert audit.detail["new_config"] == lowered


def test_post_config_parent_raising_ceiling_403(app: FastAPI, client: TestClient) -> None:
    """The first version is compared against canonical defaults: no smuggling."""
    raised = copy.deepcopy(DEFAULT_SAFETY_CONFIG)
    raised["workload"]["age_bands"][0]["weekly_overs_ceiling"] = 18
    response = client.post(
        "/workload/safety-config",
        json={"config": raised, "reason": "he feels fine"},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 403
    assert "requires coach approval" in response.json()["detail"]
    assert client.get("/workload/safety-config/versions", headers=auth(PARENT_TOKEN)).json() == []
    assert _audit_actions(app, "safety_config_change") == []


def test_post_config_coach_may_raise_with_audit(app: FastAPI, client: TestClient) -> None:
    raised = copy.deepcopy(DEFAULT_SAFETY_CONFIG)
    raised["workload"]["age_bands"][0]["weekly_overs_ceiling"] = 18
    response = client.post(
        "/workload/safety-config",
        json={"config": raised, "reason": "growth plates reviewed; cleared"},
        headers=auth(COACH_TOKEN),
    )
    assert response.status_code == 201
    assert response.json()["approved_by"] == "coach"

    (audit,) = _audit_actions(app, "safety_config_change")
    assert audit.detail is not None
    assert audit.detail["ceiling_raises"] != []
    assert "weekly_overs_ceiling: 16 -> 18" in audit.detail["ceiling_raises"][0]


def test_post_config_compares_against_previous_version_not_defaults(
    client: TestClient,
) -> None:
    lowered = copy.deepcopy(DEFAULT_SAFETY_CONFIG)
    lowered["workload"]["age_bands"][0]["weekly_overs_ceiling"] = 14
    assert (
        client.post(
            "/workload/safety-config",
            json={"config": lowered, "reason": "lighter week"},
            headers=auth(PARENT_TOKEN),
        ).status_code
        == 201
    )
    # Restoring the original 16 is a RAISE relative to v1 — parent blocked.
    response = client.post(
        "/workload/safety-config",
        json={"config": copy.deepcopy(DEFAULT_SAFETY_CONFIG), "reason": "back to normal"},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 403

    restored = client.post(
        "/workload/safety-config",
        json={"config": copy.deepcopy(DEFAULT_SAFETY_CONFIG), "reason": "back to normal"},
        headers=auth(COACH_TOKEN),
    )
    assert restored.status_code == 201
    assert restored.json()["version"] == 2


def test_post_config_invalid_shape_422(client: TestClient) -> None:
    broken = copy.deepcopy(DEFAULT_SAFETY_CONFIG)
    del broken["wellness"]
    response = client.post(
        "/workload/safety-config",
        json={"config": broken, "reason": "oops"},
        headers=auth(COACH_TOKEN),
    )
    assert response.status_code == 422
    assert "wellness: missing" in response.json()["detail"]


def test_post_config_non_numeric_limit_422(client: TestClient) -> None:
    broken = copy.deepcopy(DEFAULT_SAFETY_CONFIG)
    broken["workload"]["balls_per_over"] = None
    response = client.post(
        "/workload/safety-config",
        json={"config": broken, "reason": "oops"},
        headers=auth(COACH_TOKEN),
    )
    assert response.status_code == 422
    assert "invalid safety config" in response.json()["detail"]


def test_post_config_role_and_reason_guards(client: TestClient) -> None:
    payload = {"config": copy.deepcopy(DEFAULT_SAFETY_CONFIG), "reason": "x"}
    assert (
        client.post("/workload/safety-config", json=payload, headers=auth(PLAYER_TOKEN)).status_code
        == 403
    )
    empty_reason = {"config": copy.deepcopy(DEFAULT_SAFETY_CONFIG), "reason": ""}
    assert (
        client.post(
            "/workload/safety-config", json=empty_reason, headers=auth(PARENT_TOKEN)
        ).status_code
        == 422
    )


def test_post_config_version_race_is_409(app: FastAPI, client: TestClient) -> None:
    """Two concurrent appends compute the same next version; the loser of the
    unique(version) race gets a clear 409, not an opaque 500 (and no audit row)."""

    factory = app.state.session_factory

    def racing_get_db() -> Iterator[OrmSession]:
        """A competing transaction commits the SAME next version between this
        request's version read and its INSERT flush."""
        db: OrmSession = factory()
        real_flush = db.flush

        def flush(objects: Any = None) -> None:
            if any(isinstance(obj, SafetyConfig) for obj in db.new):
                db.flush = real_flush  # type: ignore[method-assign]
                with db.no_autoflush:
                    db.execute(
                        insert(SafetyConfig).values(
                            version=1, config={}, approved_by="other", reason="race"
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
            "/workload/safety-config",
            json={"config": copy.deepcopy(DEFAULT_SAFETY_CONFIG), "reason": "lighter week"},
            headers=auth(COACH_TOKEN),
        )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 409
    assert response.json()["detail"] == "config version conflict, retry"
    # The whole request rolled back: no surviving version, no audit row.
    assert client.get("/workload/safety-config/versions", headers=auth(PARENT_TOKEN)).json() == []
    assert _audit_actions(app, "safety_config_change") == []


def test_ledger_audit_hashes_note_not_verbatim(app: FastAPI, client: TestClient) -> None:
    """The free-text ledger note is health PII: the audit stores a sha256 and a
    presence flag, never the verbatim note (US-L3 privacy round-trip)."""
    player_id = _create_player(client)
    note = "sore left shoulder, spin only this week"
    assert _create_entry(client, player_id, note=note).status_code == 201

    (audit,) = _audit_actions(app, "ledger_create")
    assert audit.detail is not None
    new = audit.detail["new"]
    assert "note" not in new
    assert new["note_present"] is True
    assert new["note_sha256"] == hashlib.sha256(note.encode("utf-8")).hexdigest()

    # An entry with no note records absence, not an empty-string hash.
    other = _create_player(client)
    entry_id = _create_entry(client, other).json()["id"]
    assert (
        client.delete(f"/workload/ledger/{entry_id}", headers=auth(COACH_TOKEN)).status_code == 204
    )
    (deleted,) = _audit_actions(app, "ledger_delete")
    assert deleted.detail is not None
    assert deleted.detail["old"]["note_present"] is False
    assert deleted.detail["old"]["note_sha256"] is None
