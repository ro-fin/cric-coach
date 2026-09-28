#!/usr/bin/env python3
"""Local development stack for the dashboard (Phase 8, T1).

Boots the REAL cricai_api app on an in-memory SQLite database, seeds a small,
honest demo lab through the same rows and API calls the production pipeline
and the API tests use, then runs the Next.js dev server pointed at it. No
PostgreSQL, Redis, ffmpeg or camera footage is needed, so it works on the
Windows build machine and inside Playwright.

Usage::

    uv run scripts/dev_stack.py              # API :8000 + seeded demo + web :3000
    uv run scripts/dev_stack.py --api-only   # API only (e.g. for `pnpm dev` elsewhere)
    uv run scripts/dev_stack.py --no-seed    # empty database: exercise empty states
    uv run scripts/dev_stack.py --web-mode prod   # production build + next start

Sign in to the dashboard with one of the printed role tokens. They are fixed
development values, never deployment secrets.

What the demo contains (see ``seed_demo``):

- player "Arjun" with two batting sessions;
- session 1 ANALYZED: 24 tagged balls with events and C1/C2 clips, a rules
  finding on 12 of them, and a daily report that went through the real
  publish gate (so the player can see it);
- session 2 CAPTURED and DEGRADED (camera C2 missing), 6 tagged balls;
- a second daily report left as a DRAFT held by the coach review gate, so the
  review queue is not empty.

Clip object keys point at no media: playback shows the player's honest
"clip unavailable" state unless ``NEXT_PUBLIC_CRICAI_MEDIA_BASE`` serves files.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import uvicorn
from cricai_api.app import create_app
from cricai_api.settings import Settings
from cricai_data.db import create_all
from cricai_data.enums import (
    ClipStatus,
    Contact,
    EventSource,
    Footwork,
    Length,
    Line,
    Outcome,
    ReportKind,
    Shot,
)
from cricai_data.lifecycle import SessionState
from cricai_data.models import BallEvent, BallTag, Base, Clip, Finding, Report
from cricai_data.models import Session as SessionRow
from fastapi import APIRouter, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = REPO_ROOT / "apps" / "web"

PARENT_TOKEN = "dev-parent-token"
COACH_TOKEN = "dev-coach-token"
PLAYER_TOKEN = "dev-player-token"

DEMO_DATE = date(2026, 9, 26)
DRAFT_DATE = date(2026, 9, 27)
CAMERAS = ("C1", "C2")
FAULT_BALLS = 12
ANALYZED_BALLS = 24
DEGRADED_BALLS = 6


@dataclass(frozen=True)
class SeedSummary:
    player_id: str
    analyzed_session_id: str
    degraded_session_id: str
    published_report_id: str
    draft_report_id: str


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def make_engine() -> Engine:
    """One shared in-memory SQLite database for every thread of this process."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_all(engine)
    return engine


def make_app(storage_root: Path, engine: Engine) -> FastAPI:
    settings = Settings(
        database_url="sqlite://",  # unused: engine injected
        storage_root=storage_root,
        parent_token=PARENT_TOKEN,
        coach_token=COACH_TOKEN,
        player_token=PLAYER_TOKEN,
    )
    return create_app(settings=settings, engine=engine)


@contextmanager
def _db(app: FastAPI) -> Iterator[OrmSession]:
    db: OrmSession = app.state.session_factory()
    try:
        yield db
        db.commit()
    finally:
        db.close()


def _post(client: TestClient, path: str, body: dict[str, Any], token: str) -> dict[str, Any]:
    response = client.post(path, json=body, headers=_auth(token))
    if response.status_code >= 300:
        raise RuntimeError(f"seed POST {path} -> {response.status_code}: {response.text}")
    payload: dict[str, Any] = response.json()
    return payload


