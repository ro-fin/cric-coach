"""T5 #1 golden: the machine batting day, end to end on real temp PostgreSQL.

120 seeded synthetic machine balls (``cricai_data.synthetic``, seed pinned)
carry one PLANTED fault: exactly 48 of the 120 (40%) land full outside off
with a degraded ``front_foot_direction_cm`` (8 cm — under the approved rule's
15 cm threshold); every other ball strides a healthy 22 cm. The plant flows
through PRODUCTION surfaces only — the shipped rule YAMLs are imported through
``scripts/rules_io`` (US-G2: rules are data), the per-ball ground truth is
manual tags plus stored technique metric payloads (the rows a producer writes,
mirroring the bowling golden's manual bounce marks), the media is real (two
cv2-written camera videos, real ffmpeg clip cuts) and the FULL DAG runs with
its production wiring (US-J1/L1). No finding is hand-inserted: the fault must
be discovered by the real analysis path (US-G2 rules + US-J2 probes over
canonical US-G1 BallRecords).

The daily report is then asserted THROUGH THE API (US-G3/G6/H5):

- ``main_correction`` names exactly the planted rule's authored correction
  with the exact matched-ball count computed from the seed;
- its evidence links >= 2 playable clips (here 96: the 48 hit balls x 2
  cameras), every link a real CUT clip;
- exactly one drill (the planner-scheduled library drill, US-J3) and one
  measurable goal (the rule's threshold) ship — and nothing else fires;
- every claimed number recomputes from the database, and publish succeeds;
  the child sees the report only after it is published.
"""

from __future__ import annotations

import datetime
import importlib.util
import sys
import uuid as uuid_module
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from alembic import command
from alembic.config import Config
from cricai_data.db import make_engine, make_session_factory
from cricai_data.enums import (
    BlockIntent,
    BowlerSource,
    Length,
    Line,
    MetricPhase,
    SessionType,
    StageStatus,
    VideoStatus,
)
from cricai_data.models import (
    BallMetrics,
    BallTag,
    Drill,
    Finding,
    PipelineStage,
    Player,
    Video,
)
from cricai_data.models import Session as SessionRow
from cricai_data.storage import FsObjectStore
from cricai_data.synthetic import BlockSpec, SyntheticBall, SyntheticSession, ZoneBias
from cricai_data.synthetic import generate_session as generate_synthetic_session
from cricai_worker.context import WorkerContext
from cricai_worker.pipeline import PipelineOutcome, RunPolicy, run_pipeline
from fastapi.testclient import TestClient
from sqlalchemy import select

from cricai_testing.apptest import COACH_TOKEN, PLAYER_TOKEN, auth, make_test_app

pytestmark = [pytest.mark.golden, pytest.mark.integration]

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_PKG = REPO_ROOT / "packages" / "data"
RULES_DIR = DATA_PKG / "rules"

# ``scripts`` is not an importable package: load the rules CLI by path (the
# same pattern as packages/data/tests/test_rules_io.py) under a unique module
# name so this file never collides with that one inside a single pytest run.
_spec = importlib.util.spec_from_file_location(
    "rules_io_golden_batting", REPO_ROOT / "scripts" / "rules_io.py"
)
assert _spec is not None and _spec.loader is not None
rules_io = importlib.util.module_from_spec(_spec)
sys.modules["rules_io_golden_batting"] = rules_io
_spec.loader.exec_module(rules_io)

# ---------------------------------------------------------------- the plant

#: Pinned seed: exactly 48/120 balls (40%, the T5 #1 plant) land full outside
#: off, at least one full ball sits on off stump (an honest denominator), no
#: US-J2 probe clears its gates, and no other shipped rule fires — so the
#: session's ONLY discoverable fault is the planted one.
GOLDEN_SEED = 19
N_BALLS = 120
FAULT_SHARE_OF_SESSION = 0.4  # T5 #1: "40% straight-front-foot fault"

#: The planted fault's approved rule (packages/data/rules/*.yaml, US-G2).
RULE_KEY = "front_foot_stride_short_on_full"
METRIC = "front_foot_direction_cm"
THRESHOLD_CM = 15.0
RULE_CORRECTION_TEXT = (
    "Take the front foot to the pitch of the ball so your head and knee finish over it."
)

#: Planted stride values: faulty balls sit under the rule threshold, healthy
#: balls inside the rule's own 15-25 cm target band.
FAULT_STRIDE_CM = 8.0
HEALTHY_STRIDE_CM = 22.0

