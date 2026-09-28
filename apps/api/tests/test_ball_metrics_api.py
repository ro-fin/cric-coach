"""US-E2/E3/E4 acceptance: ball-metrics contract validation, upsert, summary
denominators, V1 flight synthesis with provenance, decision quality, RBAC scoping.

Tags, bounce marks and blocks are seeded directly in the DB (the same shortcut
test_bounce.py uses for calibrations) so these tests pin the metrics API without
re-testing the tags/bounce routers.
"""

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from cricai_api.routers.ball_metrics import MACHINE_WRITTEN_METRIC_KEYS
from cricai_coaching.contact_fusion import FUSION_METRIC_KEYS
from cricai_coaching.contact_metrics import compute_contact
from cricai_coaching.pre_release import compute_pre_release
from cricai_data.db import make_session_factory
from cricai_data.enums import (
    BlockIntent,
    BowlerSource,
    Contact,
    Footwork,
    Length,
    Line,
    MetricPhase,
    Outcome,
    Shot,
)
from cricai_data.models import BallEvent, BallMetrics, BallTag, BallTrack, BounceMark, SessionBlock
from cricai_data.models import Session as SessionModel
from cricai_data.storage import FsObjectStore
from cricai_vision.pose import FakePoseProvider
from cricai_worker.bowling_action import BOWLING_ACTION_KEYS
from cricai_worker.bowling_flight import BOWLING_FLIGHT_KEYS
from cricai_worker.context import WorkerContext
from cricai_worker.fuse_contacts import fuse_session_contacts
from fastapi.testclient import TestClient
from sqlalchemy import Engine
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

CONTACT_METRICS: dict[str, Any] = {
    "head_stability_score": {"value": 0.83, "unit": "score", "confidence": 0.9},
    "bat_path_class": {"value": "straight", "unit": "class", "confidence": 0.8, "proxy": True},
    "front_foot_direction_cm": {
        "value": None,
        "unit": "cm",
        "confidence": 0.0,
        "reason": "no pixel-to-cm scale",
    },
}


@pytest.fixture
def engine() -> Engine:
    return make_sqlite_engine()


@pytest.fixture
def client(engine: Engine, tmp_path: Path) -> TestClient:
    return TestClient(make_test_app(tmp_path, engine=engine))


def _create_session(
    client: TestClient,
    *,
    is_guest: bool = False,
    machine_settings: dict[str, Any] | None = None,
) -> str:
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
    payload: dict[str, Any] = {
        "player_id": player["id"],
        "date": "2026-07-07",
        "session_type": "batting",
        "bowler_source": "machine" if machine_settings is not None else "coach",
    }
    if machine_settings is not None:
        payload["machine_settings"] = machine_settings
    session = client.post("/sessions", json=payload, headers=auth(PARENT_TOKEN)).json()
    session_id: str = session["id"]
    return session_id


def _seed_event(engine: Engine, session_id: str, ball_no: int, *, valid: bool = True) -> None:
    """Seed a detected BallEvent directly (the events API is another story's file)."""
    with DbSession(engine) as db:
        db.add(
            BallEvent(
                session_id=uuid.UUID(session_id),
                ball_no=ball_no,
                start_ms=1000 * ball_no,
                release_ms=1000 * ball_no + 200,
                contact_ms=None,
                end_ms=1000 * ball_no + 900,
                confidence=0.9,
                valid=valid,
            )
        )
        db.commit()


def _seed_tag(
    engine: Engine,
    session_id: str,
    ball_no: int,
    *,
    line: Line = Line.OUTSIDE_OFF,
    length: Length = Length.GOOD,
    shot: Shot = Shot.COVER_DRIVE,
    block_id: uuid.UUID | None = None,
) -> None:
    with DbSession(engine) as db:
        db.add(
            BallTag(
                session_id=uuid.UUID(session_id),
                ball_no=ball_no,
                block_id=block_id,
                line=line,
                length=length,
                shot=shot,
                footwork=Footwork.FRONT,
                contact=Contact.MIDDLE,
                outcome=Outcome.CONTROLLED_GROUND_SHOT,
                control=True,
                created_by="coach",
            )
        )
        db.commit()