def _ball_tag(session_id: uuid.UUID, ball_no: int, *, fault: bool) -> BallTag:
    """A plausible tag. Fault balls: full outside off, driven, edged, not in control."""
    if fault:
        return BallTag(
            session_id=session_id,
            ball_no=ball_no,
            line=Line.OUTSIDE_OFF,
            length=Length.FULL,
            shot=Shot.COVER_DRIVE,
            footwork=Footwork.FRONT,
            contact=Contact.EDGE,
            outcome=Outcome.EDGED,
            control=False,
            created_by="parent",
        )
    lines = (Line.OFF, Line.MIDDLE, Line.LEG)
    lengths = (Length.GOOD, Length.FULL, Length.SHORT)
    shots = (Shot.DEFEND, Shot.STRAIGHT_DRIVE, Shot.PULL)
    footwork = (Footwork.FRONT, Footwork.FRONT, Footwork.BACK)
    index = ball_no % 3
    return BallTag(
        session_id=session_id,
        ball_no=ball_no,
        line=lines[index],
        length=lengths[index],
        shot=shots[index],
        footwork=footwork[index],
        contact=Contact.MIDDLE,
        outcome=Outcome.CONTROLLED_GROUND_SHOT,
        control=True,
        created_by="parent",
    )


def _seed_balls(
    db: OrmSession,
    session_id: uuid.UUID,
    count: int,
    cameras: tuple[str, ...],
    is_fault: Callable[[int], bool],
) -> dict[int, dict[str, str]]:
    """Tags, events and CUT clips for balls 1..count; returns ball → camera → clip id."""
    clips: dict[int, dict[str, str]] = {}
    for ball_no in range(1, count + 1):
        start = ball_no * 10_000
        db.add(_ball_tag(session_id, ball_no, fault=is_fault(ball_no)))
        db.add(
            BallEvent(
                session_id=session_id,
                ball_no=ball_no,
                start_ms=start,
                release_ms=start + 500,
                contact_ms=start + 900,
                end_ms=start + 6000,
                confidence=0.92,
                source=EventSource.AUTO,
                detector_version="dev-seed",
            )
        )
        for camera_id in cameras:
            clip = Clip(
                session_id=session_id,
                ball_no=ball_no,
                camera_id=camera_id,
                object_key=f"sessions/{session_id}/clips/b{ball_no}_{camera_id}.mp4",
                start_ms=start,
                end_ms=start + 6000,
                status=ClipStatus.CUT,
            )
            db.add(clip)
            db.flush()
            clips.setdefault(ball_no, {})[camera_id] = str(clip.id)
    return clips


def _report_body(
    kind: str, day: date, finding_id: str, evidence: dict[str, dict[str, str]], n: int
) -> dict[str, Any]:
    return {
        "kind": kind,
        "period": {"start": day.isoformat(), "end": day.isoformat()},
        "main_correction": {
            "finding_id": finding_id,
            "text": (
                "Get your front foot to the pitch of the ball on full balls outside off. "
                f"Seen on {n} balls."
            ),
            "evidence": evidence,
        },
        "drill": {
            "drill_id": None,
            "text": (
                "Front-foot ladder on full balls outside off: step to the line, head over the ball."
            ),
            "machine_settings": {"speed_kph": 90, "length": "full", "line": "outside_off"},
            "success_metric": "control_pct",
        },
        "goal": {
            "metric": "control_pct",
            "target": None,
            "condition": {"line": "outside_off", "length": "full"},
        },
        "secondary": [],
        "positive": "Your straight drive stayed along the ground all session.",
        "safety": None,
        "honesty_banner": None,
        "claims": [
            {"value": n, "metric": "ball_count", "recompute_key": f"finding:{finding_id}:n"}
        ],
    }


