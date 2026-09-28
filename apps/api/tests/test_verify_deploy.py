"""Deployment smoke unit tests (Phase-7 d1; T6 parent UAT; US-L3/B1/A3/G3/K1).

``scripts/verify_deploy.py`` is black-box by contract, so the API legs run
against the REAL in-process test app (``fastapi.testclient.TestClient`` IS an
``httpx.Client``) and the web leg is an ``httpx.MockTransport``. Broken-
deployment shapes (auth off, wrong lifecycle states, malformed payloads,
refused connections) are mocked per leg — each must FAIL its own check and
never crash the smoke.

``scripts`` is not an importable package, so the module is loaded by path
(the repo's established pattern, see packages/data/tests/test_rules_io.py).
"""

import importlib.util
import json
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from cricai_testing.apptest import PARENT_TOKEN, auth, make_test_app

_REPO_ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "verify_deploy", _REPO_ROOT / "scripts" / "verify_deploy.py"
)
assert _spec is not None and _spec.loader is not None
verify_deploy = importlib.util.module_from_spec(_spec)
sys.modules["verify_deploy"] = verify_deploy
_spec.loader.exec_module(verify_deploy)

ALL_LEGS = (
    "api-health",
    "auth-enforced",
    "auth-parent-token",
    "camera-registry",
    "session-lifecycle",
    "report-list",
    "web-root",
)


def _web_ok() -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, text="<html/>")),
        base_url="http://web",
    )


def _web_with_status(status_code: int) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(status_code)),
        base_url="http://web",
    )


Handler = Callable[[httpx.Request], httpx.Response]


def _mock_api(overrides: dict[tuple[str, str], httpx.Response] | None = None) -> httpx.Client:
    """A scripted 'deployment' for broken-shape legs: sane defaults per route,
    overridden per test. GET /players honors the bearer header (auth on)."""
    routes: dict[tuple[str, str], httpx.Response] = {
        ("GET", "/health"): httpx.Response(200, json={"status": "ok", "version": "mock"}),
        ("GET", "/cameras"): httpx.Response(200, json=[{"camera_id": "C1"}]),
        ("POST", "/players"): httpx.Response(201, json={"id": "p1"}),
        ("POST", "/sessions"): httpx.Response(201, json={"id": "s1"}),
        ("POST", "/sessions/s1/start"): httpx.Response(200, json={"state": "recording"}),
        ("POST", "/sessions/s1/stop"): httpx.Response(200, json={"state": "captured"}),
        ("GET", "/reports"): httpx.Response(200, json=[]),
    }
    routes.update(overrides or {})

    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if key == ("GET", "/players") and key not in routes:
            if request.headers.get("Authorization") == f"Bearer {PARENT_TOKEN}":
                return httpx.Response(200, json=[])
            return httpx.Response(401, json={"detail": "missing bearer token"})
        if key in routes:
            return routes[key]
        raise AssertionError(f"mock deployment got an unexpected request: {key}")

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://api")


def _by_name(results: list[Any]) -> dict[str, Any]:
    return {result.name: result for result in results}


@pytest.fixture
def api(tmp_path: Path) -> TestClient:
    return TestClient(make_test_app(tmp_path))


