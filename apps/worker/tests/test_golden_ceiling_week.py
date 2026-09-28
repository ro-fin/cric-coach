"""T5 #5 golden: the ceiling week — zero-bowling plan, verbatim hash-verified warning.

A 7-day bowling ledger history sits exactly AT the US-H1 age-band limit (age
band <= 11: 16 weekly overs = 96 weighted balls, reached across three
non-consecutive bowling days so ONLY ``workload_ceiling`` trips — T4 row 1).
The planner is then exercised through BOTH production surfaces over real temp
PostgreSQL:

- the FULL pipeline (``run_pipeline``, no registry injection): the planner
  stage upserts tomorrow's DrillPlan with ZERO bowling balls (US-J3 x US-H5
  hard block), the safety stage's verdict carries ``workload_ceiling``, and
  the report embeds that verdict VERBATIM with its SHA-256 recorded (US-G3);
- the drills generate endpoint (``POST /drills/plans/{player}/{date}/generate``)
  with an explicit bowling request: the server-derived verdict still yields a
  zero-bowling plan — a caller can never talk the ceiling away (US-H5, SAF).

The warning text is the golden surface: it must be byte-identical to
:data:`cricai_coaching.safety_agent.WARNING_TEXTS` at every hop (plan row,
safety stage output, draft report, PUBLISHED report served by the API), and
its SHA-256 is pinned below with the demo-golden intentional-update
discipline — if this digest changes, someone edited the safety wording; that
must be deliberate, reviewed, and updated here in the same commit
(``docs/safety_workload.md`` mirrors the text).

Requires ffmpeg + PostgreSQL (the ``integration`` service contract); golden —
release-gating, deterministic (seeded synthetic session, fixed dates).
"""

from __future__ import annotations

import datetime
import uuid as uuid_module
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from alembic import command
from alembic.config import Config
from cricai_api.app import create_app
from cricai_api.settings import Settings
from cricai_coaching.safety_agent import WARNING_TEXTS, scheduled_bowling, sha256_text
from cricai_data.db import make_engine, make_session_factory
from cricai_data.enums import (
    BowlerSource,
    DeliveryIntensity,
    ReportStatus,
    SafetyCode,
    SessionType,
    StageStatus,
    VideoStatus,
)
from cricai_data.models import (
    BowlingLedgerEntry,
    DrillPlan,
    PipelineStage,
    Player,
    Report,
    Video,
)
from cricai_data.models import Session as SessionRow
from cricai_data.storage import FsObjectStore
from cricai_data.synthetic import BlockSpec, generate_session
from cricai_worker.agent_stages import PLANNER_ACTOR
from cricai_worker.context import WorkerContext
from cricai_worker.pipeline import RunPolicy, run_pipeline
from fastapi.testclient import TestClient
from sqlalchemy import select

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, auth

pytestmark = [pytest.mark.golden, pytest.mark.integration]

DATA_PKG = Path(__file__).resolve().parents[3] / "packages" / "data"

FPS = 30.0
LEAD_FRAMES = 45  # 1.5 s of stillness: > the segmenter's 1 s min gap
BURST_FRAMES = 30  # 1 s of motion per ball: inside min/max event duration

#: Golden digest of the US-H1/H5 workload-ceiling warning paragraph
#: (``safety_agent.WARNING_TEXTS[SafetyCode.WORKLOAD_CEILING]``). If this
#: changes, a code change altered the canonical safety wording. That must be
#: intentional: review the diff, update ``docs/safety_workload.md`` and this
#: digest in the same commit (Definition of Done: golden fixtures updated
#: intentionally).
CEILING_WARNING_SHA256 = "bf1a6840d42e4964c2b307363d19f06f1cbbfac049ee6fd75909a949220ba767"

#: Fixed dates: the triggering session and the plan it gates (US-J3: plan_date
#: = session_date + 1; the H1 window is evaluated as of the PLAN date).
SESSION_DATE = datetime.date(2026, 7, 7)
PLAN_DATE = datetime.date(2026, 7, 8)

