"""Dev-stack demo seed (Phase 8, T1).

``scripts/dev_stack.py`` seeds the demo lab the dashboard, Playwright and
manual checks run against. The seed must stay HONEST: every row is one the
real API serves, the published report must pass the REAL publish gate, and
each role must see exactly what the API allows it to see. These tests drive
the seeded app through the API only, as the dashboard does.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("dev_stack", _REPO_ROOT / "scripts" / "dev_stack.py")
assert _spec is not None and _spec.loader is not None
dev_stack = importlib.util.module_from_spec(_spec)
sys.modules["dev_stack"] = dev_stack
_spec.loader.exec_module(dev_stack)


@pytest.fixture
def seeded(tmp_path: Path) -> tuple[TestClient, "dev_stack.SeedSummary"]:
    app = dev_stack.make_app(tmp_path / "storage", dev_stack.make_engine())
    summary = dev_stack.seed_demo(app)
    return TestClient(app), summary


def _get(client: TestClient, path: str, token: str) -> object:
    response = client.get(path, headers=dev_stack._auth(token))
    assert response.status_code == 200, (path, response.status_code, response.text)
    return response.json()


def test_sessions_tags_events_and_clips_are_served(seeded: tuple[TestClient, object]) -> None:
    client, summary = seeded
    page = _get(client, "/sessions", dev_stack.PLAYER_TOKEN)
    assert isinstance(page, dict)
    states = {row["id"]: row for row in page["items"]}
    analyzed = states[summary.analyzed_session_id]
    degraded = states[summary.degraded_session_id]
    assert analyzed["state"] == "analyzed"
    assert analyzed["degraded"] is False
    assert degraded["state"] == "captured"
    assert degraded["degraded"] is True
    assert degraded["missing_views"] == ["C2"]

    sid = summary.analyzed_session_id
    tags = _get(client, f"/sessions/{sid}/tags", dev_stack.PARENT_TOKEN)
    events = _get(client, f"/sessions/{sid}/events", dev_stack.PARENT_TOKEN)
    clips = _get(client, f"/sessions/{sid}/clips", dev_stack.PARENT_TOKEN)
    assert isinstance(tags, list) and isinstance(events, list) and isinstance(clips, list)
    assert len(tags) == dev_stack.ANALYZED_BALLS
    assert len(events) == dev_stack.ANALYZED_BALLS
    assert len(clips) == dev_stack.ANALYZED_BALLS * len(dev_stack.CAMERAS)
    assert sum(1 for tag in tags if tag["outcome"] == "edged") == dev_stack.FAULT_BALLS
    assert {clip["status"] for clip in clips} == {"cut"}


def test_published_report_passed_the_gate_and_the_player_sees_only_it(
    seeded: tuple[TestClient, object],
) -> None:
    client, summary = seeded
    path = f"/reports?player_id={summary.player_id}&kind=daily"
    player_view = _get(client, path, dev_stack.PLAYER_TOKEN)
    coach_view = _get(client, path, dev_stack.COACH_TOKEN)
    assert isinstance(player_view, list) and isinstance(coach_view, list)
    assert [row["id"] for row in player_view] == [summary.published_report_id]
    assert {row["status"] for row in player_view} == {"published"}
    assert {row["id"] for row in coach_view} == {
        summary.published_report_id,
        summary.draft_report_id,
    }
    body = player_view[0]["body"]
    assert f"Seen on {dev_stack.FAULT_BALLS} balls" in body["main_correction"]["text"]
    assert len(body["main_correction"]["evidence"]) == dev_stack.FAULT_BALLS


def test_review_queue_holds_the_draft_for_the_coach_only(
    seeded: tuple[TestClient, object],
) -> None:
    client, summary = seeded
    queue = _get(client, "/settings/review-queue", dev_stack.COACH_TOKEN)
    assert isinstance(queue, list)
    assert [item["id"] for item in queue] == [summary.draft_report_id]
    refused = client.get("/settings/review-queue", headers=dev_stack._auth(dev_stack.PLAYER_TOKEN))
    assert refused.status_code == 403


def test_seed_refuses_a_failed_api_call(tmp_path: Path) -> None:
    app = dev_stack.make_app(tmp_path / "storage", dev_stack.make_engine())
    with TestClient(app) as client, pytest.raises(RuntimeError, match="seed POST /players"):
        dev_stack._post(client, "/players", {"name": ""}, dev_stack.PARENT_TOKEN)


def test_web_env_sets_both_api_conventions() -> None:
    env = dev_stack.web_env("http://127.0.0.1:8000", base={"PATH": "x"})
    assert env["CRICAI_API_BASE_URL"] == "http://127.0.0.1:8000"
    assert env["NEXT_PUBLIC_API_BASE_URL"] == "http://127.0.0.1:8000"
    assert env["NEXT_TELEMETRY_DISABLED"] == "1"
    assert env["PATH"] == "x"


def test_banner_names_tokens_and_seed(seeded: tuple[TestClient, object]) -> None:
    _client, summary = seeded
    text = dev_stack.banner("http://a:1", "http://w:2", summary)
    for token in (dev_stack.PARENT_TOKEN, dev_stack.COACH_TOKEN, dev_stack.PLAYER_TOKEN):
        assert token in text
    assert summary.draft_report_id in text
    assert "http://w:2" in text
    empty = dev_stack.banner("http://a:1", None, None)
    assert "EMPTY" in empty
    assert "Dashboard" not in empty
