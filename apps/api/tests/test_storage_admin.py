"""US-B6 acceptance: usage samples, forecast, retention with derived protections, backups."""

import datetime
import uuid
from pathlib import Path

import pytest
from cricai_data.enums import (
    BowlerSource,
    Contact,
    Footwork,
    Length,
    Line,
    Outcome,
    SessionType,
    Shot,
)
from cricai_data.models import (
    AuditLog,
    BallEvent,
    BallTag,
    EventCorrection,
    Player,
    Session,
    StorageUsageSample,
)
from cricai_data.storage import FsObjectStore, sha256_hex
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, PLAYER_TOKEN, auth, make_test_app

RAW_KEY = "sessions/s1/C1/full.mp4"
CLIP_KEY = "sessions/s1/balls/12/C1.mp4"


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    return make_test_app(tmp_path)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _store(app: FastAPI) -> FsObjectStore:
    store: FsObjectStore = app.state.store
    return store


def _audit_rows(app: FastAPI) -> list[AuditLog]:
    with app.state.session_factory() as db:
        return list(db.scalars(select(AuditLog)).all())


def _seed_tagged_session(app: FastAPI, *, ground_truth_eligible: bool = True) -> uuid.UUID:
    """Session with one ball tag (ball_no=1); returns the session id."""
    with app.state.session_factory() as db:
        player = Player(name="Arjun", birthdate=datetime.date(2014, 11, 20))
        session = Session(
            player=player,
            session_date=datetime.date(2026, 7, 7),
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
        )
        db.add_all([player, session])
        db.flush()
        db.add(
            BallTag(
                session_id=session.id,
                ball_no=1,
                line=Line.OFF,
                length=Length.GOOD,
                shot=Shot.DRIVE,
                footwork=Footwork.FRONT,
                contact=Contact.MIDDLE,
                outcome=Outcome.CONTROLLED_GROUND_SHOT,
                control=True,
                ground_truth_eligible=ground_truth_eligible,
                created_by="parent",
            )
        )
        session_id = session.id
        db.commit()
    return session_id


def _seed_event_with_correction(app: FastAPI, session_id: uuid.UUID) -> None:
    """One ball event carrying a human correction — US-D4 ground truth."""
    with app.state.session_factory() as db:
        event = BallEvent(
            session_id=session_id,
            ball_no=1,
            start_ms=1000,
            release_ms=1500,
            contact_ms=2000,
            end_ms=3000,
            confidence=0.9,
        )
        db.add(event)
        db.flush()
        db.add(
            EventCorrection(
                event_id=event.id,
                action="adjust",
                before={"start_ms": 900},
                after={"start_ms": 1000},
                actor="coach",
            )
        )
        db.commit()


# -- usage samples ---------------------------------------------------------------


def test_record_usage_sample_with_explicit_bytes(app: FastAPI, client: TestClient) -> None:
    body = {"sampled_on": "2026-07-07", "bytes_used": 123}
    response = client.post("/storage/usage-samples", json=body, headers=auth(PARENT_TOKEN))
    assert response.status_code == 200
    assert response.json() == {"sampled_on": "2026-07-07", "bytes_used": 123}
    with app.state.session_factory() as db:
        rows = db.scalars(select(StorageUsageSample)).all()
        assert [(r.sampled_on, r.bytes_used) for r in rows] == [(datetime.date(2026, 7, 7), 123)]


def test_record_usage_sample_defaults_to_measured_store_usage(
    app: FastAPI, client: TestClient
) -> None:
    store = _store(app)
    store.put(RAW_KEY, b"x" * 300)
    store.put("backups/db.sql", b"x" * 999)  # outside sessions/: not counted

    body = {"sampled_on": "2026-07-07"}
    response = client.post("/storage/usage-samples", json=body, headers=auth(PARENT_TOKEN))
    assert response.status_code == 200
    assert response.json() == {"sampled_on": "2026-07-07", "bytes_used": 300}


