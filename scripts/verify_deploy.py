#!/usr/bin/env python3
"""Black-box LAN deployment smoke (Phase-7 plan; T6 parent UAT; US-L3).

Verifies a deployed cricAI stack from the OUTSIDE, exactly the legs the
Phase-7 plan names: API ``/health``, auth enforcement, an authenticated
session create -> start -> stop lifecycle (US-B1/US-A3), the report list
(US-G3), and the web dashboard root (US-K1). Exit code 0 means every check
passed; 1 means at least one failed; 2 means the smoke could not even start
(no parent token).

Usage (after ``scripts/deploy_local.sh start`` or ``docker compose up``)::

    uv run scripts/verify_deploy.py \
        --api-base http://lab.local:8000 --web-base http://lab.local:3000

The parent token comes from ``CRICAI_PARENT_TOKEN`` (the quiet default) or
``--parent-token-file PATH``; ``--parent-token VALUE`` also works but warns,
because it exposes the admin token to the process list and shell history.

The HTTP legs go green even when the rq worker process is dead (the API takes
sessions; nothing drains the queue). ``--check-worker`` adds a worker-queue
leg that pings redis and asserts a worker is registered — opt-in because it,
alone among the legs, needs redis + rq importable::

    uv run scripts/verify_deploy.py --check-worker \
        --redis-url redis://lab.local:6379/0 --rq-queue cricai

Footprint: the smoke creates one GUEST player (guests never accrue baselines,
reports or milestones) and one throwdown session left in CAPTURED. If the
camera registry is EMPTY (fresh deploy), it registers C1 with lab defaults —
era semantics make a later real C1 registration supersede it cleanly; on a
rigged box it only reads the registry.

Importable and unit-testable by design (Phase-7 d1 contract): ``run_smoke``
takes two ``httpx.Client``-compatible clients, so tests drive the API legs
against the in-process test app (``fastapi.testclient.TestClient`` IS an
``httpx.Client``) and mock the web leg — apps/api/tests/test_verify_deploy.py.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

#: Guest smoke identity (US-L3: guest data is parent/coach-only, short-lived).
SMOKE_PLAYER = {"name": "deploy-smoke", "birthdate": "2014-01-01", "is_guest": True}

#: Registered only when the camera registry is empty (fresh deploy): C1 at the
#: US-A2 analysis-grade floor so session start raises no fps warning.
SMOKE_CAMERA = {
    "camera_id": "C1",
    "position_label": "deploy-smoke default (behind bowler)",
    "xyz_offset_m": {"x": 0.0, "y": 3.0, "z": 1.2},
    "height_m": 1.2,
    "fps": 120,
    "resolution": "1920x1080",
}


class SmokeFailure(Exception):
    """One smoke leg failed its expectation (reported, never raised to main)."""


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one smoke leg."""

    name: str
    ok: bool
    detail: str


def _expect_status(response: httpx.Response, expected: int, leg: str) -> httpx.Response:
    if response.status_code != expected:
        raise SmokeFailure(
            f"{leg}: expected HTTP {expected}, got {response.status_code}: {response.text[:200]}"
        )
    return response


def check_health(api: httpx.Client) -> str:
    """API ``/health`` answers 200 with ``status: ok`` (unauthenticated leg)."""
    body = _expect_status(api.get("/health"), 200, "GET /health").json()
    if body.get("status") != "ok":
        raise SmokeFailure(f"GET /health: status is {body.get('status')!r}, not 'ok'")
    return f"api version {body.get('version')}"


def check_auth_enforced(api: httpx.Client) -> str:
    """A tokenless request must be rejected — proves role tokens are configured
    and the API has no auth-off mode (US-L3)."""
    _expect_status(api.get("/players"), 401, "GET /players (no token)")
    return "unauthenticated request rejected (401)"


def check_parent_token(api: httpx.Client, headers: dict[str, str]) -> str:
    """The configured parent token authenticates (admin role, US-L3)."""
    _expect_status(api.get("/players", headers=headers), 200, "GET /players (parent token)")
    return "parent token accepted"


def ensure_camera(api: httpx.Client, headers: dict[str, str], state: dict[str, str]) -> str:
    """An active camera must exist for session start (US-A1 registry); on a
    fresh deploy, register the C1 default era."""
    rows = _expect_status(api.get("/cameras", headers=headers), 200, "GET /cameras").json()
    active = [row["camera_id"] for row in rows]
    if active:
        state["camera_id"] = active[0]
        return f"using registered camera {active[0]} ({len(active)} active)"
    created = _expect_status(
        api.post("/cameras", json=SMOKE_CAMERA, headers=headers), 201, "POST /cameras"
    ).json()
    state["camera_id"] = created["camera_id"]
    return "registry was empty: registered smoke camera C1 (120 fps)"