def _register_camera(api: TestClient, camera_id: str = "C2") -> None:
    response = api.post(
        "/cameras",
        json={
            "camera_id": camera_id,
            "position_label": f"{camera_id} rigged",
            "xyz_offset_m": {"x": 0.0, "y": 3.0, "z": 1.2},
            "height_m": 1.2,
            "fps": 120,
            "resolution": "1920x1080",
        },
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 201


class TestRunSmokeAgainstRealApp:
    def test_fresh_deploy_all_legs_pass_and_smoke_registers_c1(self, api: TestClient) -> None:
        results = verify_deploy.run_smoke(api, _web_ok(), PARENT_TOKEN)
        by_name = _by_name(results)
        assert [result.name for result in results] == list(ALL_LEGS)
        assert all(result.ok for result in results), [
            (result.name, result.detail) for result in results if not result.ok
        ]
        assert "registry was empty" in by_name["camera-registry"].detail
        assert "created -> recording -> captured" in by_name["session-lifecycle"].detail
        assert "0 report(s)" in by_name["report-list"].detail
        # The smoke's footprint is a GUEST player (US-L3: short-lived by policy).
        players = api.get("/players", headers=auth(PARENT_TOKEN)).json()
        assert [player["is_guest"] for player in players] == [True]

    def test_rigged_deploy_reads_registry_and_never_writes_a_camera(self, api: TestClient) -> None:
        _register_camera(api, "C2")
        results = verify_deploy.run_smoke(api, _web_ok(), PARENT_TOKEN)
        by_name = _by_name(results)
        assert all(result.ok for result in results)
        assert "using registered camera C2" in by_name["camera-registry"].detail
        cameras = api.get("/cameras", headers=auth(PARENT_TOKEN)).json()
        assert [camera["camera_id"] for camera in cameras] == ["C2"]

    def test_bad_parent_token_fails_auth_leg_and_skips_dependents(self, api: TestClient) -> None:
        results = verify_deploy.run_smoke(api, _web_ok(), "not-the-parent-token")
        by_name = _by_name(results)
        assert by_name["api-health"].ok and by_name["auth-enforced"].ok
        assert by_name["web-root"].ok
        assert not by_name["auth-parent-token"].ok
        for name in ("camera-registry", "session-lifecycle", "report-list"):
            assert not by_name[name].ok
            assert "skipped: parent token rejected" in by_name[name].detail

    def test_web_root_failure_is_isolated_to_the_web_leg(self, api: TestClient) -> None:
        results = verify_deploy.run_smoke(api, _web_with_status(500), PARENT_TOKEN)
        by_name = _by_name(results)
        assert not by_name["web-root"].ok
        assert "expected HTTP 200, got 500" in by_name["web-root"].detail
        assert all(result.ok for result in results if result.name != "web-root")


class TestBrokenDeploymentShapes:
    def test_health_status_not_ok_fails_the_health_leg(self) -> None:
        api = _mock_api({("GET", "/health"): httpx.Response(200, json={"status": "degraded"})})
        by_name = _by_name(verify_deploy.run_smoke(api, _web_ok(), PARENT_TOKEN))
        assert not by_name["api-health"].ok
        assert "'degraded'" in by_name["api-health"].detail

    def test_auth_off_deployment_is_detected(self) -> None:
        # A tokenless /players answered 200 means auth is OFF — release-blocking.
        api = _mock_api({("GET", "/players"): httpx.Response(200, json=[])})
        by_name = _by_name(verify_deploy.run_smoke(api, _web_ok(), PARENT_TOKEN))
        assert not by_name["auth-enforced"].ok
        assert "expected HTTP 401, got 200" in by_name["auth-enforced"].detail
        assert by_name["session-lifecycle"].ok  # other legs still reported

    def test_camera_registry_error_skips_lifecycle_and_reports(self) -> None:
        api = _mock_api({("GET", "/cameras"): httpx.Response(500, text="boom")})
        by_name = _by_name(verify_deploy.run_smoke(api, _web_ok(), PARENT_TOKEN))
        assert not by_name["camera-registry"].ok
        assert "skipped: no active camera available" in by_name["session-lifecycle"].detail
        assert "skipped: session lifecycle did not complete" in by_name["report-list"].detail

    def test_start_state_other_than_recording_fails_lifecycle(self) -> None:
        api = _mock_api(
            {("POST", "/sessions/s1/start"): httpx.Response(200, json={"state": "created"})}
        )
        by_name = _by_name(verify_deploy.run_smoke(api, _web_ok(), PARENT_TOKEN))
        assert not by_name["session-lifecycle"].ok
        assert "not 'recording'" in by_name["session-lifecycle"].detail

    def test_stop_state_other_than_captured_fails_lifecycle(self) -> None:
        api = _mock_api(
            {("POST", "/sessions/s1/stop"): httpx.Response(200, json={"state": "recording"})}
        )
        by_name = _by_name(verify_deploy.run_smoke(api, _web_ok(), PARENT_TOKEN))
        assert not by_name["session-lifecycle"].ok
        assert "not 'captured'" in by_name["session-lifecycle"].detail

    def test_report_list_that_is_not_a_list_fails_the_leg(self) -> None:
        api = _mock_api({("GET", "/reports"): httpx.Response(200, json={"items": []})})
        by_name = _by_name(verify_deploy.run_smoke(api, _web_ok(), PARENT_TOKEN))
        assert not by_name["report-list"].ok
        assert "expected a list" in by_name["report-list"].detail

    def test_malformed_payload_fails_its_leg_without_crashing_the_smoke(self) -> None:
        # /cameras answering a mapping breaks the leg's row iteration: the
        # smoke must record the failure and keep going, not traceback.
        api = _mock_api({("GET", "/cameras"): httpx.Response(200, json={"cameras": []})})
        results = verify_deploy.run_smoke(api, _web_ok(), PARENT_TOKEN)
        by_name = _by_name(results)
        assert not by_name["camera-registry"].ok
        assert [result.name for result in results] == list(ALL_LEGS)

    def test_refused_connection_is_reported_per_leg(self) -> None:
        def refuse(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        api = httpx.Client(transport=httpx.MockTransport(refuse), base_url="http://api")
        results = verify_deploy.run_smoke(api, _web_ok(), PARENT_TOKEN)
        by_name = _by_name(results)
        assert not by_name["api-health"].ok
        assert "ConnectError" in by_name["api-health"].detail
        assert by_name["web-root"].ok


class TestMain:
    def _clients(
        self, api: httpx.Client, web: httpx.Client
    ) -> Callable[[str, str], tuple[httpx.Client, httpx.Client]]:
        def make_clients(_api_base: str, _web_base: str) -> tuple[httpx.Client, httpx.Client]:
            return api, web

        return make_clients

    def test_exit_zero_when_every_check_passes(
        self, api: TestClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = verify_deploy.main(
            ["--parent-token", PARENT_TOKEN], make_clients=self._clients(api, _web_ok())
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "PASS  api-health" in out
        assert f"{len(ALL_LEGS)}/{len(ALL_LEGS)} checks passed" in out

    def test_exit_one_when_any_check_fails(
        self, api: TestClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = verify_deploy.main(
            ["--parent-token", PARENT_TOKEN],
            make_clients=self._clients(api, _web_with_status(503)),
        )
        out = capsys.readouterr().out
        assert code == 1
        assert "FAIL  web-root" in out

    def test_parent_token_falls_back_to_environment(
        self, api: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CRICAI_PARENT_TOKEN", PARENT_TOKEN)
        code = verify_deploy.main([], make_clients=self._clients(api, _web_ok()))
        assert code == 0

    def test_exit_two_without_any_parent_token(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("CRICAI_PARENT_TOKEN", raising=False)
        code = verify_deploy.main([], make_clients=self._clients(_mock_api(), _web_ok()))
        assert code == 2
        assert "no parent token" in capsys.readouterr().out

    def test_clients_are_closed_even_when_a_leg_raised(self, api: TestClient) -> None:
        web = _web_ok()
        verify_deploy.main(["--parent-token", PARENT_TOKEN], make_clients=self._clients(api, web))
        assert web.is_closed


class TestTokenSources:
    """Finding 27: the admin token on argv leaks to `ps` and shell history —
    a token FILE and the env var are the supported quiet paths; the argv flag
    stays for compatibility but must warn loudly."""

    def _clients(
        self, api: httpx.Client, web: httpx.Client
    ) -> Callable[[str, str], tuple[httpx.Client, httpx.Client]]:
        def make_clients(_api_base: str, _web_base: str) -> tuple[httpx.Client, httpx.Client]:
            return api, web

        return make_clients

    def test_parent_token_file_authenticates(self, api: TestClient, tmp_path: Path) -> None:
        token_file = tmp_path / "parent.token"
        token_file.write_text(f"{PARENT_TOKEN}\n")  # trailing newline must be stripped
        code = verify_deploy.main(
            ["--parent-token-file", str(token_file)],
            make_clients=self._clients(api, _web_ok()),
        )
        assert code == 0

    def test_missing_token_file_is_fatal_exit_two(
        self, api: TestClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = verify_deploy.main(
            ["--parent-token-file", str(tmp_path / "absent.token")],
            make_clients=self._clients(api, _web_ok()),
        )
        assert code == 2
        assert "token file" in capsys.readouterr().out

    def test_argv_token_warns_about_process_list_exposure(
        self, api: TestClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = verify_deploy.main(
            ["--parent-token", PARENT_TOKEN], make_clients=self._clients(api, _web_ok())
        )
        assert code == 0
        err = capsys.readouterr().err
        assert "process list" in err and "CRICAI_PARENT_TOKEN" in err

    def test_env_and_file_paths_do_not_warn(
        self,
        api: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        token_file = tmp_path / "parent.token"
        token_file.write_text(PARENT_TOKEN)
        assert (
            verify_deploy.main(
                ["--parent-token-file", str(token_file)],
                make_clients=self._clients(api, _web_ok()),
            )
            == 0
        )
        monkeypatch.setenv("CRICAI_PARENT_TOKEN", PARENT_TOKEN)
        # main() closes the clients it is handed (one CLI run == fresh sockets),
        # so the env-var leg gets its own app rather than the now-closed `api`.
        env_api = TestClient(make_test_app(tmp_path / "env-app"))
        assert verify_deploy.main([], make_clients=self._clients(env_api, _web_ok())) == 0
        assert capsys.readouterr().err == ""


class TestWorkerLeg:
    """Finding 7: '7/7 checks passed' used to green-light a stack whose rq
    worker was dead. The optional --check-worker leg probes redis + the rq
    worker registration; without the flag the smoke's legs are unchanged."""

    def _clients(
        self, api: httpx.Client, web: httpx.Client
    ) -> Callable[[str, str], tuple[httpx.Client, httpx.Client]]:
        def make_clients(_api_base: str, _web_base: str) -> tuple[httpx.Client, httpx.Client]:
            return api, web

        return make_clients

    def test_check_worker_reports_the_worker_count(self) -> None:
        detail = verify_deploy.check_worker(
            "redis://lab:6379/0", "cricai", count_workers=lambda _url, _queue: 2
        )
        assert "2 rq worker(s)" in detail and "'cricai'" in detail

    def test_check_worker_fails_when_no_worker_listens(self) -> None:
        with pytest.raises(verify_deploy.SmokeFailure, match="NO rq worker"):
            verify_deploy.check_worker(
                "redis://lab:6379/0", "cricai", count_workers=lambda _url, _queue: 0
            )

    def test_real_worker_count_pings_redis_before_counting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default prober must fail on an unreachable redis (the ping),
        never report 0 workers against a dead server."""
        calls: list[str] = []

        class FakeConnection:
            def ping(self) -> None:
                calls.append("ping")

        class FakeRedis:
            @staticmethod
            def from_url(url: str) -> FakeConnection:
                calls.append(f"from_url:{url}")
                return FakeConnection()

        class FakeQueue:
            # `connection` mirrors rq.Queue's real kwarg (the prober passes it
            # by keyword) even though the fake only records the queue name.
            def __init__(self, name: str, connection: object) -> None:  # noqa: ARG002
                calls.append(f"queue:{name}")

        class FakeWorker:
            @staticmethod
            def count(queue: object) -> int:  # noqa: ARG004
                calls.append("count")
                return 1

        monkeypatch.setitem(sys.modules, "redis", types.SimpleNamespace(Redis=FakeRedis))
        monkeypatch.setitem(
            sys.modules, "rq", types.SimpleNamespace(Queue=FakeQueue, Worker=FakeWorker)
        )
        assert verify_deploy._count_rq_workers("redis://lab:6379/0", "cricai") == 1
        assert calls == ["from_url:redis://lab:6379/0", "ping", "queue:cricai", "count"]

    def test_smoke_has_no_worker_leg_without_the_flag(
        self, api: TestClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = verify_deploy.main(
            ["--parent-token", PARENT_TOKEN],
            make_clients=self._clients(api, _web_ok()),
            worker_probe=lambda _url, _queue: pytest.fail("probe must not run"),
        )
        out = capsys.readouterr().out
        assert code == 0
        assert "worker-queue" not in out
        assert f"{len(ALL_LEGS)}/{len(ALL_LEGS)} checks passed" in out

    def test_check_worker_flag_appends_a_passing_leg(
        self, api: TestClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        seen: list[tuple[str, str]] = []

        def probe(redis_url: str, queue: str) -> str:
            seen.append((redis_url, queue))
            return "redis up; 1 rq worker(s) on queue 'cricai'"

        code = verify_deploy.main(
            [
                "--parent-token",
                PARENT_TOKEN,
                "--check-worker",
                "--redis-url",
                "redis://lab:6379/0",
                "--rq-queue",
                "cricai",
            ],
            make_clients=self._clients(api, _web_ok()),
            worker_probe=probe,
        )
        out = capsys.readouterr().out
        assert code == 0
        assert seen == [("redis://lab:6379/0", "cricai")]
        assert "PASS  worker-queue" in out
        assert f"{len(ALL_LEGS) + 1}/{len(ALL_LEGS) + 1} checks passed" in out

    def test_dead_worker_fails_the_leg_and_the_exit_code(
        self, api: TestClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def probe(_redis_url: str, _queue: str) -> str:
            raise verify_deploy.SmokeFailure("redis at redis://lab:6379/0 is up but NO rq worker")

        code = verify_deploy.main(
            ["--parent-token", PARENT_TOKEN, "--check-worker"],
            make_clients=self._clients(api, _web_ok()),
            worker_probe=probe,
        )
        out = capsys.readouterr().out
        assert code == 1
        assert "FAIL  worker-queue" in out and "NO rq worker" in out

    def test_unreachable_redis_fails_the_leg_without_crashing_the_smoke(
        self, api: TestClient, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A dead redis makes the real prober raise ConnectionError (not
        SmokeFailure) — the very scenario this leg exists for. It must FAIL the
        worker leg with the full report and exit 1, never traceback out of main."""

        def probe(_redis_url: str, _queue: str) -> str:
            raise ConnectionError("Error 61 connecting to localhost:6379. Connection refused.")

        code = verify_deploy.main(
            ["--parent-token", PARENT_TOKEN, "--check-worker"],
            make_clients=self._clients(api, _web_ok()),
            worker_probe=probe,
        )
        out = capsys.readouterr().out
        assert code == 1
        assert "PASS  api-health" in out  # the HTTP legs still print their report
        assert "FAIL  worker-queue" in out and "ConnectionError" in out


def test_smoke_camera_payload_matches_the_registry_contract() -> None:
    """The C1 fallback payload must stay valid for POST /cameras (US-A1/A2)."""
    payload = json.loads(json.dumps(verify_deploy.SMOKE_CAMERA))
    assert payload["camera_id"] == "C1"
    assert payload["fps"] == 120  # US-A2 analysis-grade floor: no start warning