#: Age <= 11 band (US-H1 defaults): 16 weekly overs x 6 balls = 96 weighted
#: balls. Three 32-ball spin days land the window EXACTLY at the ceiling
#: (reaching the ceiling trips it, US-H1 Gherkin) with 3 bowling days and no
#: consecutive pair — so ``workload_ceiling`` is the ONLY violation.
LEDGER_DAYS = (
    PLAN_DATE - datetime.timedelta(days=6),
    PLAN_DATE - datetime.timedelta(days=4),
    PLAN_DATE - datetime.timedelta(days=2),
)
BALLS_PER_LEDGER_DAY = 32


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


def _seed_ceiling_week(
    ctx: WorkerContext, tmp_path: Path, n_balls: int
) -> tuple[uuid_module.UUID, uuid_module.UUID]:
    """(player_id, session_id): a batting session plus a week AT the H1 limit.

    The player is 11 on every counted date (band ``max_age`` 11, ceiling 16
    overs). The ledger is manual coach entries — the batting trigger session
    itself adds nothing to the ledger, so the seeded history is exactly the
    workload the ceiling is evaluated over.
    """
    local = tmp_path / "c1.mp4"
    frames = _write_session_video(local, n_balls)
    key = "videos/golden/ceiling-C1.mp4"
    ctx.store.put(key, local.read_bytes())
    with ctx.session_factory() as db:
        player = Player(name="Mira", birthdate=datetime.date(2015, 1, 10))
        session = SessionRow(
            player=player,
            session_date=SESSION_DATE,
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
            expected_cameras=["C1"],
        )
        db.add_all([player, session])
        db.flush()
        db.add(
            Video(
                session_id=session.id,
                camera_id="C1",
                object_key=key,
                filename="c1.mp4",
                checksum_sha256="c" * 64,
                size_bytes=local.stat().st_size,
                status=VideoStatus.PROBED,
                probe={
                    "fps": FPS,
                    "width": 64,
                    "height": 48,
                    "resolution": "64x48",
                    "codec": "mp4v",
                    "duration_s": frames / FPS,
                },
            )
        )
        for entry_date in LEDGER_DAYS:
            db.add(
                BowlingLedgerEntry(
                    player_id=player.id,
                    entry_date=entry_date,
                    balls=BALLS_PER_LEDGER_DAY,
                    intensity=DeliveryIntensity.SPIN,
                    source="manual",
                    created_by="coach",
                )
            )
        db.commit()
        return player.id, session.id


def _assert_verbatim_ceiling_verdict(verdict: dict[str, Any]) -> None:
    """The pinned SafetyVerdict: ONLY workload_ceiling, byte-identical wording.

    The text is compared against :data:`WARNING_TEXTS` (the single source of
    safety wording, US-H5) and its hash against both a fresh recompute and the
    pinned golden digest — so tampering anywhere between the safety agent and
    this artifact, or an unreviewed wording edit, fails loudly.
    """
    canonical = WARNING_TEXTS[SafetyCode.WORKLOAD_CEILING]
    assert verdict["active"] is True
    assert verdict["codes"] == [SafetyCode.WORKLOAD_CEILING.value]
    assert verdict["text"] == canonical  # byte-identical, VERBATIM (US-H5)
    assert verdict["sha256"] == sha256_text(canonical) == CEILING_WARNING_SHA256


def _assert_zero_bowling(blocks: list[dict[str, Any]]) -> None:
    """The plan schedules ZERO bowling balls, by the safety agent's own scan."""
    balls, malformed = scheduled_bowling({"blocks": blocks})
    assert (balls, malformed) == (0, [])
    assert all(block["intent"] != "bowling" for block in blocks)
    assert blocks  # the batting split still plans a real (bowling-free) day


def _pg_client(pg_url: str, tmp_path: Path) -> TestClient:
    """The production FastAPI app over the SAME temp-PG database."""
    settings = Settings(
        database_url=pg_url,
        storage_root=tmp_path / "api-storage",
        parent_token=PARENT_TOKEN,
        coach_token=COACH_TOKEN,
    )
    return TestClient(create_app(settings=settings, engine=make_engine(pg_url)))