def session_lifecycle(api: httpx.Client, headers: dict[str, str], state: dict[str, str]) -> str:
    """US-B1 create + US-A3 start/stop as one unit, via a guest player and a
    coach-throwdown batting session (no machine checklist gate)."""
    player = _expect_status(
        api.post("/players", json=SMOKE_PLAYER, headers=headers), 201, "POST /players"
    ).json()
    session_payload = {
        "player_id": player["id"],
        "date": datetime.now(tz=UTC).date().isoformat(),
        "session_type": "batting",
        "bowler_source": "coach",
        "notes": "deploy-smoke (verify_deploy.py)",
    }
    session = _expect_status(
        api.post("/sessions", json=session_payload, headers=headers), 201, "POST /sessions"
    ).json()
    started = _expect_status(
        api.post(
            f"/sessions/{session['id']}/start",
            json={"cameras": [state["camera_id"]]},
            headers=headers,
        ),
        200,
        "POST /sessions/{id}/start",
    ).json()
    if started["state"] != "recording":
        raise SmokeFailure(f"start: state is {started['state']!r}, not 'recording'")
    stopped = _expect_status(
        api.post(f"/sessions/{session['id']}/stop", json={}, headers=headers),
        200,
        "POST /sessions/{id}/stop",
    ).json()
    if stopped["state"] != "captured":
        raise SmokeFailure(f"stop: state is {stopped['state']!r}, not 'captured'")
    state["player_id"] = player["id"]
    return f"session {session['id']}: created -> recording -> captured"


def check_report_list(api: httpx.Client, headers: dict[str, str], state: dict[str, str]) -> str:
    """US-G3 report surface answers for the smoke player (empty list is fine —
    no pipeline ran; the leg proves the read path, not report content)."""
    body = _expect_status(
        api.get("/reports", params={"player_id": state["player_id"]}, headers=headers),
        200,
        "GET /reports",
    ).json()
    if not isinstance(body, list):
        raise SmokeFailure(f"GET /reports: expected a list, got {type(body).__name__}")
    return f"{len(body)} report(s) listed"


def check_web_root(web: httpx.Client) -> str:
    """US-K1 dashboard root reachable."""
    _expect_status(web.get("/"), 200, "GET / (web)")
    return "dashboard root reachable"


def _count_rq_workers(redis_url: str, queue: str) -> int:
    """Number of rq workers registered on ``queue``, after pinging redis.

    redis/rq are imported lazily so the smoke's HTTP legs never require them,
    and the ping runs FIRST so an unreachable redis fails loudly here rather
    than reporting a misleading zero-worker count against a dead server.
    """
    import redis  # noqa: PLC0415 — lazy so the HTTP-only smoke never needs redis/rq
    import rq  # noqa: PLC0415

    connection = redis.Redis.from_url(redis_url)
    connection.ping()
    listened = rq.Queue(queue, connection=connection)
    return rq.Worker.count(queue=listened)


WorkerProbe = Callable[[str, str], str]


def check_worker(
    redis_url: str,
    queue: str,
    count_workers: Callable[[str, str], int] = _count_rq_workers,
) -> str:
    """Redis is reachable AND at least one rq worker listens on ``queue``.

    The HTTP legs go green on a stack whose worker process is dead — the API
    accepts sessions while nothing drains the queue — so a passing smoke can
    still hide a broken deployment (finding 7). This leg probes the queue side
    directly. It is OPT-IN (``--check-worker``): the smoke is otherwise pure
    black-box HTTP and must not force redis/rq to be importable.
    """
    count = count_workers(redis_url, queue)
    if count < 1:
        raise SmokeFailure(
            f"redis at {redis_url} is up but NO rq worker is registered on queue {queue!r}"
        )
    return f"redis up; {count} rq worker(s) on queue {queue!r}"