def _seed_mark(
    engine: Engine,
    session_id: str,
    ball_no: int,
    *,
    camera_id: str = "C3",
    line: Line | None = Line.OUTSIDE_OFF,
    length: Length | None = Length.SHORT,
    pitch_x: float = 0.6,
    pitch_y: float = 9.5,
) -> None:
    with DbSession(engine) as db:
        db.add(
            BounceMark(
                session_id=uuid.UUID(session_id),
                ball_no=ball_no,
                camera_id=camera_id,
                frame_no=42,
                px_x=100.0,
                px_y=200.0,
                pitch_x=pitch_x,
                pitch_y=pitch_y,
                line=line,
                length=length,
            )
        )
        db.commit()


def _seed_block(
    engine: Engine, session_id: str, machine_settings: dict[str, Any] | None
) -> uuid.UUID:
    with DbSession(engine) as db:
        block = SessionBlock(
            session_id=uuid.UUID(session_id),
            block_no=1,
            start_s=0.0,
            bowler_source=BowlerSource.MACHINE,
            machine_settings=machine_settings,
            intent=BlockIntent.TECHNICAL,
        )
        db.add(block)
        db.commit()
        return block.id


def _put(
    client: TestClient,
    session_id: str,
    ball_no: int,
    phase: str,
    metrics: dict[str, Any],
    *,
    token: str = PARENT_TOKEN,
    schema_version: int | None = None,
) -> Any:
    payload: dict[str, Any] = {"metrics": metrics}
    if schema_version is not None:
        payload["schema_version"] = schema_version
    # Serialize with stdlib json (allow_nan=True default): unlike httpx's encoder it
    # emits NaN/Infinity tokens, the exact wire shape a naive client can produce.
    return client.put(
        f"/sessions/{session_id}/balls/{ball_no}/metrics/{phase}",
        content=json.dumps(payload),
        headers={**auth(token), "Content-Type": "application/json"},
    )


def _get_ball(client: TestClient, session_id: str, ball_no: int, token: str = PARENT_TOKEN) -> Any:
    return client.get(f"/sessions/{session_id}/balls/{ball_no}/metrics", headers=auth(token))


# --- PUT: upsert + contract validation -------------------------------------------------