def test_golden_ceiling_week_plans_zero_bowling_with_verbatim_warning(
    pg_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T5 #5: at-limit week -> zero-bowling day; warning verbatim at every hop."""
    monkeypatch.setenv("CRICAI_DATABASE_URL", pg_url)
    monkeypatch.delenv("CRICAI_POSE_MODEL_ASSET", raising=False)
    command.upgrade(Config(str(DATA_PKG / "alembic.ini")), "head")
    ctx = WorkerContext(
        session_factory=make_session_factory(make_engine(pg_url)),
        store=FsObjectStore(tmp_path / "store"),
    )
    n_balls = len(generate_session(4242, (BlockSpec(n_balls=3),)).balls)
    player_id, session_id = _seed_ceiling_week(ctx, tmp_path, n_balls)

    # ---- surface 1: the pipeline planner stage (US-J3/H1/H5) ----------------
    outcome = run_pipeline(ctx, session_id, policy=RunPolicy(max_attempts=1))
    fate = {stage.stage: stage for stage in outcome.stages}
    assert outcome.status == "succeeded"
    for stage in ("planner", "safety", "report"):
        assert fate[stage].status is StageStatus.SUCCEEDED, (stage, fate[stage].error)

    with ctx.session_factory() as db:
        rows = db.scalars(select(PipelineStage).where(PipelineStage.run_id == outcome.run_id)).all()
        payloads = {
            row.stage: (row.output or {}).get("payload", {})
            for row in rows
            if row.status is StageStatus.SUCCEEDED
        }
        plan = db.scalar(
            select(DrillPlan).where(
                DrillPlan.player_id == player_id, DrillPlan.plan_date == PLAN_DATE
            )
        )
        report = db.scalar(select(Report).where(Report.session_id == session_id))

    # The planner stage saw the active ceiling and wrote a zero-bowling plan.
    assert payloads["planner"]["plan_preserved"] is False
    assert payloads["planner"]["safety_active"] is True
    assert payloads["planner"]["safety_codes"] == [SafetyCode.WORKLOAD_CEILING.value]
    assert plan is not None
    assert plan.created_by == PLANNER_ACTOR
    _assert_zero_bowling(list(plan.blocks))
    _assert_verbatim_ceiling_verdict(dict(plan.safety))
    assert plan.safety_sha256 == CEILING_WARNING_SHA256

    # The independent safety stage re-derived the same verdict byte-for-byte
    # (US-H5 defense in depth) and blessed the persisted zero-bowling plan.
    _assert_verbatim_ceiling_verdict(dict(payloads["safety"]))

    # The report embeds the verdict VERBATIM and records its hash (US-G3/H5).
    assert report is not None
    _assert_verbatim_ceiling_verdict(dict(report.body["safety"]))
    assert report.body["safety"] == payloads["safety"]  # the run's own verdict, untouched
    assert report.safety_sha256 == CEILING_WARNING_SHA256

    # ---- surface 2: the API — publish gate + drills generate (US-H5/J3) -----
    client = _pg_client(pg_url, tmp_path)

    published = client.post(f"/reports/{report.id}/publish", headers=auth(COACH_TOKEN))
    assert published.status_code == 200, published.text
    assert published.json() == {"status": "published", "reasons": []}
    served = client.get(f"/reports/{report.id}", headers=auth(COACH_TOKEN)).json()
    assert served["status"] == ReportStatus.PUBLISHED.value
    _assert_verbatim_ceiling_verdict(served["body"]["safety"])  # byte-identical as served
    assert served["safety_sha256"] == CEILING_WARNING_SHA256

    # Even an explicit bowling request cannot talk the ceiling away: the
    # server-derived verdict yields a zero-bowling plan (SAF, US-H5).
    generated = client.post(
        f"/drills/plans/{player_id}/{PLAN_DATE.isoformat()}/generate",
        json={"session_id": str(session_id), "bowling_request_balls": 30},
        headers=auth(COACH_TOKEN),
    )
    assert generated.status_code == 200, generated.text  # regenerates the existing row
    api_plan = generated.json()
    _assert_zero_bowling(api_plan["blocks"])
    _assert_verbatim_ceiling_verdict(api_plan["safety"])
    assert api_plan["safety_sha256"] == CEILING_WARNING_SHA256
