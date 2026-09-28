"""T5 #2 golden: one MIXED day — machine + coach-throwdown + player-bowling blocks.

The whole day is driven through the REAL production surface on real temp
PostgreSQL: player/session/checklist/camera/lifecycle/blocks/tags/upload via
the API (US-B1/A5/A1/A3/B3/B4/B2), synthetic media through the full pipeline
DAG (US-J1; same cv2-burst technique as the Phase-6 full-DAG template), and
the ledger via the US-H1 workload API. Nothing downstream is hand-planted.

What this golden pins (T5 #2 acceptance):

- **Per-ball block attribution** — balls inherit their block BY TIMESTAMP
  (US-B3): the real detected events resolve to the declared machine /
  coach-throwdown / player-bowling blocks via ``GET /blocks/assign`` and via
  the canonical BallRecord assembler (US-G1), matching the synthetic plan
  ball-for-ball with no tag-level block hints.
- **US-H2 split reconciliation** — the day's plan-vs-actual over the tagged
  blocks matches hand-computed deviations exactly, including the pinned
  boundary rule (|deviation| == alert threshold does NOT flag) and the
  kid-first fun-block predicate.
- **Workload ledger exact** (US-H1 x the documented MIXED limitation) — a
  MIXED session contributes NO ``auto_backfill`` rows (per
  ``cricai_worker.backfill_workload``: the schema cannot attribute per-ball
  bowler identity inside a mixed session, so MIXED contributes only via
  explicit manual entries); the manual entries then drive exact rolling-7
  math, with the coach-throwdown block's arm throws weighing 0 toward the
  overs ceiling and never creating a bowling day.

Deterministic: fixed seed, fixed dates, cv2-generated media, explicit
``end=`` on every rolling-window read — no wall-clock dependence.
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import uuid as uuid_module
from collections import Counter
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from alembic import command
from alembic.config import Config
from cricai_coaching.report import batting_split_block
from cricai_coaching.safety_config import DEFAULT_SAFETY_CONFIG, SAFETY_CONFIG_SEED_VERSION
from cricai_coaching.workload import (
    IntentReconciliation,
    SplitReconciliation,
    reconcile_batting_split,
)
from cricai_data.ballrecord import session_ball_records
from cricai_data.db import make_engine, make_session_factory
from cricai_data.enums import BlockIntent, BowlerSource, SessionType, StageStatus
from cricai_data.models import PipelineStage, SafetyConfig
from cricai_data.storage import FsObjectStore
from cricai_data.synthetic import BlockSpec, SyntheticSession, generate_session
from cricai_worker.backfill_workload import BackfillResult, backfill_workload
from cricai_worker.context import WorkerContext
from cricai_worker.pipeline import PipelineOutcome, RunPolicy, run_pipeline
from cricai_worker.stage_adapters import SKIPPED_NOT_BOWLING
from fastapi.testclient import TestClient
from sqlalchemy import select

from cricai_testing.apptest import PARENT_TOKEN, auth, make_test_app

pytestmark = [pytest.mark.integration, pytest.mark.golden]

DATA_PKG = Path(__file__).resolve().parents[3] / "packages" / "data"

# Same media technique as the Phase-6 full-DAG template: one moving-block
# burst per ball on the C1 reference camera, 1.5 s of stillness between balls.
FPS = 30.0
LEAD_FRAMES = 45  # 1.5 s of stillness: > the segmenter's 1 s min gap
BURST_FRAMES = 30  # 1 s of motion per ball: inside min/max event duration

GOLDEN_SEED = 7002
SESSION_DATE = datetime.date(2026, 7, 8)
BIRTHDATE = datetime.date(2014, 11, 20)  # age 11 on SESSION_DATE: 16-over band

#: The day's three blocks (US-B3): machine batting, coach throwdowns, and the
#: kid's own bowling — 3 synthetic balls each, 9 on the session timeline.
MIXED_BLOCK_SPECS: tuple[BlockSpec, ...] = (
    BlockSpec(n_balls=3, bowler_source=BowlerSource.MACHINE, intent=BlockIntent.TECHNICAL),
    BlockSpec(n_balls=3, bowler_source=BowlerSource.COACH, intent=BlockIntent.DECISION),
    BlockSpec(n_balls=3, bowler_source=BowlerSource.HUMAN, intent=BlockIntent.SPIN_SPECIFIC),
)

#: The coach's plan for the day (US-H2: split configurable per phase of
#: season) — chosen so the hand-computed deviations cover the flag boundary:
#: technical lands AT the 25% threshold (must NOT flag), decision beyond it,
#: spin_specific on plan, and the protected fun block skipped entirely. Seeded
#: as the GOVERNING batting split (``_seed_season_plan_config``) so the shipped
#: report reconciles the day against this exact plan — production reconciles
#: against ``config['batting_split']['blocks']``, never a ``plan=`` override.
SEASON_PLAN: dict[str, int] = {"technical": 4, "decision": 2, "spin_specific": 3, "fun": 1}


def _burst_start_s(ball_no: int) -> float:
    """Second at which ball ``ball_no``'s motion burst begins on the timeline."""
    return (LEAD_FRAMES + (ball_no - 1) * (BURST_FRAMES + LEAD_FRAMES)) / FPS