def seed_demo(app: FastAPI) -> SeedSummary:
    """Seed the demo lab through the API and the ORM, and publish through the real gate."""
    with TestClient(app) as client:
        player = _post(
            client, "/players", {"name": "Arjun", "birthdate": "2014-11-20"}, PARENT_TOKEN
        )
        player_id = player["id"]

        def new_session(day: date, notes: str) -> uuid.UUID:
            created = _post(
                client,
                "/sessions",
                {
                    "player_id": player_id,
                    "date": day.isoformat(),
                    "session_type": "batting",
                    "bowler_source": "machine",
                    "machine_settings": {"speed_kph": 90, "length": "full"},
                    "notes": notes,
                },
                PARENT_TOKEN,
            )
            return uuid.UUID(created["id"])

        analyzed_id = new_session(DEMO_DATE, "Machine at 90 kph, full outside off focus.")
        degraded_id = new_session(DRAFT_DATE, "Short session; side camera was unplugged.")

        with _db(app) as db:
            analyzed = db.get(SessionRow, analyzed_id)
            degraded = db.get(SessionRow, degraded_id)
            assert analyzed is not None and degraded is not None
            analyzed.state = SessionState.ANALYZED
            degraded.state = SessionState.CAPTURED
            degraded.degraded = True
            degraded.missing_views = ["C2"]

            fault_balls = set(range(2, 2 * FAULT_BALLS + 1, 2))
            clips = _seed_balls(
                db, analyzed_id, ANALYZED_BALLS, CAMERAS, lambda ball: ball in fault_balls
            )
            _seed_balls(db, degraded_id, DEGRADED_BALLS, ("C1",), lambda _ball: False)

            finding = Finding(
                session_id=analyzed_id,
                agent="rules",
                rule_key="front_foot_stride",
                kind="technique",
                severity="major",
                metric="control_pct",
                condition={"line": "outside_off", "length": "full"},
                n=FAULT_BALLS,
                effect_size=None,
                confidence=0.9,
                ball_ids=sorted(fault_balls),
                evidence={},
                payload={},
            )
            db.add(finding)
            db.flush()
            finding_id = str(finding.id)
            evidence = {str(ball): clips[ball] for ball in sorted(fault_balls)}

            published = Report(
                player_id=uuid.UUID(player_id),
                session_id=analyzed_id,
                kind=ReportKind.DAILY,
                period_start=DEMO_DATE,
                period_end=DEMO_DATE,
                body=_report_body("daily", DEMO_DATE, finding_id, evidence, FAULT_BALLS),
            )
            draft = Report(
                player_id=uuid.UUID(player_id),
                session_id=analyzed_id,
                kind=ReportKind.DAILY,
                period_start=DRAFT_DATE,
                period_end=DRAFT_DATE,
                body=_report_body("daily", DRAFT_DATE, finding_id, evidence, FAULT_BALLS),
                review_due_at=datetime.now(UTC) + timedelta(hours=20),
            )
            db.add_all([published, draft])
            db.flush()
            published_id, draft_id = str(published.id), str(draft.id)

        decision = client.post(f"/reports/{published_id}/publish", headers=_auth(COACH_TOKEN))
        if decision.status_code != 200 or decision.json().get("status") != "published":
            raise RuntimeError(f"demo report failed the publish gate: {decision.text}")

    return SeedSummary(
        player_id=player_id,
        analyzed_session_id=str(analyzed_id),
        degraded_session_id=str(degraded_id),
        published_report_id=published_id,
        draft_report_id=draft_id,
    )


RESET_PATH = "/__dev/reset"


def mount_dev_reset(app: FastAPI) -> None:
    """Add POST /__dev/reset: wipe the in-memory database and reseed the demo.

    DEVELOPMENT ONLY. It exists so end-to-end journeys that change data (a coach
    publishing a report) start from the same demo every time. It is mounted by
    this script alone, never by ``create_app``, so the product API and its
    OpenAPI schema never contain it. Parent token required.
    """
    router = APIRouter()

    @router.post(RESET_PATH, include_in_schema=False)
    def reset(authorization: str = Header(default="")) -> dict[str, str]:
        if authorization != f"Bearer {PARENT_TOKEN}":
            raise HTTPException(status_code=401, detail="dev reset needs the dev parent token")
        engine: Engine = app.state.engine
        Base.metadata.drop_all(engine)
        create_all(engine)
        return asdict(seed_demo(app))

    app.include_router(router)