def test_put_creates_then_replaces_the_phase_set(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    _seed_event(engine, session_id, 1)
    created = _put(client, session_id, 1, "contact", CONTACT_METRICS)
    assert created.status_code == 201
    body = created.json()
    assert body["phase"] == "contact"
    assert body["stored"] is True
    assert body["schema_version"] == 1
    assert body["metrics"] == CONTACT_METRICS

    updated = _put(
        client,
        session_id,
        1,
        "contact",
        {"head_stability_score": {"value": 0.5, "unit": "score", "confidence": 0.7}},
        schema_version=2,
    )
    assert updated.status_code == 200
    listed = _get_ball(client, session_id, 1).json()
    assert len(listed) == 1
    assert listed[0]["schema_version"] == 2
    assert listed[0]["metrics"]["head_stability_score"]["value"] == 0.5
    assert "bat_path_class" not in listed[0]["metrics"]  # replace, not merge


@pytest.mark.parametrize(
    ("metrics", "detail_match"),
    [
        ({}, "at least one entry"),
        ({"m": "hi"}, "must be an object"),
        ({"m": {"value": 1, "unit": "cm", "confidence": 0.5, "bogus": 1}}, "unknown keys"),
        ({"m": {"unit": "cm", "confidence": 0.5}}, "missing required key 'value'"),
        ({"m": {"value": {"x": 1}, "unit": "cm", "confidence": 0.5}}, "'value' must be"),
        ({"m": {"value": [1, "x"], "unit": "cm", "confidence": 0.5}}, "'value' must be"),
        ({"m": {"value": 1, "confidence": 0.5}}, "'unit' must be a string"),
        ({"m": {"value": 1, "unit": 3, "confidence": 0.5}}, "'unit' must be a string"),
        ({"m": {"value": 1, "unit": "cm"}}, "'confidence' must be a finite number"),
        (
            {"m": {"value": 1, "unit": "cm", "confidence": True}},
            "'confidence' must be a finite number",
        ),
        ({"m": {"value": 1, "unit": "cm", "confidence": 1.5}}, "within"),
        ({"m": {"value": 1, "unit": "cm", "confidence": -0.1}}, "within"),
        # json.loads accepts NaN/Infinity tokens: non-finite numbers are 422s (never
        # stored as silent nulls, never a jsonb 500 on PostgreSQL).
        ({"m": {"value": float("nan"), "unit": "cm", "confidence": 0.5}}, "'value' must be"),
        ({"m": {"value": float("inf"), "unit": "cm", "confidence": 0.5}}, "'value' must be"),
        ({"m": {"value": [1.0, float("nan")], "unit": "cm", "confidence": 0.5}}, "'value' must be"),
        (
            {"m": {"value": 1, "unit": "cm", "confidence": float("nan")}},
            "'confidence' must be a finite number",
        ),
        (
            {"m": {"value": 1, "unit": "cm", "confidence": float("-inf")}},
            "'confidence' must be a finite number",
        ),
        ({"m": {"value": 1, "unit": "cm", "confidence": 0.5, "reason": "  "}}, "'reason'"),
        ({"m": {"value": 1, "unit": "cm", "confidence": 0.5, "reason": 7}}, "'reason'"),
        ({"m": {"value": None, "unit": "cm", "confidence": 0.0}}, "silent-zero"),
        ({"m": {"value": 1, "unit": "cm", "confidence": 0.5, "proxy": "yes"}}, "'proxy'"),
        ({"m": {"value": 1, "unit": "cm", "confidence": 0.5, "source": 5}}, "'source'"),
    ],
)
def test_put_rejects_contract_violations(
    client: TestClient, engine: Engine, metrics: dict[str, Any], detail_match: str
) -> None:
    session_id = _create_session(client)
    _seed_event(engine, session_id, 1)
    response = _put(client, session_id, 1, "contact", metrics)
    assert response.status_code == 422
    assert detail_match in response.json()["detail"]


def test_put_accepts_every_contract_value_shape(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    _seed_event(engine, session_id, 1)
    metrics = {
        "numeric": {"value": 3, "unit": "cm", "confidence": 1.0},
        "boolean": {"value": True, "unit": "bool", "confidence": 1},
        "classy": {"value": "straight", "unit": "class", "confidence": 0.5, "proxy": True},
        "pair": {"value": [0.2, 5.5], "unit": "m", "confidence": 1.0, "source": "manual"},
        "explained": {"value": None, "unit": "cm", "confidence": 0.0, "reason": "low visibility"},
    }
    assert _put(client, session_id, 1, "flight", metrics).status_code == 201


def test_put_rejects_invalid_phase_and_ball_no(client: TestClient) -> None:
    session_id = _create_session(client)
    assert _put(client, session_id, 1, "warmup", CONTACT_METRICS).status_code == 422
    assert _put(client, session_id, 0, "contact", CONTACT_METRICS).status_code == 422


def test_put_unknown_session_is_404(client: TestClient) -> None:
    assert _put(client, UNKNOWN_SESSION, 1, "contact", CONTACT_METRICS).status_code == 404


def test_put_unknown_ball_is_404_and_creates_no_phantom_row(
    client: TestClient, engine: Engine
) -> None:
    """No ball_metrics row without a known ball: a phantom row would pollute summary
    denominators AND pin US-D4 renumbering (ball_metrics is a dependent table)."""
    session_id = _create_session(client)
    _seed_event(engine, session_id, 1)
    _seed_tag(engine, session_id, 2)
    _seed_event(engine, session_id, 3, valid=False)  # rejected events are not balls

    denied = _put(client, session_id, 99, "contact", CONTACT_METRICS)
    assert denied.status_code == 404
    assert "ball 99" in denied.json()["detail"]
    rejected = _put(client, session_id, 3, "contact", CONTACT_METRICS)
    assert rejected.status_code == 404

    # Known balls still work: via a detected event (ball 1) or a manual tag (ball 2).
    assert _put(client, session_id, 1, "contact", CONTACT_METRICS).status_code == 201
    assert _put(client, session_id, 2, "contact", CONTACT_METRICS).status_code == 201

    # No phantom rows were stored for the rejected PUTs.
    summary = client.get(
        f"/sessions/{session_id}/metrics/summary", headers=auth(PARENT_TOKEN)
    ).json()
    assert [phase["balls"] for phase in summary["phases"]] == [2]
    assert _get_ball(client, session_id, 99).json() == []


# --- compute_* outputs round-trip through the PUT validator (US-E2/E3 contract seam) -----


def test_compute_pre_release_output_roundtrips_through_put(
    client: TestClient, engine: Engine
) -> None:
    """Regression guard: the phase's own producer output must pass the API validator
    (the head_speed side-channel key inside still_at_release once 422'd here)."""
    session_id = _create_session(client)
    _seed_event(engine, session_id, 1)
    track = FakePoseProvider().extract([None] * 24, fps=120.0)
    metrics = compute_pre_release(track, release_frame=12)
    response = _put(client, session_id, 1, "pre_release", dict(metrics))
    assert response.status_code == 201
    stored = response.json()["metrics"]
    assert set(stored) == set(metrics)
    assert stored["still_at_release"]["unit"] == "bool"
    assert stored["head_speed_at_release"]["unit"] == "px_per_ms"


def test_compute_contact_output_roundtrips_through_put(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    _seed_event(engine, session_id, 1)
    track = FakePoseProvider().extract([None] * 24, fps=120.0)
    payload = {
        name: value.to_payload() for name, value in compute_contact(track, contact_frame=23).items()
    }
    response = _put(client, session_id, 1, "contact", payload)
    assert response.status_code == 201
    stored = response.json()["metrics"]
    assert set(stored) == set(payload)
    # Tracking-free stand-ins keep their proxy provenance through the store (US-E3).
    assert stored["contact_point_class"]["proxy"] is True
    assert stored["bat_path_class"]["proxy"] is True


def _seed_track(engine: Engine, store: FsObjectStore, session_id: str, ball_no: int) -> None:
    """A ball_tracks row + pinned US-F3 payload timed to _seed_event's window: a
    clean 90-deg middled deflection with a post_contact segment from 1600 ms."""
    key = f"sessions/{session_id}/balls/{ball_no}/track-C1.json"
    payload = {
        "points": [
            {
                "frame_no": round(ts / 10),
                "ts_ms": ts,
                "px_x": x,
                "px_y": y,
                "score": 0.9,
                "bridged": False,
            }
            for ts, x, y in [
                (1500.0, 50.0, 50.0),
                (1550.0, 100.0, 50.0),
                (1600.0, 150.0, 50.0),
                (1610.0, 150.0, 40.0),
                (1630.0, 150.0, 20.0),
            ]
        ],
        "segments": [
            {"kind": "post_contact", "start_ms": 1600.0, "end_ms": 1800.0, "confidence": 0.9}
        ],
        "flags": {"identity_risk": False, "long_gap": False},
    }
    with DbSession(engine) as db:
        db.add(
            BallTrack(
                session_id=uuid.UUID(session_id),
                ball_no=ball_no,
                camera_id="C1",
                tracker_version="trk-test-1",
                points_key=key,
                coverage=0.95,
                segments=[],
                flags={},
                confidence=0.9,
            )
        )
        db.commit()
    store.put(key, json.dumps(payload).encode("utf-8"))


def test_e3_put_preserves_fusion_written_contact_keys(
    client: TestClient, engine: Engine, tmp_path: Path
) -> None:
    """The US-F5 job merge-writes contact_quality/bat_path; an E3 whole-phase-set
    PUT must not silently delete them (fuse -> real PUT -> fusion outputs survive)."""
    session_id = _create_session(client)
    _seed_event(engine, session_id, 1)
    store = FsObjectStore(tmp_path / "worker-store")
    _seed_track(engine, store, session_id, 1)
    ctx = WorkerContext(session_factory=make_session_factory(engine), store=store)

    summary = fuse_session_contacts(ctx, uuid.UUID(session_id))
    assert summary.fused_count == 1  # fusion really wrote the two fusion-owned keys

    replaced = _put(client, session_id, 1, "contact", CONTACT_METRICS)
    assert replaced.status_code == 200
    metrics = replaced.json()["metrics"]
    assert metrics["head_stability_score"]["value"] == 0.83  # the E3 set landed
    quality = metrics["contact_quality"]  # ...and the fusion outputs survived
    assert quality["value"] == "middle"
    assert quality["source"].startswith("contact-fusion-")
    assert metrics["bat_path"]["value"] is None  # null-with-reason survives too
    assert metrics["bat_path"]["reason"] == "no detection provider configured"

    # An explicit write to a fusion-owned key still wins (preserve, never lock):
    override = {"contact_quality": {"value": "edge", "unit": "class", "confidence": 0.5}}
    overridden = _put(client, session_id, 1, "contact", override).json()["metrics"]
    assert overridden["contact_quality"]["value"] == "edge"
    assert overridden["bat_path"]["reason"] == "no detection provider configured"
    assert "head_stability_score" not in overridden  # non-fusion keys still replace


#: Flight metrics as the US-I3 bowling-flight job merge-writes them by key.
BOWLING_FLIGHT_METRICS: dict[str, Any] = {
    "turn_cm": {"value": 12.6, "unit": "cm", "confidence": 0.8, "source": "bowling-flight-v1"},
    "apex_m": {"value": 2.4, "unit": "m", "confidence": 0.8, "source": "bowling-flight-v1"},
    "dip_flag": {"value": True, "unit": "flag", "confidence": 0.7, "source": "bowling-flight-v1"},
    "target_hit": {"value": True, "unit": "flag", "confidence": 1.0, "source": "target-scoring"},
}

#: Pre-release metrics as the US-I2 bowling-action job merge-writes them —
#: deliberately MISSING hand_xy/head-offset keys (occlusion), so the survival
#: guard's key-absent path is exercised too.
BOWLING_ACTION_METRICS: dict[str, Any] = {
    "release_frame": {"value": 12, "unit": "frame", "confidence": 0.9, "source": "release-v1"},
    "release_ms": {"value": 1200, "unit": "ms", "confidence": 0.9, "source": "release-v1"},
    "release_height_cm": {"value": 201.5, "unit": "cm", "confidence": 0.8, "source": "release-v1"},
    "brace_state": {"value": "braced", "unit": "class", "confidence": 0.8, "source": "checkpoints"},
    "falling_away_deg": {"value": 4.2, "unit": "deg", "confidence": 0.8, "source": "checkpoints"},
}


def _seed_phase_metrics(
    engine: Engine, session_id: str, ball_no: int, phase: MetricPhase, metrics: dict[str, Any]
) -> None:
    """Seed a stored metric row directly, as the worker jobs' merge-writes leave it."""
    with DbSession(engine) as db:
        db.add(
            BallMetrics(
                session_id=uuid.UUID(session_id),
                ball_no=ball_no,
                phase=phase,
                metrics=metrics,
                schema_version=1,
            )
        )
        db.commit()


def test_e4_flight_put_preserves_bowling_flight_keys(client: TestClient, engine: Engine) -> None:
    """The US-I3 job merge-writes turn_cm/apex_m/dip_flag/target_hit into the
    flight phase; an E4 whole-phase-set PUT (a speed correction, a job re-run)
    must not silently delete them — survival is mutual, exactly the US-F5
    contact discipline (findings 6/32/57)."""
    session_id = _create_session(client)
    _seed_event(engine, session_id, 1)
    _seed_phase_metrics(engine, session_id, 1, MetricPhase.FLIGHT, BOWLING_FLIGHT_METRICS)

    speed = {"speed_kph": {"value": 92.0, "unit": "kph", "confidence": 1.0}}
    replaced = _put(client, session_id, 1, "flight", speed)
    assert replaced.status_code == 200
    metrics = replaced.json()["metrics"]
    assert metrics["speed_kph"]["value"] == 92.0  # the E4 correction landed
    for key in ("turn_cm", "apex_m", "dip_flag", "target_hit"):
        assert metrics[key] == BOWLING_FLIGHT_METRICS[key]  # ...and i3's outputs survived

    # An explicit write to a job-owned key still wins (preserve, never lock):
    override = {"turn_cm": {"value": 3.1, "unit": "cm", "confidence": 0.5}}
    overridden = _put(client, session_id, 1, "flight", override).json()["metrics"]
    assert overridden["turn_cm"]["value"] == 3.1
    assert overridden["target_hit"] == BOWLING_FLIGHT_METRICS["target_hit"]
    assert "speed_kph" not in overridden  # non-protected keys still replace


def test_e2_pre_release_put_preserves_bowling_action_keys(
    client: TestClient, engine: Engine
) -> None:
    """The US-I2 job merge-writes release geometry + checkpoint verdicts into
    the pre_release phase; an E2 whole-phase-set PUT must preserve every stored
    job-owned key the payload does not rewrite (findings 6/32/57)."""
    session_id = _create_session(client)
    _seed_event(engine, session_id, 1)
    _seed_phase_metrics(engine, session_id, 1, MetricPhase.PRE_RELEASE, BOWLING_ACTION_METRICS)

    e2_set = {"front_arm_angle_deg": {"value": 143.0, "unit": "deg", "confidence": 0.9}}
    replaced = _put(client, session_id, 1, "pre_release", e2_set)
    assert replaced.status_code == 200
    metrics = replaced.json()["metrics"]
    assert metrics["front_arm_angle_deg"]["value"] == 143.0
    for key, entry in BOWLING_ACTION_METRICS.items():  # every stored job-owned key survived
        assert metrics[key] == entry
    assert "hand_xy" not in metrics  # absent keys are not resurrected


def test_machine_written_registry_mirrors_the_worker_owned_key_tuples() -> None:
    """The API cannot import the worker app, so the per-phase protection
    registry mirrors the worker's job-owned key constants by hand; this parity
    test is the drift guard (findings 6/32/57)."""
    assert set(MACHINE_WRITTEN_METRIC_KEYS[MetricPhase.FLIGHT]) == set(BOWLING_FLIGHT_KEYS)
    assert set(MACHINE_WRITTEN_METRIC_KEYS[MetricPhase.PRE_RELEASE]) == set(BOWLING_ACTION_KEYS)
    assert set(MACHINE_WRITTEN_METRIC_KEYS[MetricPhase.CONTACT]) == set(FUSION_METRIC_KEYS)
    assert set(MACHINE_WRITTEN_METRIC_KEYS) == set(MetricPhase)  # total: no unguarded phase


def test_put_is_parent_only(client: TestClient) -> None:
    session_id = _create_session(client)
    for token in (COACH_TOKEN, PLAYER_TOKEN):
        denied = _put(client, session_id, 1, "contact", CONTACT_METRICS, token=token)
        assert denied.status_code == 403
    missing = client.put(
        f"/sessions/{session_id}/balls/1/metrics/contact", json={"metrics": CONTACT_METRICS}
    )
    assert missing.status_code == 401


# --- GET per ball: stored phases + US-E4 V1 flight synthesis ----------------------------


def test_get_orders_phases_and_synthesizes_flight_from_manual_sources(
    client: TestClient, engine: Engine
) -> None:
    session_id = _create_session(client, machine_settings={"speed_kph": 85, "length": "good"})
    _seed_tag(engine, session_id, 1)
    _put(client, session_id, 1, "contact", CONTACT_METRICS)
    _put(
        client,
        session_id,
        1,
        "pre_release",
        {"stance_width_cm": {"value": 30.5, "unit": "cm", "confidence": 0.9}},
    )
    listed = _get_ball(client, session_id, 1).json()
    assert [row["phase"] for row in listed] == ["pre_release", "contact", "flight"]
    assert [row["stored"] for row in listed] == [True, True, False]
    flight = listed[2]["metrics"]
    assert flight["speed_kph"] == {
        "value": 85.0,
        "unit": "kph",
        "confidence": 1.0,
        "source": "manual",
    }
    assert flight["line"]["value"] == "outside_off"
    assert flight["decision_class"]["value"] == "drive"
    assert flight["bounce_xy"]["value"] is None
    assert "no bounce mark" in flight["bounce_xy"]["reason"]


def test_get_prefers_a_stored_flight_row_over_synthesis(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client, machine_settings={"speed_kph": 85, "length": "good"})
    _seed_tag(engine, session_id, 1)
    stored = {"speed_kph": {"value": 99.0, "unit": "kph", "confidence": 1.0, "source": "manual"}}
    _put(client, session_id, 1, "flight", stored)
    listed = _get_ball(client, session_id, 1).json()
    assert len(listed) == 1
    assert listed[0]["stored"] is True
    assert listed[0]["metrics"] == stored


def test_get_synthesizes_flight_from_a_bounce_only_ball(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    _seed_mark(engine, session_id, 2, line=Line.OFF, length=Length.FULL)
    _seed_mark(engine, session_id, 2, camera_id="C4", line=Line.MIDDLE, length=Length.GOOD)
    listed = _get_ball(client, session_id, 2).json()
    assert len(listed) == 1
    flight = listed[0]["metrics"]
    assert listed[0]["stored"] is False
    # Representative mark = lowest camera_id (C3), same dedupe rule as the heatmap.
    assert flight["line"]["value"] == "off"
    assert flight["length"]["value"] == "full"
    assert flight["bounce_xy"]["value"] == [0.6, 9.5]
    assert flight["shot"]["value"] is None
    assert "no tag" in flight["shot"]["reason"]
    assert flight["speed_kph"]["value"] is None  # coach session: no machine speed


def test_get_without_any_context_is_empty(client: TestClient) -> None:
    session_id = _create_session(client)
    assert _get_ball(client, session_id, 9).json() == []


def test_flight_speed_prefers_the_tags_block_machine_settings(
    client: TestClient, engine: Engine
) -> None:
    session_id = _create_session(client, machine_settings={"speed_kph": 85, "length": "good"})
    block_id = _seed_block(engine, session_id, {"speed_kph": 100, "length": "short"})
    _seed_tag(engine, session_id, 1, block_id=block_id)
    flight = _get_ball(client, session_id, 1).json()[0]["metrics"]
    assert flight["speed_kph"]["value"] == 100.0


def test_flight_speed_falls_back_to_session_settings(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client, machine_settings={"speed_kph": 85, "length": "good"})
    # A block without machine settings (e.g. coach throwdowns) falls through.
    block_id = _seed_block(engine, session_id, None)
    _seed_tag(engine, session_id, 1, block_id=block_id)
    # A dangling block reference also falls through instead of crashing.
    _seed_tag(engine, session_id, 2, block_id=uuid.uuid4())
    for ball_no in (1, 2):
        flight = _get_ball(client, session_id, ball_no).json()[0]["metrics"]
        assert flight["speed_kph"]["value"] == 85.0


def test_flight_speed_null_when_settings_lack_a_speed(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    with DbSession(engine) as db:
        session = db.get(SessionModel, uuid.UUID(session_id))
        assert session is not None
        session.machine_settings = {"length": "good"}
        db.commit()
    _seed_tag(engine, session_id, 1)
    flight = _get_ball(client, session_id, 1).json()[0]["metrics"]
    assert flight["speed_kph"]["value"] is None
    assert "machine speed" in flight["speed_kph"]["reason"]


# --- GET summary: means with reported denominators (US-E3) ------------------------------


def test_summary_reports_means_and_non_null_denominators(
    client: TestClient, engine: Engine
) -> None:
    session_id = _create_session(client)
    for ball_no in (1, 2, 3):
        _seed_event(engine, session_id, ball_no)
    values: list[dict[str, Any]] = [
        {
            "head_stability_score": {"value": 0.5, "unit": "score", "confidence": 0.9},
            "control": {"value": True, "unit": "bool", "confidence": 0.9},
            "bat_path_class": {"value": "straight", "unit": "class", "confidence": 0.8},
            "only_ball_one": {"value": 4.0, "unit": "cm", "confidence": 0.9},
        },
        {
            "head_stability_score": {"value": 0.7, "unit": "score", "confidence": 0.9},
            "control": {"value": False, "unit": "bool", "confidence": 0.9},
            "bat_path_class": {"value": "across", "unit": "class", "confidence": 0.8},
        },
        {
            "head_stability_score": {
                "value": None,
                "unit": "score",
                "confidence": 0.0,
                "reason": "low visibility",
            },
            "control": {"value": True, "unit": "bool", "confidence": 0.9},
            "bat_path_class": {
                "value": None,
                "unit": "class",
                "confidence": 0.0,
                "reason": "low visibility",
            },
        },
    ]
    for ball_no, metrics in enumerate(values, start=1):
        _put(client, session_id, ball_no, "contact", metrics)
    _put(
        client,
        session_id,
        1,
        "pre_release",
        {"stance_width_cm": {"value": 31.0, "unit": "cm", "confidence": 0.9}},
    )

    summary = client.get(
        f"/sessions/{session_id}/metrics/summary", headers=auth(COACH_TOKEN)
    ).json()
    assert summary["session_id"] == session_id
    assert [phase["phase"] for phase in summary["phases"]] == ["pre_release", "contact"]
    contact = summary["phases"][1]
    assert contact["balls"] == 3
    by_name = {metric["name"]: metric for metric in contact["metrics"]}
    # Nulls are excluded from the mean and the reported denominator (US-E3 AC).
    assert by_name["head_stability_score"] == {
        "name": "head_stability_score",
        "mean": 0.6,
        "count": 2,
    }
    # Booleans average as the fraction true: the control % with its denominator.
    assert by_name["control"] == {"name": "control", "mean": round(2 / 3, 4), "count": 3}
    # Classes have no mean but their denominator is still reported.
    assert by_name["bat_path_class"] == {"name": "bat_path_class", "mean": None, "count": 2}
    assert by_name["only_ball_one"] == {"name": "only_ball_one", "mean": 4.0, "count": 1}


def test_summary_phase_filter(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    _seed_event(engine, session_id, 1)
    _put(client, session_id, 1, "contact", CONTACT_METRICS)
    filtered = client.get(
        f"/sessions/{session_id}/metrics/summary",
        params={"phase": "contact"},
        headers=auth(PARENT_TOKEN),
    ).json()
    assert [phase["phase"] for phase in filtered["phases"]] == ["contact"]
    empty = client.get(
        f"/sessions/{session_id}/metrics/summary",
        params={"phase": "flight"},
        headers=auth(PARENT_TOKEN),
    ).json()
    assert empty["phases"] == []


# --- GET decision quality (US-E4) -------------------------------------------------------


def test_decision_quality_joins_tags_and_bounce_marks(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client)
    _seed_tag(engine, session_id, 1, shot=Shot.LEAVE)
    _seed_tag(engine, session_id, 2, shot=Shot.COVER_DRIVE)
    _seed_tag(engine, session_id, 3, length=Length.FULL, shot=Shot.HOOK)
    _seed_tag(engine, session_id, 4, line=Line.MIDDLE, shot=Shot.DEFEND)  # ignored
    # Tagged ball 2 also has a mark: the tag wins, no double count.
    _seed_mark(engine, session_id, 2, line=Line.MIDDLE, length=Length.GOOD)
    # Bounce-only ball 5: outside off but shotless -> unclassified; C3 represents it.
    _seed_mark(engine, session_id, 5)
    _seed_mark(engine, session_id, 5, camera_id="C4", line=Line.MIDDLE, length=Length.GOOD)
    # Unclassifiable mark (no derived line) is ignored entirely.
    _seed_mark(engine, session_id, 6, line=None, length=None)

    quality = client.get(
        f"/sessions/{session_id}/decision-quality", headers=auth(PLAYER_TOKEN)
    ).json()
    assert quality == {
        "session_id": session_id,
        "outside_off_balls": 4,
        "leaves": 1,
        "chases": 2,
        "unclassified": 1,
        "zones": [
            {"length": "full", "leaves": 0, "chases": 1, "balls": 1},
            {"length": "good", "leaves": 1, "chases": 1, "balls": 2},
        ],
    }


# --- RBAC & guest scoping (US-L3) --------------------------------------------------------


def test_reads_are_open_to_players_on_non_guest_sessions(
    client: TestClient, engine: Engine
) -> None:
    session_id = _create_session(client)
    _seed_event(engine, session_id, 1)
    _put(client, session_id, 1, "contact", CONTACT_METRICS)
    assert _get_ball(client, session_id, 1, token=PLAYER_TOKEN).status_code == 200
    assert (
        client.get(
            f"/sessions/{session_id}/metrics/summary", headers=auth(PLAYER_TOKEN)
        ).status_code
        == 200
    )


def test_guest_sessions_are_hidden_from_players(client: TestClient, engine: Engine) -> None:
    session_id = _create_session(client, is_guest=True)
    _seed_event(engine, session_id, 1)
    _put(client, session_id, 1, "contact", CONTACT_METRICS)  # parent still writes fine
    for url in (
        f"/sessions/{session_id}/balls/1/metrics",
        f"/sessions/{session_id}/metrics/summary",
        f"/sessions/{session_id}/decision-quality",
    ):
        assert client.get(url, headers=auth(PLAYER_TOKEN)).status_code == 404
        assert client.get(url, headers=auth(COACH_TOKEN)).status_code == 200


def test_unknown_session_reads_are_404(client: TestClient) -> None:
    for url in (
        f"/sessions/{UNKNOWN_SESSION}/balls/1/metrics",
        f"/sessions/{UNKNOWN_SESSION}/metrics/summary",
        f"/sessions/{UNKNOWN_SESSION}/decision-quality",
    ):
        assert client.get(url, headers=auth(PARENT_TOKEN)).status_code == 404