def run_smoke(api: httpx.Client, web: httpx.Client, parent_token: str) -> list[CheckResult]:
    """Run every smoke leg; dependent legs are skipped (as failures) when their
    prerequisite failed, so one broken leg never crashes the whole report."""
    headers = {"Authorization": f"Bearer {parent_token}"}
    state: dict[str, str] = {}
    results: list[CheckResult] = []

    def attempt(name: str, leg: Callable[[], str]) -> bool:
        try:
            detail = leg()
        except SmokeFailure as failure:
            results.append(CheckResult(name, False, str(failure)))
            return False
        except Exception as error:  # a broken deployment (bad JSON, refused
            # connection, wrong shape) must FAIL the leg, never crash the smoke
            results.append(CheckResult(name, False, f"{type(error).__name__}: {error}"))
            return False
        results.append(CheckResult(name, True, detail))
        return True

    def skip(name: str, reason: str) -> None:
        results.append(CheckResult(name, False, f"skipped: {reason}"))

    attempt("api-health", lambda: check_health(api))
    attempt("auth-enforced", lambda: check_auth_enforced(api))
    if attempt("auth-parent-token", lambda: check_parent_token(api, headers)):
        attempt("camera-registry", lambda: ensure_camera(api, headers, state))
        if "camera_id" in state:
            attempt("session-lifecycle", lambda: session_lifecycle(api, headers, state))
        else:
            skip("session-lifecycle", "no active camera available")
        if "player_id" in state:
            attempt("report-list", lambda: check_report_list(api, headers, state))
        else:
            skip("report-list", "session lifecycle did not complete")
    else:
        for name in ("camera-registry", "session-lifecycle", "report-list"):
            skip(name, "parent token rejected")
    attempt("web-root", lambda: check_web_root(web))
    return results


ClientFactory = Callable[[str, str], tuple[httpx.Client, httpx.Client]]


def _default_clients(api_base: str, web_base: str) -> tuple[httpx.Client, httpx.Client]:
    return (
        httpx.Client(base_url=api_base, timeout=10.0),
        httpx.Client(base_url=web_base, timeout=10.0, follow_redirects=True),
    )


def _resolve_parent_token(args: argparse.Namespace) -> str | None:
    """Parent token from (in order) a token FILE, the argv flag, or the env.

    The argv flag stays for compatibility but leaks the admin token to the
    process list and shell history, so it warns loudly (finding 27); the file
    and env paths are quiet. Returns ``None`` only for a MISSING token file (a
    fatal misconfiguration whose message is already printed) so the caller can
    exit 2 without a second, generic error line.
    """
    if args.parent_token_file is not None:
        path = Path(args.parent_token_file)
        if not path.is_file():
            print(f"FATAL: token file {path} does not exist")
            return None
        return path.read_text().strip()
    if args.parent_token is not None:
        print(
            "WARNING: --parent-token puts the admin token on the process list (ps) and "
            "in shell history; prefer CRICAI_PARENT_TOKEN or --parent-token-file",
            file=sys.stderr,
        )
        argv_token: str = args.parent_token
        return argv_token
    return os.environ.get("CRICAI_PARENT_TOKEN", "")


def main(
    argv: list[str] | None = None,
    make_clients: ClientFactory = _default_clients,
    worker_probe: WorkerProbe = check_worker,
) -> int:
    parser = argparse.ArgumentParser(
        description="Black-box LAN deployment smoke (Phase-7 plan; T6 parent UAT; US-L3)."
    )
    parser.add_argument("--api-base", default="http://localhost:8000")
    parser.add_argument("--web-base", default="http://localhost:3000")
    parser.add_argument(
        "--parent-token",
        default=None,
        help="admin token ON THE PROCESS LIST — prefer CRICAI_PARENT_TOKEN or --parent-token-file",
    )
    parser.add_argument(
        "--parent-token-file",
        default=None,
        help="read the parent token from a file (quiet path — not in ps/shell history)",
    )
    parser.add_argument(
        "--check-worker",
        action="store_true",
        help="also verify redis is reachable and an rq worker is registered (needs redis+rq)",
    )
    parser.add_argument("--redis-url", default="redis://localhost:6379/0")
    parser.add_argument("--rq-queue", default="cricai")
    args = parser.parse_args(argv)

    token = _resolve_parent_token(args)
    if token is None:
        return 2
    if not token:
        print(
            "FATAL: no parent token "
            "(set CRICAI_PARENT_TOKEN, or pass --parent-token-file / --parent-token)"
        )
        return 2

    api, web = make_clients(args.api_base, args.web_base)
    try:
        results = run_smoke(api, web, token)
    finally:
        api.close()
        web.close()

    if args.check_worker:
        try:
            detail = worker_probe(args.redis_url, args.rq_queue)
        except SmokeFailure as failure:
            results.append(CheckResult("worker-queue", False, str(failure)))
        except Exception as error:  # an unreachable redis (ConnectionError) or a
            # missing redis/rq (ImportError) is the exact dead-queue scenario this
            # leg exists for — it must FAIL the leg alongside the rest of the
            # report, never crash the smoke (mirrors run_smoke's attempt()).
            results.append(CheckResult("worker-queue", False, f"{type(error).__name__}: {error}"))
        else:
            results.append(CheckResult("worker-queue", True, detail))

    for result in results:
        print(f"{'PASS' if result.ok else 'FAIL'}  {result.name}: {result.detail}")
    failed = [result for result in results if not result.ok]
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