def web_env(api_base: str, base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for `next dev`: both the proxy (F2) and legacy (pre-F2b) conventions."""
    env = dict(os.environ if base is None else base)
    env["CRICAI_API_BASE_URL"] = api_base
    env["NEXT_PUBLIC_API_BASE_URL"] = api_base
    env.setdefault("NEXT_TELEMETRY_DISABLED", "1")
    return env


def web_commands(pnpm: str, mode: str, host: str, port: int) -> list[list[str]]:
    """The commands that serve the dashboard: `next dev`, or a production build + `next start`.

    Production mode is what the lab tablet runs and what end-to-end journeys
    use: pages are compiled once up front instead of on first visit, which on
    a busy machine can take over a minute per route under `next dev`.
    """
    serve = ["--port", str(port), "--hostname", host]
    if mode == "dev":
        return [[pnpm, "dev", *serve]]
    if mode == "prod":
        return [[pnpm, "build"], [pnpm, "start", *serve]]
    raise ValueError(f"unknown web mode {mode!r} (expected 'dev' or 'prod')")


def banner(api_base: str, web_base: str | None, summary: SeedSummary | None) -> str:
    lines = ["", "cricAI dev stack", f"  API       {api_base}  (docs: {api_base}/docs)"]
    if web_base:
        lines.append(f"  Dashboard {web_base}")
    lines += [
        "  Sign in with a role token:",
        f"    parent  {PARENT_TOKEN}",
        f"    coach   {COACH_TOKEN}",
        f"    player  {PLAYER_TOKEN}",
    ]
    if summary is None:
        lines.append("  Database is EMPTY (--no-seed).")
    else:
        lines += [
            f"  Player    {summary.player_id}",
            f"  Sessions  {summary.analyzed_session_id} (analyzed)",
            f"            {summary.degraded_session_id} (degraded, C2 missing)",
            f"  Reports   {summary.published_report_id} (published)",
            f"            {summary.draft_report_id} (draft, in the coach review queue)",
        ]
    lines.append(f"  Reseed    POST {api_base}{RESET_PATH} (parent token)")
    lines.append("  Ctrl+C stops everything.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - process orchestration
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--web-port", type=int, default=3000)
    parser.add_argument("--api-only", action="store_true")
    parser.add_argument("--no-seed", action="store_true")
    parser.add_argument(
        "--web-mode",
        choices=("dev", "prod"),
        default="dev",
        help="dev: next dev (hot reload); prod: next build then next start",
    )
    args = parser.parse_args(argv)

    storage = Path(tempfile.mkdtemp(prefix="cricai-dev-"))
    app = make_app(storage, make_engine())
    mount_dev_reset(app)
    summary = None if args.no_seed else seed_demo(app)
    api_base = f"http://{args.host}:{args.api_port}"

    server = uvicorn.Server(
        uvicorn.Config(app, host=args.host, port=args.api_port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    web: subprocess.Popen[bytes] | None = None
    web_base = None
    if not args.api_only:
        pnpm = shutil.which("pnpm")
        if pnpm is None:
            print("pnpm not found on PATH (source tools/env.sh)", file=sys.stderr)
            server.should_exit = True
            return 2
        web_base = f"http://{args.host}:{args.web_port}"
        *prepare, serve = web_commands(pnpm, args.web_mode, args.host, args.web_port)
        for command in prepare:
            if subprocess.run(command, cwd=WEB_DIR, env=web_env(api_base), check=False).returncode:
                print(f"{' '.join(command[1:])} failed", file=sys.stderr)
                server.should_exit = True
                return 1
        web = subprocess.Popen(serve, cwd=WEB_DIR, env=web_env(api_base))
    print(banner(api_base, web_base, summary), flush=True)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        while not stop.wait(0.5):
            if web is not None and web.poll() is not None:
                print(f"web dev server exited ({web.returncode})", file=sys.stderr)
                break
    finally:
        if web is not None and web.poll() is None:
            web.terminate()
            web.wait(timeout=20)
        server.should_exit = True
        thread.join(timeout=10)
        shutil.rmtree(storage, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
