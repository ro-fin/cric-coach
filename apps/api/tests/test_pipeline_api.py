"""US-J1/L1 pipeline API: trigger, resume, run trace, and the re-derive cascade.

The API process is its own in-process DAG runner, so these tests drive real
runs over the app's SQLite engine with fake stages injected through
``app.state.pipeline_registry`` (dotted paths into this module). Lock-held and
busy paths are forced by monkeypatching the runner seams the router imports.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from cricai_api.routers import pipeline as router
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
from cricai_data.models import BallTag, Finding, PipelineStage, Player
from cricai_data.models import Session as SessionRow
from cricai_worker.pipeline import LOCKED, NON_RESUMABLE, STAGES, PipelineOutcome
from fastapi.testclient import TestClient

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, PLAYER_TOKEN, auth


def ok_stage(ctx: Any, session_id: uuid.UUID) -> dict[str, Any]:
    return {"ok": True}


FAKE_REGISTRY = dict.fromkeys(STAGES, f"{__name__}:ok_stage")


def use_fakes(client: TestClient) -> None:
    client.app.state.pipeline_registry = FAKE_REGISTRY


def make_session(
    client: TestClient, *, with_findings: bool = False, with_tag: bool = False
) -> uuid.UUID:
    with client.app.state.session_factory() as db:
        player = Player(name="Arjun", birthdate=date(2014, 11, 20))
        session = SessionRow(
            player=player,
            session_date=date(2026, 7, 7),
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
        )
        db.add_all([player, session])
        db.flush()
        if with_findings:
            db.add(
                Finding(
                    session_id=session.id,
                    agent="batting",
                    kind="rule_hit",
                    severity="major",
                    metric="control_pct",
                    n=10,
                    confidence=0.9,
                )
            )
        if with_tag:
            db.add(
                BallTag(
                    session_id=session.id,
                    ball_no=1,
                    line=Line.OUTSIDE_OFF,
                    length=Length.GOOD,
                    shot=Shot.DRIVE,
                    footwork=Footwork.FRONT,
                    contact=Contact.MIDDLE,
                    outcome=Outcome.CONTROLLED_GROUND_SHOT,
                    control=True,
                    created_by="parent",
                )
            )
        db.commit()
        return session.id


# ------------------------------------------------------------------ trigger


def test_trigger_runs_the_dag_and_returns_the_trace(client: TestClient) -> None:
    use_fakes(client)
    session_id = make_session(client)
    resp = client.post(f"/pipeline/sessions/{session_id}/runs", json={}, headers=auth(PARENT_TOKEN))
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "succeeded"
    assert [s["stage"] for s in body["stages"]] == list(STAGES)
    assert body["stages"][-1]["stage"] == "report"


def test_trigger_with_default_paths_degrades_when_no_registry(client: TestClient) -> None:
    # No app.state.pipeline_registry -> real (unbuilt) DEFAULT_STAGE_PATHS -> honest FAILED run.
    session_id = make_session(client)
    resp = client.post(f"/pipeline/sessions/{session_id}/runs", json={}, headers=auth(PARENT_TOKEN))
    assert resp.status_code == 201
    assert resp.json()["status"] == "failed"


def test_trigger_unknown_session_is_404(client: TestClient) -> None:
    use_fakes(client)
    resp = client.post(
        f"/pipeline/sessions/{uuid.uuid4()}/runs", json={}, headers=auth(PARENT_TOKEN)
    )
    assert resp.status_code == 404


def test_trigger_requires_parent_role(client: TestClient) -> None:
    session_id = make_session(client)
    assert (
        client.post(
            f"/pipeline/sessions/{session_id}/runs", json={}, headers=auth(COACH_TOKEN)
        ).status_code
        == 403
    )
    assert client.post(f"/pipeline/sessions/{session_id}/runs", json={}).status_code == 401


def test_trigger_returns_409_when_session_is_locked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = make_session(client)
    locked = PipelineOutcome(session_id=session_id, run_id=None, status=LOCKED, stages=())
    monkeypatch.setattr(router, "run_pipeline", lambda *_a, **_kw: locked)
    resp = client.post(f"/pipeline/sessions/{session_id}/runs", json={}, headers=auth(PARENT_TOKEN))
    assert resp.status_code == 409
    assert "already running" in resp.json()["detail"]


# ------------------------------------------------------------------- resume


def test_resume_short_circuits_completed_stages(client: TestClient) -> None:
    use_fakes(client)
    session_id = make_session(client)
    first = client.post(
        f"/pipeline/sessions/{session_id}/runs", json={}, headers=auth(PARENT_TOKEN)
    )
    assert first.status_code == 201
    resumed = client.post(
        f"/pipeline/sessions/{session_id}/resume", json={}, headers=auth(PARENT_TOKEN)
    )
    assert resumed.status_code == 201
    body = resumed.json()
    assert body["status"] == "succeeded"
    # Media stages short-circuit by digest; agent stages read mutable DB state
    # (US-H5) so they are NON_RESUMABLE and always re-execute on a resume.
    resumed_by_stage = {s["stage"]: s["resumed"] for s in body["stages"]}
    assert all(
        resumed is (stage not in NON_RESUMABLE) for stage, resumed in resumed_by_stage.items()
    )


# --------------------------------------------------------------- run traces


def test_list_runs_returns_summaries(client: TestClient) -> None:
    use_fakes(client)
    session_id = make_session(client)
    client.post(f"/pipeline/sessions/{session_id}/runs", json={}, headers=auth(PARENT_TOKEN))
    resp = client.get(f"/pipeline/sessions/{session_id}/runs", headers=auth(COACH_TOKEN))
    assert resp.status_code == 200
    runs = resp.json()
    assert len(runs) == 1
    assert runs[0]["status"] == "succeeded"
    assert runs[0]["session_id"] == str(session_id)


def test_list_runs_unknown_session_is_404(client: TestClient) -> None:
    resp = client.get(f"/pipeline/sessions/{uuid.uuid4()}/runs", headers=auth(PARENT_TOKEN))
    assert resp.status_code == 404


def test_list_runs_forbidden_for_player(client: TestClient) -> None:
    session_id = make_session(client)
    resp = client.get(f"/pipeline/sessions/{session_id}/runs", headers=auth(PLAYER_TOKEN))
    assert resp.status_code == 403


def test_run_detail_returns_full_per_attempt_trace(client: TestClient) -> None:
    use_fakes(client)
    session_id = make_session(client)
    run_id = client.post(
        f"/pipeline/sessions/{session_id}/runs",
        json={"detector_context": {"det": "v9"}},
        headers=auth(PARENT_TOKEN),
    ).json()["run_id"]
    resp = client.get(f"/pipeline/runs/{run_id}", headers=auth(PARENT_TOKEN))
    assert resp.status_code == 200
    body = resp.json()
    assert body["detector_context"] == {"det": "v9"}
    assert [s["stage"] for s in body["stages"]] == list(STAGES)
    assert body["stages"][0]["input_digest"] is not None


def test_run_detail_sorts_unknown_stage_names_last(client: TestClient) -> None:
    use_fakes(client)
    session_id = make_session(client)
    run_id = client.post(
        f"/pipeline/sessions/{session_id}/runs", json={}, headers=auth(PARENT_TOKEN)
    ).json()["run_id"]
    with client.app.state.session_factory() as db:
        db.add(PipelineStage(run_id=uuid.UUID(run_id), stage="legacy_stage", attempt=1))
        db.commit()
    stages = client.get(f"/pipeline/runs/{run_id}", headers=auth(PARENT_TOKEN)).json()["stages"]
    assert stages[-1]["stage"] == "legacy_stage"  # unknown stage sorts after the pinned order


def test_run_detail_unknown_run_is_404(client: TestClient) -> None:
    resp = client.get(f"/pipeline/runs/{uuid.uuid4()}", headers=auth(PARENT_TOKEN))
    assert resp.status_code == 404


# ----------------------------------------------------------------- rederive


def test_rederive_cascades_and_reruns(client: TestClient) -> None:
    use_fakes(client)
    session_id = make_session(client, with_findings=True)
    resp = client.post(
        f"/pipeline/sessions/{session_id}/rederive", json={}, headers=auth(PARENT_TOKEN)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["counts"]["findings_deleted"] == 1
    assert body["manual_invalidation"] is False
    assert body["run"]["status"] == "succeeded"  # start_run defaults to true
    assert body["run_locked"] is False


def test_rederive_without_rerun_skips_the_dag(client: TestClient) -> None:
    use_fakes(client)
    session_id = make_session(client, with_findings=True)
    resp = client.post(
        f"/pipeline/sessions/{session_id}/rederive",
        json={"start_run": False},
        headers=auth(PARENT_TOKEN),
    )
    assert resp.status_code == 200
    assert resp.json()["run"] is None


def test_rederive_blocked_by_manual_rows_is_409(client: TestClient) -> None:
    session_id = make_session(client, with_tag=True)
    resp = client.post(
        f"/pipeline/sessions/{session_id}/rederive", json={}, headers=auth(PARENT_TOKEN)
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["blockers"] == {"ball_tags": 1}
    assert "nothing was modified" in detail["message"]


def test_rederive_confirmed_invalidation_deletes_manual_rows(client: TestClient) -> None:
    use_fakes(client)
    session_id = make_session(client, with_tag=True)
    resp = client.post(
        f"/pipeline/sessions/{session_id}/rederive",
        json={"confirm_manual_invalidation": True},
        headers=auth(PARENT_TOKEN),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["manual_invalidation"] is True
    assert body["counts"]["ball_tags_deleted"] == 1


def test_rederive_run_locked_is_reported(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = make_session(client, with_findings=True)
    locked = PipelineOutcome(session_id=session_id, run_id=None, status=LOCKED, stages=())
    monkeypatch.setattr(router, "run_pipeline", lambda *_a, **_kw: locked)
    resp = client.post(
        f"/pipeline/sessions/{session_id}/rederive", json={}, headers=auth(PARENT_TOKEN)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["run"] is None
    assert body["run_locked"] is True


def test_rederive_busy_session_is_409(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    session_id = make_session(client)

    def busy(*args: Any, **kwargs: Any) -> Any:
        raise router.SessionBusyError(session_id)

    monkeypatch.setattr(router, "rederive_session", busy)
    resp = client.post(
        f"/pipeline/sessions/{session_id}/rederive", json={}, headers=auth(PARENT_TOKEN)
    )
    assert resp.status_code == 409
    assert "locked by a running pipeline" in resp.json()["detail"]


def test_rederive_unknown_session_is_404(client: TestClient) -> None:
    resp = client.post(
        f"/pipeline/sessions/{uuid.uuid4()}/rederive", json={}, headers=auth(PARENT_TOKEN)
    )
    assert resp.status_code == 404