def _burst_end_s(ball_no: int) -> float:
    return _burst_start_s(ball_no) + BURST_FRAMES / FPS


#: Block boundaries sit mid-gap between the surrounding bursts, so the pinned
#: half-open ``[start_s, end_s)`` assignment (US-B3) is what is under test —
#: not detector edge effects. Balls 1-3 / 4-6 / 7-9 per MIXED_BLOCK_SPECS.
BLOCK2_START_S = (_burst_end_s(3) + _burst_start_s(4)) / 2
BLOCK3_START_S = (_burst_end_s(6) + _burst_start_s(7)) / 2


def _write_session_video(path: Path, n_balls: int) -> None:
    """A 64x48 mp4 with one moving-block burst per ball (Phase-6 template)."""
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


def _post(client: TestClient, url: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = client.post(url, json=payload, headers=auth(PARENT_TOKEN))
    assert response.status_code == 201, (url, response.status_code, response.text)
    body: dict[str, Any] = response.json()
    return body


def _get(client: TestClient, url: str, params: dict[str, Any] | None = None) -> Any:
    response = client.get(url, params=params, headers=auth(PARENT_TOKEN))
    assert response.status_code == 200, (url, response.status_code, response.text)
    return response.json()


def _wire(pg_url: str, tmp_path: Path) -> tuple[WorkerContext, TestClient]:
    """Real temp-PG schema + one engine shared by the API app and the worker."""
    command.upgrade(Config(str(DATA_PKG / "alembic.ini")), "head")
    engine = make_engine(pg_url)
    ctx = WorkerContext(
        session_factory=make_session_factory(engine),
        store=FsObjectStore(tmp_path / "storage"),  # same root the app serves
    )
    return ctx, TestClient(make_test_app(tmp_path, engine))


def _seed_season_plan_config(ctx: WorkerContext) -> None:
    """Make SEASON_PLAN the GOVERNING batting split (US-H2/H4: latest config
    version wins), so the daily report's shipped block reconciles the day
    against the same plan the block-level assertion pins. Everything else is
    copied verbatim from the v1 seed, so the workload ledger math below (age
    bands, intensity weights, ceilings) is unchanged — only the batting-split
    plan differs. A direct insert is the fixture equivalent of the coach having
    versioned a season plan; production resolves it the same way (version DESC).
    """
    config = copy.deepcopy(DEFAULT_SAFETY_CONFIG)
    config["batting_split"]["blocks"] = dict(SEASON_PLAN)
    config["batting_split"]["daily_balls"] = sum(SEASON_PLAN.values())
    with ctx.session_factory() as db:
        db.add(
            SafetyConfig(
                version=SAFETY_CONFIG_SEED_VERSION + 1,
                config=config,
                approved_by="coach",
                reason="T5 #2 golden: season plan for the day (US-H2)",
            )
        )
        db.commit()


def _create_started_session(client: TestClient) -> tuple[str, str]:
    """(player_id, session_id): a MIXED session taken to RECORDING via the API.

    The machine block means the US-A5 safety checklist gates the start — the
    ack is part of the golden path, never bypassed.
    """
    player = _post(client, "/players", {"name": "Arjun", "birthdate": BIRTHDATE.isoformat()})
    session = _post(
        client,
        "/sessions",
        {
            "player_id": player["id"],
            "date": SESSION_DATE.isoformat(),
            "session_type": SessionType.MIXED.value,
            "bowler_source": BowlerSource.MACHINE.value,
            "machine_settings": {"speed_kph": 85.0, "length": "good"},
        },
    )
    session_id: str = session["id"]
    checklist = _get(client, "/checklists/machine")
    _post(
        client,
        f"/sessions/{session_id}/checklist-ack",
        {"items": {item["id"]: True for item in checklist["items"]}, "acked_by": "parent"},
    )
    _post(
        client,
        "/cameras",
        {
            "camera_id": "C1",
            "position_label": "behind bowler",
            "xyz_offset_m": {"x": 0.0, "y": -3.0, "z": 0.0},
            "height_m": 1.6,
            "fps": 30,
            "resolution": "1280x720",
        },
    )
    start = client.post(
        f"/sessions/{session_id}/start", json={"cameras": ["C1"]}, headers=auth(PARENT_TOKEN)
    )
    assert start.status_code == 200, start.text
    assert start.json()["cameras"] == ["C1"]
    player_id: str = player["id"]
    return player_id, session_id


def _declare_blocks(client: TestClient, session_id: str) -> dict[int, dict[str, Any]]:
    """Create the three blocks via the API (auto-closing the open one) and
    return them by block_no after asserting the declared timeline (US-B3)."""
    starts = (0.0, BLOCK2_START_S, BLOCK3_START_S)
    for spec, start_s in zip(MIXED_BLOCK_SPECS, starts, strict=True):
        payload: dict[str, Any] = {
            "start_s": start_s,
            "bowler_source": spec.bowler_source.value,
            "intent": spec.intent.value,
            "auto_close_open": start_s > 0.0,
        }
        if spec.bowler_source is BowlerSource.MACHINE:
            payload["machine_settings"] = {"speed_kph": 85.0, "length": "good"}
        _post(client, f"/sessions/{session_id}/blocks", payload)

    listing = _get(client, f"/sessions/{session_id}/blocks")
    assert listing["gaps"] == []  # the blocks tile the whole timeline
    assert [
        (b["block_no"], b["start_s"], b["end_s"], b["bowler_source"], b["intent"])
        for b in listing["blocks"]
    ] == [
        (1, 0.0, BLOCK2_START_S, "machine", "technical"),
        (2, BLOCK2_START_S, BLOCK3_START_S, "coach", "decision"),
        (3, BLOCK3_START_S, None, "human", "spin_specific"),  # still open
    ]
    return {b["block_no"]: b for b in listing["blocks"]}


def _tag_balls(client: TestClient, session_id: str, plan: SyntheticSession) -> None:
    """Manual per-ball tags (US-B4) WITHOUT block hints: attribution must come
    from timestamps alone, which is exactly what this golden pins."""
    for ball in plan.balls:
        _post(
            client,
            f"/sessions/{session_id}/tags",
            {
                "ball_no": ball.ball_no,
                "line": ball.line.value,
                "length": ball.length.value,
                "shot": ball.shot.value,
                "footwork": ball.footwork.value,
                "contact": ball.contact.value,
                "outcome": ball.outcome.value,
                "control": ball.control,
            },
        )


def _upload_c1(client: TestClient, session_id: str, tmp_path: Path, n_balls: int) -> None:
    """The session's C1 footage through the real US-B2 multipart upload."""
    local = tmp_path / "c1.mp4"
    _write_session_video(local, n_balls)
    data = local.read_bytes()
    upload = _post(
        client,
        f"/sessions/{session_id}/videos/uploads",
        {
            "camera_id": "C1",
            "filename": "c1.mp4",
            "declared_checksum": hashlib.sha256(data).hexdigest(),
            "declared_size": len(data),
        },
    )
    part = client.put(
        f"/videos/uploads/{upload['upload_id']}/parts/1",
        params={"store_upload_id": upload["store_upload_id"]},
        content=data,
        headers=auth(PARENT_TOKEN),
    )
    assert part.status_code == 200, part.text
    done = client.post(
        f"/videos/uploads/{upload['upload_id']}/complete",
        json={"store_upload_id": upload["store_upload_id"], "part_count": 1},
        headers=auth(PARENT_TOKEN),
    )
    assert done.status_code == 200, done.text
    video = done.json()["video"]
    assert video["status"] == "probed"  # real ffprobe measured the real bytes
    assert video["probe"]["fps"] == FPS


def _run_full_dag(ctx: WorkerContext, session_id: str) -> PipelineOutcome:
    """The production DAG over the uploaded media; MIXED semantics asserted.

    Every media + agent stage executes for real; the three bowling stages are
    honest gated no-ops for a MIXED session (US-I2-I6 x US-J1) — the very gap
    that makes the H1 ledger manual-entry-only for mixed days.
    """
    outcome = run_pipeline(ctx, uuid_module.UUID(session_id), policy=RunPolicy(max_attempts=1))
    fate = {s.stage: s for s in outcome.stages}
    assert outcome.status == "succeeded"
    for stage, state in fate.items():
        assert state.status is StageStatus.SUCCEEDED, (stage, state.error)
        assert state.resumed is False

    with ctx.session_factory() as db:
        rows = db.scalars(select(PipelineStage).where(PipelineStage.run_id == outcome.run_id)).all()
    payloads = {row.stage: (row.output or {}).get("payload", {}) for row in rows}
    n_balls = len(generate_session(GOLDEN_SEED, MIXED_BLOCK_SPECS).balls)
    assert payloads["events"]["motion_energy_source"] == "video"  # computed, not seeded
    assert payloads["events"]["detected"] == n_balls
    assert payloads["metrics"]["fused_count"] == n_balls
    for stage in ("bowling_action", "bowling_flight", "classify_variations"):
        assert payloads[stage] == SKIPPED_NOT_BOWLING
    return outcome


def _assert_block_attribution(
    ctx: WorkerContext,
    client: TestClient,
    session_id: str,
    plan: SyntheticSession,
    blocks_by_no: dict[int, dict[str, Any]],
) -> dict[str, int]:
    """Balls inherit their block by timestamp (US-B3): the REAL detected
    events resolve to the planned blocks through both production surfaces.
    Returns actual ball counts per block id for the US-H2 reconciliation."""
    events = _get(client, f"/sessions/{session_id}/events")
    assert [e["ball_no"] for e in events] == [b.ball_no for b in plan.balls]
    for ball, event in zip(plan.balls, events, strict=True):
        assigned = _get(
            client,
            f"/sessions/{session_id}/blocks/assign",
            params={"t_s": event["start_ms"] / 1000.0},
        )
        assert assigned["block_no"] == ball.block_no, (ball.ball_no, assigned)

    # The canonical BallRecord (US-G1) agrees: block, bowler and the pinned
    # MIXED rule (a block-covered ball is batting practice) per ball.
    with ctx.session_factory() as db:
        records = session_ball_records(db, uuid_module.UUID(session_id))
    for ball, record in zip(plan.balls, records, strict=True):
        assert record["ball_id"] == ball.ball_no
        assert record["block_id"] == blocks_by_no[ball.block_no]["id"], ball
        assert record["bowler"] == ball.bowler_source.value
        assert record["mode"] == "batting"  # MIXED + block context (US-G1 rule)
        assert record["line"] == ball.line.value  # the manual tag won (US-B4)
    counts = Counter(str(record["block_id"]) for record in records)
    return dict(counts)


def _assert_h2_reconciliation(
    client: TestClient,
    blocks_by_no: dict[int, dict[str, Any]],
    balls_by_block_id: dict[str, int],
) -> SplitReconciliation:
    """US-H2: the day's plan-vs-actual matches the hand-computed deviations.

    The actual counts come from the pipeline's own block attribution; the
    governing config comes from the API — now the seeded season plan (alert
    threshold 25%). Reconciled with NO ``plan=`` override so this call is the
    SAME one production makes (``generate_report`` reconciles the day against
    ``config['batting_split']['blocks']``). Every number below is hand-computed:
    technical (3-4)/4 = -25% sits exactly AT the threshold so it must NOT flag;
    decision (3-2)/2 = +50% flags; spin_specific is on plan; the skipped fun
    block is -100% and flags, and the kid-first predicate reports the fun block
    missing (US-H2 AC). Returns the reconciliation so the caller can pin the
    shipped report block against it end-to-end.
    """
    config = _get(client, "/workload/safety-config")["config"]
    assert config["batting_split"]["blocks"] == SEASON_PLAN  # the seeded plan governs
    day_blocks = [
        {
            "intent": block["intent"],
            "balls": balls_by_block_id.get(block["id"], 0),
            "drill_id": None,
        }
        for block in blocks_by_no.values()
    ]
    reconciliation = reconcile_batting_split(day_blocks, config)
    assert reconciliation == SplitReconciliation(
        intents=(
            IntentReconciliation(
                intent="technical",
                planned_balls=4,
                actual_balls=3,
                deviation_pct=-25.0,
                flagged=False,
            ),
            IntentReconciliation(
                intent="decision",
                planned_balls=2,
                actual_balls=3,
                deviation_pct=50.0,
                flagged=True,
            ),
            IntentReconciliation(
                intent="spin_specific",
                planned_balls=3,
                actual_balls=3,
                deviation_pct=0.0,
                flagged=False,
            ),
            IntentReconciliation(
                intent="fun",
                planned_balls=1,
                actual_balls=0,
                deviation_pct=-100.0,
                flagged=True,
            ),
        ),
        planned_total=10,
        actual_total=9,
        flagged_intents=("decision", "fun"),
        fun_block_intact=False,
    )
    return reconciliation


def _split_rows(block: dict[str, Any]) -> list[tuple[str, int, int, float, bool]]:
    """A batting_split block's per-intent rows as comparable tuples. deviation_pct
    is coerced to float so the JSONB round-trip's loose rendering (0 vs 0.0) does
    not defeat the shipped-vs-hand-computed equality."""
    return [
        (
            row["intent"],
            row["planned_balls"],
            row["actual_balls"],
            float(row["deviation_pct"]),
            row["flagged"],
        )
        for row in block["intents"]
    ]


def _ledger(client: TestClient, player_id: str) -> list[dict[str, Any]]:
    return list(_get(client, f"/workload/players/{player_id}/ledger"))


def _summary(client: TestClient, player_id: str, end: datetime.date) -> dict[str, Any]:
    windows = _get(
        client, f"/workload/players/{player_id}/summary", params={"end": end.isoformat()}
    )
    assert len(windows) == 1
    window: dict[str, Any] = windows[0]
    return window


def _assert_workload_ledger(
    ctx: WorkerContext, client: TestClient, session_id: str, player_id: str
) -> None:
    """US-H1 x the documented MIXED limitation: no auto rows, manual rows
    drive exact rolling-7 math, throwdowns weigh 0 toward the ceiling."""
    # The pipeline's safety stage derived NOTHING into the ledger (it only
    # backfills BOWLING sessions), and the backfill job agrees: an explicit
    # MIXED target is processed but creates no rows; the batch run does not
    # even target it (cricai_worker.backfill_workload pinned MIXED semantics).
    assert _ledger(client, player_id) == []
    explicit = backfill_workload(ctx, uuid_module.UUID(session_id))
    assert explicit == BackfillResult(sessions_processed=1, entries_created=0, entries_deleted=0)
    batch = backfill_workload(ctx)
    assert batch == BackfillResult(sessions_processed=0, entries_created=0, entries_deleted=0)
    assert _ledger(client, player_id) == []

    # The parent records the coach-throwdown block manually: arm throws are in
    # the ledger but weigh 0 toward the overs ceiling and never create a
    # bowling day (US-H1 pinned decision; ECB/CA rationale in the config seed).
    _post(
        client,
        f"/workload/players/{player_id}/ledger",
        {
            "entry_date": SESSION_DATE.isoformat(),
            "balls": 3,
            "intensity": "throwdown",
            "session_id": session_id,
            "note": "coach throwdown block (block 2)",
        },
    )
    throwdown_only = _summary(client, player_id, SESSION_DATE)
    assert throwdown_only == {
        "window_start": (SESSION_DATE - datetime.timedelta(days=6)).isoformat(),
        "window_end": SESSION_DATE.isoformat(),
        "weighted_balls": 0.0,
        "weighted_overs": 0.0,
        "bowling_days": [],  # a throwdown-only day is NOT a bowling day
        "consecutive_day_pairs": 0,
        "band_max_age": 11,
        "ceiling_overs": 16.0,
        "violations": [],
        "remaining_balls": 96,  # the full 16-over allowance is untouched
    }

    # The player-bowling block (leg-spin, 3 balls) plus prior history placed
    # exactly on the rolling-7 boundary: day D-6 is inside the [D-6, D]
    # window, day D-7 is outside it (US-H1: "in any rolling 7").
    _post(
        client,
        f"/workload/players/{player_id}/ledger",
        {
            "entry_date": SESSION_DATE.isoformat(),
            "balls": 3,
            "intensity": "spin",
            "session_id": session_id,
            "note": "player bowling block (block 3)",
        },
    )
    _post(
        client,
        f"/workload/players/{player_id}/ledger",
        {
            "entry_date": (SESSION_DATE - datetime.timedelta(days=6)).isoformat(),
            "balls": 12,
            "intensity": "pace_intent",
        },
    )
    _post(
        client,
        f"/workload/players/{player_id}/ledger",
        {
            "entry_date": (SESSION_DATE - datetime.timedelta(days=7)).isoformat(),
            "balls": 60,
            "intensity": "pace_intent",
        },
    )

    # Hand-computed window ending on the session day: 12 (D-6) + 3 spin + 3x0
    # throwdown = 15 weighted balls = 2.5 overs; the 60-ball day at D-7 has
    # rolled out; two bowling days, no consecutive pair, allowance 96-15=81.
    assert _summary(client, player_id, SESSION_DATE) == {
        "window_start": (SESSION_DATE - datetime.timedelta(days=6)).isoformat(),
        "window_end": SESSION_DATE.isoformat(),
        "weighted_balls": 15.0,
        "weighted_overs": 2.5,
        "bowling_days": [
            (SESSION_DATE - datetime.timedelta(days=6)).isoformat(),
            SESSION_DATE.isoformat(),
        ],
        "consecutive_day_pairs": 0,
        "band_max_age": 11,
        "ceiling_overs": 16.0,
        "violations": [],
        "remaining_balls": 81,
    }
    # The window ending one day earlier still contains D-7: 60 + 12 = 72
    # weighted balls = 12.0 overs, one consecutive-day pair (at the allowed
    # maximum, not over it) — both rolling-7 edges verified.
    assert _summary(client, player_id, SESSION_DATE - datetime.timedelta(days=1)) == {
        "window_start": (SESSION_DATE - datetime.timedelta(days=7)).isoformat(),
        "window_end": (SESSION_DATE - datetime.timedelta(days=1)).isoformat(),
        "weighted_balls": 72.0,
        "weighted_overs": 12.0,
        "bowling_days": [
            (SESSION_DATE - datetime.timedelta(days=7)).isoformat(),
            (SESSION_DATE - datetime.timedelta(days=6)).isoformat(),
        ],
        "consecutive_day_pairs": 1,
        "band_max_age": 11,
        "ceiling_overs": 16.0,
        "violations": [],
        "remaining_balls": 24,
    }

    # Re-running the backfill never touches manual rows (US-H1 audit rule).
    assert backfill_workload(ctx, uuid_module.UUID(session_id)) == BackfillResult(
        sessions_processed=1, entries_created=0, entries_deleted=0
    )
    rows = _ledger(client, player_id)
    assert [(row["entry_date"], row["balls"], row["intensity"]) for row in rows] == [
        ((SESSION_DATE - datetime.timedelta(days=7)).isoformat(), 60, "pace_intent"),
        ((SESSION_DATE - datetime.timedelta(days=6)).isoformat(), 12, "pace_intent"),
        (SESSION_DATE.isoformat(), 3, "throwdown"),
        (SESSION_DATE.isoformat(), 3, "spin"),
    ]
    assert all(row["source"] == "manual" for row in rows)


def test_golden_mixed_session_end_to_end(
    pg_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T5 #2: block attribution, US-H2 reconciliation, US-H1 ledger — exact."""
    monkeypatch.setenv("CRICAI_DATABASE_URL", pg_url)
    monkeypatch.delenv("CRICAI_POSE_MODEL_ASSET", raising=False)
    ctx, client = _wire(pg_url, tmp_path)
    _seed_season_plan_config(ctx)  # governs the shipped report's batting split
    plan = generate_session(GOLDEN_SEED, MIXED_BLOCK_SPECS)
    assert [b.block_no for b in plan.balls] == [1, 1, 1, 2, 2, 2, 3, 3, 3]

    player_id, session_id = _create_started_session(client)
    blocks_by_no = _declare_blocks(client, session_id)
    _tag_balls(client, session_id, plan)
    _upload_c1(client, session_id, tmp_path, n_balls=len(plan.balls))
    stop = client.post(f"/sessions/{session_id}/stop", json={}, headers=auth(PARENT_TOKEN))
    assert stop.status_code == 200, stop.text
    assert stop.json()["degraded"] is False
    assert stop.json()["missing_views"] == []

    _run_full_dag(ctx, session_id)

    # The agent tail shipped a daily report for the mixed day (US-G3/J1).
    reports = _get(client, "/reports", params={"player_id": player_id, "kind": "daily"})
    assert [r["session_id"] for r in reports] == [session_id]

    balls_by_block_id = _assert_block_attribution(ctx, client, session_id, plan, blocks_by_no)
    reconciliation = _assert_h2_reconciliation(client, blocks_by_no, balls_by_block_id)

    # End-to-end US-H2 (T5 #2 AC): the SAME reconciliation the block-level
    # assertion pins is the one the shipped daily report carries — production
    # reconciles the day against the governing config (the seeded season plan),
    # so the report body's batting_split equals batting_split_block(reconciliation)
    # field-for-field. deviation_pct is coerced to float because the JSONB
    # round-trip through the API renders numbers loosely (e.g. 0 vs 0.0).
    shipped = reports[0]["body"]["batting_split"]
    expected = batting_split_block(reconciliation)
    assert shipped["planned_total"] == expected["planned_total"]
    assert shipped["actual_total"] == expected["actual_total"]
    assert shipped["flagged_intents"] == expected["flagged_intents"]
    assert shipped["fun_block_intact"] == expected["fun_block_intact"]
    assert shipped["note"] == expected["note"]
    assert _split_rows(shipped) == _split_rows(expected)

    _assert_workload_ledger(ctx, client, session_id, player_id)