#: The library drill the planner must schedule against the fault (US-J3).
DRILL_NAME = "Step to the pitch"
DRILL_SETUP = (  # digit-free on purpose: drill wording carries no claims
    "Feed full balls outside off; step the front foot to the pitch of the ball and hold the finish."
)
DRILL_BALLS = 30
DRILL_MACHINE_SETTINGS = {"line": "outside_off", "length": "full", "speed_kph": 85.0}

SESSION_DATE = datetime.date(2026, 7, 8)
CAMERAS = ("C1", "C2")  # two views: US-G6 evidence and the US-L4 sync component

# ------------------------------------------------------------ media geometry

FPS = 30.0
LEAD_FRAMES = 45  # 1.5 s of stillness: > the segmenter's 1 s min gap
BURST_FRAMES = 30  # 1 s of motion per ball: inside min/max event duration


def _write_session_video(path: Path, n_balls: int) -> int:
    """A 64x48 mp4 with one moving-block burst per ball; returns frame count."""
    total = LEAD_FRAMES + n_balls * (BURST_FRAMES + LEAD_FRAMES)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (64, 48))
    assert writer.isOpened()
    for index in range(total):
        frame = np.zeros((48, 64, 3), dtype=np.uint8)
        cycle = (index - LEAD_FRAMES) % (BURST_FRAMES + LEAD_FRAMES)
        if index >= LEAD_FRAMES and cycle < BURST_FRAMES:
            x = (cycle * 3) % 50
            frame[10:30, x : x + 10] = 255
        writer.write(frame)
    writer.release()
    return total


# ------------------------------------------------------------------ the seed


def _golden_session() -> SyntheticSession:
    """The seeded synthetic day: the machine bowls AT the fault zone (US-L2)."""
    return generate_synthetic_session(
        GOLDEN_SEED,
        (BlockSpec(n_balls=N_BALLS),),
        zone_biases=(ZoneBias(Line.OUTSIDE_OFF, Length.FULL, control_rate=0.78, weight=10.0),),
    )


def _is_fault(ball: SyntheticBall) -> bool:
    """The planted fault lives on full balls outside off (T5 #1)."""
    return ball.line is Line.OUTSIDE_OFF and ball.length is Length.FULL


def _is_rule_matched(ball: SyntheticBall) -> bool:
    """The approved rule's condition: full balls on off OR outside off."""
    return ball.length is Length.FULL and ball.line in (Line.OUTSIDE_OFF, Line.OFF)


@dataclass(frozen=True)
class Plant:
    """The exact expectations computed from the seed, never from the pipeline."""

    fault_ball_nos: tuple[int, ...]  # the degraded balls: the rule's exact hits
    n_matched: int  # the rule's denominator: every full off/outside-off ball


def _plant_expectations(synth: SyntheticSession) -> Plant:
    fault = tuple(ball.ball_no for ball in synth.balls if _is_fault(ball))
    matched = [ball for ball in synth.balls if _is_rule_matched(ball)]
    return Plant(fault_ball_nos=fault, n_matched=len(matched))


def _stride_payload(ball: SyntheticBall) -> dict[str, Any]:
    """The stored technique metric a pose producer writes (US-E3 shape)."""
    stride = FAULT_STRIDE_CM if _is_fault(ball) else HEALTHY_STRIDE_CM
    return {"value": stride, "unit": "cm", "confidence": 0.9, "proxy": False, "source": "pose"}


def _seed_session(
    ctx: WorkerContext, tmp_path: Path, synth: SyntheticSession
) -> tuple[uuid_module.UUID, uuid_module.UUID, uuid_module.UUID]:
    """Seed the machine batting day; returns (session_id, player_id, drill_id).

    Mirrors the bowling golden's seeding discipline: real media bytes in the
    store, one Video row per camera, and the coach's ground truth as manual
    tags plus stored per-ball metric payloads — never a hand-inserted finding.
    """
    local = tmp_path / "camera.mp4"
    frames = _write_session_video(local, len(synth.balls))
    video_bytes = local.read_bytes()
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=datetime.date(2015, 4, 12))
        session = SessionRow(
            player=player,
            session_date=SESSION_DATE,
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
            expected_cameras=list(CAMERAS),
            machine_settings={"speed_kph": 85.0},
        )
        db.add_all([player, session])
        db.flush()
        for camera_id, checksum in zip(CAMERAS, ("a" * 64, "b" * 64), strict=True):
            key = f"videos/golden/{camera_id}.mp4"
            ctx.store.put(key, video_bytes)
            db.add(
                Video(
                    session_id=session.id,
                    camera_id=camera_id,
                    object_key=key,
                    filename=f"{camera_id.lower()}.mp4",
                    checksum_sha256=checksum,
                    size_bytes=len(video_bytes),
                    status=VideoStatus.PROBED,
                    probe={
                        "fps": FPS,
                        "width": 64,
                        "height": 48,
                        "resolution": "64x48",
                        "codec": "mp4v",
                        "duration_s": frames / FPS,
                        "mean_luma_samples": [96.0, 128.0, 140.0],  # usable exposure
                    },
                )
            )
        for ball in synth.balls:
            db.add(
                BallTag(
                    session_id=session.id,
                    ball_no=ball.ball_no,
                    line=ball.line,
                    length=ball.length,
                    shot=ball.shot,
                    footwork=ball.footwork,
                    contact=ball.contact,
                    outcome=ball.outcome,
                    control=ball.control,
                    created_by="coach",
                )
            )
            db.add(
                BallMetrics(
                    session_id=session.id,
                    ball_no=ball.ball_no,
                    phase=MetricPhase.CONTACT,
                    metrics={METRIC: _stride_payload(ball)},
                )
            )
        drill = Drill(
            name=DRILL_NAME,
            setup=DRILL_SETUP,
            machine_settings=dict(DRILL_MACHINE_SETTINGS),
            ball_count=DRILL_BALLS,
            target_metric=METRIC,
            intent=BlockIntent.TECHNICAL,
            author="coach",
        )
        db.add(drill)
        db.flush()
        ids = (session.id, player.id, drill.id)
        db.commit()
        return ids