def test_record_usage_sample_upserts_on_date(app: FastAPI, client: TestClient) -> None:
    first = {"sampled_on": "2026-07-07", "bytes_used": 100}
    second = {"sampled_on": "2026-07-07", "bytes_used": 250}
    assert (
        client.post("/storage/usage-samples", json=first, headers=auth(PARENT_TOKEN)).status_code
        == 200
    )
    response = client.post("/storage/usage-samples", json=second, headers=auth(PARENT_TOKEN))
    assert response.status_code == 200
    assert response.json() == {"sampled_on": "2026-07-07", "bytes_used": 250}
    with app.state.session_factory() as db:
        rows = db.scalars(select(StorageUsageSample)).all()
        assert [(r.sampled_on, r.bytes_used) for r in rows] == [(datetime.date(2026, 7, 7), 250)]


def test_record_usage_sample_rejects_negative_bytes(client: TestClient) -> None:
    body = {"sampled_on": "2026-07-07", "bytes_used": -1}
    response = client.post("/storage/usage-samples", json=body, headers=auth(PARENT_TOKEN))
    assert response.status_code == 422


# -- forecast ------------------------------------------------------------------


def _post_sample(client: TestClient, sampled_on: str, bytes_used: int) -> None:
    body = {"sampled_on": sampled_on, "bytes_used": bytes_used}
    response = client.post("/storage/usage-samples", json=body, headers=auth(PARENT_TOKEN))
    assert response.status_code == 200


def test_forecast_from_recorded_history_uses_date_ordinal_deltas(client: TestClient) -> None:
    # Perfect 100 B/day line with a weekend gap: indices 0, 1, 3.
    _post_sample(client, "2026-07-01", 100)
    _post_sample(client, "2026-07-02", 200)
    _post_sample(client, "2026-07-04", 400)

    response = client.get(
        "/storage/forecast", params={"capacity_bytes": 1_000}, headers=auth(PARENT_TOKEN)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["bytes_used"] == 400
    assert body["capacity_bytes"] == 1_000
    assert body["daily_rate_bytes"] == pytest.approx(100.0)
    assert body["days_remaining"] == pytest.approx(6.0)
    assert body["insufficient_history"] is False


def test_forecast_single_sample_reports_insufficient_history(
    app: FastAPI, client: TestClient
) -> None:
    _post_sample(client, "2026-07-07", 50)
    _store(app).put(RAW_KEY, b"x" * 300)  # fallback measures the store, not the sample

    response = client.get(
        "/storage/forecast", params={"capacity_bytes": 1_000}, headers=auth(PARENT_TOKEN)
    )
    assert response.status_code == 200
    assert response.json() == {
        "bytes_used": 300,
        "capacity_bytes": 1_000,
        "daily_rate_bytes": 0.0,
        "days_remaining": None,
        "insufficient_history": True,
    }


def test_forecast_no_samples_sums_session_objects_only(app: FastAPI, client: TestClient) -> None:
    store = _store(app)
    store.put(RAW_KEY, b"x" * 300)
    store.put(CLIP_KEY, b"x" * 100)
    store.put("backups/db.sql", b"x" * 999)  # outside sessions/: not counted

    response = client.get(
        "/storage/forecast", params={"capacity_bytes": 1_000}, headers=auth(PARENT_TOKEN)
    )
    assert response.status_code == 200
    assert response.json() == {
        "bytes_used": 400,
        "capacity_bytes": 1_000,
        "daily_rate_bytes": 0.0,
        "days_remaining": None,
        "insufficient_history": True,
    }


def test_forecast_empty_store(client: TestClient) -> None:
    response = client.get(
        "/storage/forecast", params={"capacity_bytes": 5}, headers=auth(PARENT_TOKEN)
    )
    assert response.status_code == 200
    assert response.json()["bytes_used"] == 0


def test_forecast_requires_positive_capacity(client: TestClient) -> None:
    missing = client.get("/storage/forecast", headers=auth(PARENT_TOKEN))
    assert missing.status_code == 422
    zero = client.get("/storage/forecast", params={"capacity_bytes": 0}, headers=auth(PARENT_TOKEN))
    assert zero.status_code == 422


# -- retention preview ---------------------------------------------------------


def test_preview_applies_default_policy_and_touches_nothing(
    app: FastAPI, client: TestClient
) -> None:
    store = _store(app)
    store.put(RAW_KEY, b"old raw")
    body = {
        "ages": {
            RAW_KEY: 91,
            "sessions/s1/C2/fresh.mp4": 90,  # boundary: age == limit stays
            CLIP_KEY: 800,
            "backups/db.sql": 10_000,  # 'other': never eligible
        }
    }
    response = client.post("/storage/retention/preview", json=body, headers=auth(PARENT_TOKEN))
    assert response.status_code == 200
    assert response.json() == {"expired_keys": [RAW_KEY, CLIP_KEY]}
    assert store.exists(RAW_KEY)  # dry run deletes nothing
    assert _audit_rows(app) == []


def test_preview_honours_custom_policy_and_protected_keys(client: TestClient) -> None:
    body = {
        "ages": {RAW_KEY: 8, CLIP_KEY: 8},
        "policy": {"raw_video_days": 7, "clip_days": 7, "metrics_days": None},
        "protected_keys": [CLIP_KEY],
    }
    response = client.post("/storage/retention/preview", json=body, headers=auth(PARENT_TOKEN))
    assert response.status_code == 200
    assert response.json() == {"expired_keys": [RAW_KEY]}


def test_preview_excludes_server_derived_ground_truth_prefixes(
    app: FastAPI, client: TestClient
) -> None:
    session_id = _seed_tagged_session(app)
    protected_clip = f"sessions/{session_id}/balls/1/C1.mp4"
    body = {"ages": {protected_clip: 10_000, CLIP_KEY: 10_000}}
    response = client.post("/storage/retention/preview", json=body, headers=auth(PARENT_TOKEN))
    assert response.status_code == 200
    assert response.json() == {"expired_keys": [CLIP_KEY]}


def test_preview_rejects_missing_ages(client: TestClient) -> None:
    response = client.post("/storage/retention/preview", json={}, headers=auth(PARENT_TOKEN))
    assert response.status_code == 422


# -- retention apply -----------------------------------------------------------


def test_apply_deletes_expired_and_writes_audit_rows(app: FastAPI, client: TestClient) -> None:
    store = _store(app)
    store.put(RAW_KEY, b"old raw video")
    store.put("sessions/s1/C2/fresh.mp4", b"fresh")

    body = {"ages": {RAW_KEY: 120, "sessions/s1/C2/fresh.mp4": 10}}
    response = client.post("/storage/retention/apply", json=body, headers=auth(PARENT_TOKEN))
    assert response.status_code == 200
    assert response.json() == {"deleted": 1}
    assert not store.exists(RAW_KEY)
    assert store.exists("sessions/s1/C2/fresh.mp4")

    rows = _audit_rows(app)
    assert len(rows) == 1
    row = rows[0]
    assert row.actor == "parent"
    assert row.action == "retention_delete"
    assert row.entity == "object"
    assert row.entity_id == sha256_hex(RAW_KEY.encode())
    assert row.detail == {"key": RAW_KEY, "deleted": True}


def test_apply_logs_missing_object_as_not_deleted(app: FastAPI, client: TestClient) -> None:
    body = {"ages": {RAW_KEY: 120}}  # expired but never uploaded to the store
    response = client.post("/storage/retention/apply", json=body, headers=auth(PARENT_TOKEN))
    assert response.status_code == 200
    assert response.json() == {"deleted": 0}
    rows = _audit_rows(app)
    assert len(rows) == 1
    assert rows[0].detail == {"key": RAW_KEY, "deleted": False}


@pytest.mark.safety
def test_apply_never_deletes_protected_ground_truth_clips(app: FastAPI, client: TestClient) -> None:
    """SAF: eval-set clips survive retention no matter how old (US-B6)."""
    store = _store(app)
    store.put(CLIP_KEY, b"ground truth clip")
    body = {"ages": {CLIP_KEY: 10_000}, "protected_keys": [CLIP_KEY]}
    response = client.post("/storage/retention/apply", json=body, headers=auth(PARENT_TOKEN))
    assert response.status_code == 200
    assert response.json() == {"deleted": 0}
    assert store.exists(CLIP_KEY)
    assert _audit_rows(app) == []  # nothing deleted, nothing logged


@pytest.mark.safety
def test_apply_protects_ground_truth_clips_even_with_empty_protected_keys(
    app: FastAPI, client: TestClient
) -> None:
    """SAF: protections derive from tags server-side; callers cannot omit them."""
    session_id = _seed_tagged_session(app)
    protected_clip = f"sessions/{session_id}/balls/1/C1.mp4"
    store = _store(app)
    store.put(protected_clip, b"ground truth clip")

    body = {"ages": {protected_clip: 10_000}, "protected_keys": []}
    response = client.post("/storage/retention/apply", json=body, headers=auth(PARENT_TOKEN))
    assert response.status_code == 200
    assert response.json() == {"deleted": 0}
    assert store.exists(protected_clip)
    assert _audit_rows(app) == []


def test_apply_deletes_clips_of_non_ground_truth_tags(app: FastAPI, client: TestClient) -> None:
    session_id = _seed_tagged_session(app, ground_truth_eligible=False)
    clip = f"sessions/{session_id}/balls/1/C1.mp4"
    store = _store(app)
    store.put(clip, b"ineligible clip")

    response = client.post(
        "/storage/retention/apply", json={"ages": {clip: 10_000}}, headers=auth(PARENT_TOKEN)
    )
    assert response.status_code == 200
    assert response.json() == {"deleted": 1}
    assert not store.exists(clip)


# -- backup manifest & restore drill --------------------------------------------


def test_backup_manifest_round_trips_via_store(app: FastAPI, client: TestClient) -> None:
    session_id = _seed_tagged_session(app)
    _seed_event_with_correction(app, session_id)
    store = _store(app)
    store.put(f"sessions/{session_id}/C1/full.mp4", b"raw video")

    response = client.post("/storage/backup/manifest", headers=auth(PARENT_TOKEN))
    assert response.status_code == 201
    body = response.json()
    assert body["entity_counts"] == {
        "players": 1,
        "sessions": 1,
        "session_blocks": 0,
        "videos": 0,
        "ball_tags": 1,
        "bounce_marks": 0,
        "ball_events": 1,
        "event_corrections": 1,
        "clips": 0,
        "pose_tracks": 0,
        "ball_metrics": 0,
        "reference_balls": 0,
        "frame_samples": 0,
        "annotations": 0,
        "datasets": 0,
        "dataset_members": 0,
        "model_runs": 0,
        "model_versions": 0,
        "ball_tracks": 0,
        "bounce_estimates": 0,
        "coaching_rules": 0,
        "rule_overrides": 0,
        "safety_configs": 0,
        "reports": 0,
        "drills": 0,
        "drill_plans": 0,
        "bowling_ledger_entries": 0,
        "wellness_checkins": 0,
        "pain_clearances": 0,
        "evidence_verdicts": 0,
        "bowling_targets": 0,
        "delivery_labels": 0,
        "coach_notes": 0,
        "milestones": 0,
        "app_settings": 0,
    }
    assert body["object_count"] == 1
    assert len(body["sha256"]) == 64
    assert body["manifest_key"] == f"backups/manifest-{body['sha256'][:12]}.json"
    stored = store.get(body["manifest_key"])
    assert f'"{body["sha256"]}"' in stored.decode()

    rows = _audit_rows(app)
    assert len(rows) == 1
    assert rows[0].action == "backup_manifest"
    assert rows[0].actor == "parent"
    assert rows[0].entity_id == body["sha256"]
    assert rows[0].detail == {
        "manifest_key": body["manifest_key"],
        "entity_counts": body["entity_counts"],
        "object_count": 1,
    }

    verify = client.post(
        "/storage/backup/verify",
        json={"manifest_key": body["manifest_key"]},
        headers=auth(PARENT_TOKEN),
    )
    assert verify.status_code == 200
    assert verify.json() == {"ok": True, "discrepancies": []}
    verify_rows = [r for r in _audit_rows(app) if r.action == "backup_verify"]
    assert len(verify_rows) == 1
    assert verify_rows[0].detail == {
        "manifest_key": body["manifest_key"],
        "ok": True,
        "discrepancies": [],
    }


def test_backup_verify_catches_deleted_row_and_missing_object(
    app: FastAPI, client: TestClient
) -> None:
    session_id = _seed_tagged_session(app)
    _seed_event_with_correction(app, session_id)
    store = _store(app)
    raw_key = f"sessions/{session_id}/C1/full.mp4"
    store.put(raw_key, b"raw video")

    manifest_key = client.post("/storage/backup/manifest", headers=auth(PARENT_TOKEN)).json()[
        "manifest_key"
    ]

    with app.state.session_factory() as db:  # simulate a bad restore: lost rows
        tag = db.scalars(select(BallTag)).one()
        db.delete(tag)
        # …including the dropped US-D4 ground-truth correction trail.
        correction = db.scalars(select(EventCorrection)).one()
        db.delete(correction)
        db.commit()
    store.delete(raw_key)  # …and a lost object

    response = client.post(
        "/storage/backup/verify", json={"manifest_key": manifest_key}, headers=auth(PARENT_TOKEN)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert "entity count mismatch for ball_tags: expected 1, got 0" in body["discrepancies"]
    assert "entity count mismatch for event_corrections: expected 1, got 0" in body["discrepancies"]
    assert "object count mismatch: expected 1, got 0" in body["discrepancies"]
    assert any("checksum mismatch" in d for d in body["discrepancies"])
    verify_rows = [r for r in _audit_rows(app) if r.action == "backup_verify"]
    assert len(verify_rows) == 1
    assert verify_rows[0].detail is not None
    assert verify_rows[0].detail["ok"] is False


def test_backup_verify_unknown_manifest_returns_404(app: FastAPI, client: TestClient) -> None:
    response = client.post(
        "/storage/backup/verify",
        json={"manifest_key": "backups/manifest-nope.json"},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 404
    assert _audit_rows(app) == []


# -- RBAC ----------------------------------------------------------------------


def _all_requests(client: TestClient, headers: dict[str, str]) -> list[int]:
    body = {"ages": {}}
    return [
        client.get("/storage/forecast", params={"capacity_bytes": 1}, headers=headers).status_code,
        client.post(
            "/storage/usage-samples", json={"sampled_on": "2026-07-07"}, headers=headers
        ).status_code,
        client.post("/storage/retention/preview", json=body, headers=headers).status_code,
        client.post("/storage/retention/apply", json=body, headers=headers).status_code,
        client.post("/storage/backup/manifest", headers=headers).status_code,
        client.post(
            "/storage/backup/verify", json={"manifest_key": "backups/x.json"}, headers=headers
        ).status_code,
    ]


@pytest.mark.parametrize("token", [COACH_TOKEN, PLAYER_TOKEN])
def test_storage_admin_is_parent_only(client: TestClient, token: str) -> None:
    assert _all_requests(client, auth(token)) == [403] * 6


def test_storage_admin_requires_token(client: TestClient) -> None:
    assert _all_requests(client, {}) == [401] * 6