# ------------------------------------------------------------ DAG assertions


def _assert_full_dag_ran(ctx: WorkerContext, outcome: PipelineOutcome) -> None:
    """Every stage really SUCCEEDED and the media derivations were real."""
    assert outcome.status == "succeeded"
    for state in outcome.stages:
        assert state.status is StageStatus.SUCCEEDED, (state.stage, state.error)
    with ctx.session_factory() as db:
        rows = db.scalars(select(PipelineStage).where(PipelineStage.run_id == outcome.run_id))
        payloads = {
            row.stage: (row.output or {}).get("payload", {})
            for row in rows
            if row.status is StageStatus.SUCCEEDED
        }
    assert payloads["events"]["motion_energy_source"] == "video"  # computed, not seeded
    assert payloads["events"]["detected"] == N_BALLS
    assert payloads["clips"]["cut"] == N_BALLS * len(CAMERAS)  # real ffmpeg cuts
    assert payloads["pose"]["provider"] == "fake-pose:1"  # honest model fallback
    assert payloads["metrics"]["fused_count"] == N_BALLS
    # The REAL analysis path found exactly ONE fault: the planted one. Zero
    # probe findings and zero other-rule findings clear their gates (US-J2).
    assert payloads["analysis"]["finding_count"] == 1


def _assert_finding_is_the_planted_fault(
    ctx: WorkerContext, session_id: uuid_module.UUID, finding_id: str, plant: Plant
) -> None:
    """The headline finding is the machine-derived rule finding, exactly."""
    with ctx.session_factory() as db:
        finding = db.get(Finding, uuid_module.UUID(finding_id))
        assert finding is not None
        assert finding.session_id == session_id
        assert finding.rule_key == RULE_KEY
        assert finding.metric == METRIC
        assert finding.n == plant.n_matched
        assert finding.ball_ids == sorted(plant.fault_ball_nos)  # the exact hits
        assert finding.payload["hits"] == len(plant.fault_ball_nos)
        assert finding.payload["threshold"] == THRESHOLD_CM


# --------------------------------------------------------- report assertions


def _assert_main_correction(body: dict[str, Any], plant: Plant) -> str:
    """US-G3/G6: the correction names the fault with exact counts + evidence."""
    assert body["honesty_banner"] is None  # trusted data: no banner, no excuse
    assert body["coverage_note"] is None
    correction = body["main_correction"]
    assert correction is not None
    assert correction["text"] == (f"{RULE_CORRECTION_TEXT} Seen on {plant.n_matched} balls.")
    evidence = correction["evidence"]
    assert set(evidence) == {str(ball_no) for ball_no in plant.fault_ball_nos}
    assert all(set(cameras) == set(CAMERAS) for cameras in evidence.values())
    clips = {clip for cameras in evidence.values() for clip in cameras.values()}
    assert len(clips) == len(plant.fault_ball_nos) * len(CAMERAS)
    assert len(clips) >= 2  # US-G6: a headline finding links >= 2 clips
    assert body["secondary"] == []  # nothing else met the evidence bar
    finding_id: str = correction["finding_id"]
    return finding_id


def _assert_one_drill_one_goal(body: dict[str, Any], drill_id: uuid_module.UUID) -> None:
    """US-G3/J3: exactly one drill (the planner's library pick) and one goal."""
    assert body["drill"] == {
        "drill_id": str(drill_id),
        "text": f"{DRILL_SETUP} Do {DRILL_BALLS} balls.",
        "machine_settings": dict(DRILL_MACHINE_SETTINGS),
        "success_metric": METRIC,
    }
    assert body["goal"] == {
        "metric": METRIC,
        "target": THRESHOLD_CM,
        "condition": {"line": ["outside_off", "off"], "length": ["full"]},
    }


def _assert_claims_recompute(
    client: TestClient,
    report_id: str,
    body: dict[str, Any],
    finding_id: str,
    drill_id: uuid_module.UUID,
    plant: Plant,
) -> None:
    """US-G3/H5: every number in wording is claimed and recomputes from the DB."""
    assert {(claim["recompute_key"], claim["value"]) for claim in body["claims"]} == {
        (f"finding:{finding_id}:n", plant.n_matched),
        (f"drill:{drill_id}:ball_count", DRILL_BALLS),
        (f"finding:{finding_id}:threshold", THRESHOLD_CM),
    }
    response = client.get(f"/reports/{report_id}/claims", headers=auth(COACH_TOKEN))
    assert response.status_code == 200
    checks = response.json()
    assert len(checks) == len(body["claims"])
    for check in checks:
        assert check["match"] is True, check
        assert check["recomputed"] == check["claimed"]


def _assert_publish_ships_to_the_player(client: TestClient, report_id: str) -> None:
    """US-G3/H5: publish passes the full gate; the child sees it only after."""
    assert client.get(f"/reports/{report_id}", headers=auth(PLAYER_TOKEN)).status_code == 404
    published = client.post(f"/reports/{report_id}/publish", headers=auth(COACH_TOKEN))
    assert published.status_code == 200
    assert published.json() == {"status": "published", "reasons": []}
    player_view = client.get(f"/reports/{report_id}", headers=auth(PLAYER_TOKEN))
    assert player_view.status_code == 200
    assert player_view.json()["status"] == "published"


# ------------------------------------------------------------------ the test


def test_machine_batting_day_golden(
    pg_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T5 #1: 120 machine balls with a planted 40% straight-front-foot fault
    run the FULL production DAG; the daily report names exactly that fault with
    exact counts, >= 2 evidence clips, one drill, one goal — and publishes."""
    monkeypatch.setenv("CRICAI_DATABASE_URL", pg_url)
    monkeypatch.delenv("CRICAI_POSE_MODEL_ASSET", raising=False)
    command.upgrade(Config(str(DATA_PKG / "alembic.ini")), "head")
    engine = make_engine(pg_url)
    ctx = WorkerContext(
        session_factory=make_session_factory(engine),
        store=FsObjectStore(tmp_path / "store"),
    )

    # US-G2: the approved coaching rules ship as data — import ALL seed YAMLs
    # through the production CLI, planted rule included.
    counts = rules_io.import_rules(ctx, [RULES_DIR])
    assert counts["created"] == len(list(RULES_DIR.glob("*.yaml")))

    synth = _golden_session()
    plant = _plant_expectations(synth)
    assert len(synth.balls) == N_BALLS
    # The T5 #1 plant, exact: 40% of the day's balls carry the fault, and they
    # trip the rule (hit share above trigger_share over the matched zone).
    assert len(plant.fault_ball_nos) == int(N_BALLS * FAULT_SHARE_OF_SESSION) == 48
    assert plant.n_matched > len(plant.fault_ball_nos)  # honest denominator
    assert len(plant.fault_ball_nos) / plant.n_matched >= 0.5

    session_id, player_id, drill_id = _seed_session(ctx, tmp_path, synth)

    outcome = run_pipeline(ctx, session_id, policy=RunPolicy(max_attempts=1))
    _assert_full_dag_ran(ctx, outcome)

    # Everything below goes THROUGH THE API, as a coach (then the player).
    client = TestClient(make_test_app(tmp_path, engine=engine))
    listed = client.get("/reports", params={"player_id": str(player_id)}, headers=auth(COACH_TOKEN))
    assert listed.status_code == 200
    (report,) = listed.json()  # exactly one report: the day's DAILY draft
    assert report["kind"] == "daily"
    assert report["status"] == "draft"
    assert report["body"]["period"] == {
        "start": SESSION_DATE.isoformat(),
        "end": SESSION_DATE.isoformat(),
    }

    finding_id = _assert_main_correction(report["body"], plant)
    _assert_finding_is_the_planted_fault(ctx, session_id, finding_id, plant)
    _assert_one_drill_one_goal(report["body"], drill_id)
    _assert_claims_recompute(client, report["id"], report["body"], finding_id, drill_id, plant)
    _assert_publish_ships_to_the_player(client, report["id"])
