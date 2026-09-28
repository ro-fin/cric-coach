"""US-C6 acceptance: heatmap aggregation, filters, PNG export, US-C4 exclusion."""

import io
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from cricai_data.enums import (
    BlockIntent,
    BowlerSource,
    Contact,
    EventSource,
    Footwork,
    Length,
    Line,
    Outcome,
    Shot,
)
from cricai_data.models import (
    BallEvent,
    BallTag,
    BounceEstimate,
    BounceMark,
    Session,
    SessionBlock,
)
from cricai_vision.zones import ZoneConfig
from fastapi.testclient import TestClient
from matplotlib import image as mpimg
from sqlalchemy import select

from cricai_testing.apptest import COACH_TOKEN, PARENT_TOKEN, PLAYER_TOKEN, auth

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _peak_cell_pixels(png: bytes) -> int:
    """Count pixels of the peak-density viridis yellow: a proxy for the hot cell's area."""
    rgba = mpimg.imread(io.BytesIO(png))
    mask = (rgba[..., 0] > 0.9) & (rgba[..., 1] > 0.8) & (rgba[..., 2] < 0.3)
    return int(mask.sum())


@contextmanager
def _db(client: TestClient) -> Iterator[Any]:
    factory = client.app.state.session_factory  # type: ignore[union-attr]
    db = factory()
    try:
        yield db
        db.commit()
    finally:
        db.close()


def _create_player(client: TestClient, *, is_guest: bool = False) -> str:
    response = client.post(
        "/players",
        json={"name": "Arjun", "birthdate": "2014-11-20", "is_guest": is_guest},
        headers=auth(PARENT_TOKEN),
    )
    player_id: str = response.json()["id"]
    return player_id


def _create_session(client: TestClient, player_id: str, *, bowler_source: str = "coach") -> str:
    payload: dict[str, Any] = {
        "player_id": player_id,
        "date": "2026-07-07",
        "session_type": "batting",
        "bowler_source": bowler_source,
    }
    if bowler_source == "machine":
        payload["machine_settings"] = {"speed_kph": 90, "length": "good"}
    response = client.post("/sessions", json=payload, headers=auth(PARENT_TOKEN))
    session_id: str = response.json()["id"]
    return session_id


def _make_session(client: TestClient, *, bowler_source: str = "coach") -> str:
    return _create_session(client, _create_player(client), bowler_source=bowler_source)


def _seed_mark(
    client: TestClient,
    session_id: str,
    ball_no: int,
    *,
    camera_id: str = "C3",
    line: Line | None = Line.OFF,
    length: Length | None = Length.GOOD,
    flagged: bool = False,
    pitch_x: float = 6.5,
    pitch_y: float = 0.25,
) -> None:
    """Seed a bounce mark directly (bounce router is another story's placeholder)."""
    with _db(client) as db:
        db.add(
            BounceMark(
                session_id=uuid.UUID(session_id),
                ball_no=ball_no,
                camera_id=camera_id,
                frame_no=1000 + ball_no,
                px_x=512.0,
                px_y=300.0,
                pitch_x=pitch_x,
                pitch_y=pitch_y,
                line=line,
                length=length,
                flagged_for_review=flagged,
            )
        )


def _seed_estimate(
    client: TestClient,
    session_id: str,
    ball_no: int,
    *,
    line: Line | None = Line.OFF,
    length: Length | None = Length.GOOD,
    pitch_x: float = 5.5,
    pitch_y: float = 0.2,
    confidence: float = 0.9,
) -> None:
    """Seed an auto bounce estimate directly (the worker job is another suite).

    A valid BallEvent rides along: estimates only ever exist for balls whose
    event was valid when the worker ran, and the resolver serves an auto
    estimate only while its event stays valid (US-D4)."""
    with _db(client) as db:
        db.add(
            BallEvent(
                session_id=uuid.UUID(session_id),
                ball_no=ball_no,
                start_ms=ball_no * 10_000,
                release_ms=ball_no * 10_000 + 400,
                end_ms=ball_no * 10_000 + 4_000,
                confidence=0.9,
                source=EventSource.AUTO,
                detector_version="det-1",
                valid=True,
            )
        )
        db.add(
            BounceEstimate(
                session_id=uuid.UUID(session_id),
                ball_no=ball_no,
                pitch_x=pitch_x,
                pitch_y=pitch_y,
                line=line,
                length=length,
                confidence=confidence,
                tracker_version="trk-1+bounce-est-1",
            )
        )


def _reject_event(client: TestClient, session_id: str, ball_no: int) -> None:
    """Flip the event invalid — exactly what POST /events/{id}/reject does."""
    with _db(client) as db:
        event = db.execute(
            select(BallEvent).where(
                BallEvent.session_id == uuid.UUID(session_id), BallEvent.ball_no == ball_no
            )
        ).scalar_one()
        event.valid = False


def _seed_tag(
    client: TestClient,
    session_id: str,
    ball_no: int,
    *,
    control: bool = True,
    contact: Contact = Contact.MIDDLE,
    shot: Shot = Shot.DRIVE,
    block_id: str | None = None,
) -> None:
    with _db(client) as db:
        db.add(
            BallTag(
                session_id=uuid.UUID(session_id),
                ball_no=ball_no,
                block_id=uuid.UUID(block_id) if block_id is not None else None,
                line=Line.OFF,
                length=Length.GOOD,
                shot=shot,
                footwork=Footwork.FRONT,
                contact=contact,
                outcome=Outcome.CONTROLLED_GROUND_SHOT,
                control=control,
                created_by="coach",
            )
        )


def _seed_block(
    client: TestClient,
    session_id: str,
    block_no: int,
    *,
    bowler_source: BowlerSource = BowlerSource.MACHINE,
) -> str:
    with _db(client) as db:
        block = SessionBlock(
            session_id=uuid.UUID(session_id),
            block_no=block_no,
            start_s=0.0,
            bowler_source=bowler_source,
            intent=BlockIntent.TECHNICAL,
        )
        db.add(block)
        db.flush()
        return str(block.id)


def _mark_suspect(client: TestClient, session_id: str) -> None:
    with _db(client) as db:
        db.get(Session, uuid.UUID(session_id)).calibration_suspect = True


def _get_heatmap(
    client: TestClient, session_id: str, token: str = PARENT_TOKEN, **params: Any
) -> Any:
    response = client.get(f"/sessions/{session_id}/heatmap", params=params, headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Aggregation math
# ---------------------------------------------------------------------------


def test_empty_session_has_empty_zone_table(client: TestClient) -> None:
    session_id = _make_session(client)
    body = _get_heatmap(client, session_id)
    assert body == {
        "session_id": session_id,
        "total_balls": 0,
        "cells": [],
        "flagged_balls": [],
        "points": [],
    }


def test_cell_counts_and_percentages(client: TestClient) -> None:
    session_id = _make_session(client)
    # Three tagged balls in (off, good): control T/T/F, contact middle/edge/miss.
    for ball_no, control, contact in (
        (1, True, Contact.MIDDLE),
        (2, True, Contact.EDGE),
        (3, False, Contact.MISS),
    ):
        _seed_mark(client, session_id, ball_no)
        _seed_tag(client, session_id, ball_no, control=control, contact=contact)
    # One tag-less ball in (leg, short): counted, null percentages (documented).
    _seed_mark(client, session_id, 4, line=Line.LEG, length=Length.SHORT)

    body = _get_heatmap(client, session_id)
    assert body["total_balls"] == 4
    assert body["cells"] == [
        {
            "line": "off",
            "length": "good",
            "balls": 3,
            "control_pct": 66.7,
            "false_shot_pct": 66.7,  # edge + miss out of 3 tagged
            "sources": {"manual": 3, "auto": 0},
            "hollow": 0,
        },
        {
            "line": "leg",
            "length": "short",
            "balls": 1,
            "control_pct": None,
            "false_shot_pct": None,
            "sources": {"manual": 1, "auto": 0},
            "hollow": 0,
        },
    ]


def test_marks_without_zone_classes_are_ignored(client: TestClient) -> None:
    session_id = _make_session(client)
    _seed_mark(client, session_id, 1, line=None, length=None)
    body = _get_heatmap(client, session_id)
    assert body["total_balls"] == 0
    assert body["cells"] == []


def test_multi_camera_ball_counted_once_flagged_if_any_mark_is(client: TestClient) -> None:
    session_id = _make_session(client)
    _seed_mark(client, session_id, 1, camera_id="C3")
    _seed_mark(client, session_id, 1, camera_id="C4", flagged=True)  # >15cm disagreement
    _seed_mark(client, session_id, 2, camera_id="C3", flagged=True)
    _seed_mark(client, session_id, 2, camera_id="C4")
    _seed_mark(client, session_id, 3, camera_id="C3")
    _seed_mark(client, session_id, 3, camera_id="C4")

    body = _get_heatmap(client, session_id)
    assert body["total_balls"] == 3  # never double-counted per camera
    assert body["cells"][0]["balls"] == 3
    assert body["flagged_balls"] == [1, 2]


def test_flagged_marks_included_in_cells_and_listed(client: TestClient) -> None:
    session_id = _make_session(client)
    _seed_mark(client, session_id, 7, flagged=True)
    body = _get_heatmap(client, session_id)
    assert body["total_balls"] == 1  # included, not hidden (review UI needs it)
    assert body["flagged_balls"] == [7]


# ---------------------------------------------------------------------------
# US-F4: auto bounces, precedence, confidence exclusion & provenance
# ---------------------------------------------------------------------------


def test_auto_estimates_fill_unmarked_balls_with_provenance(client: TestClient) -> None:
    session_id = _make_session(client)
    _seed_mark(client, session_id, 1)
    _seed_estimate(client, session_id, 2, confidence=0.9)

    body = _get_heatmap(client, session_id)
    assert body["total_balls"] == 2
    assert body["cells"][0]["sources"] == {"manual": 1, "auto": 1}
    assert body["cells"][0]["hollow"] == 0
    assert body["points"] == [
        {
            "ball_no": 1,
            "line": "off",
            "length": "good",
            "pitch_x": 6.5,
            "pitch_y": 0.25,
            "source": "manual",
            "confidence": None,  # a click is ground truth, not a model output
            "hollow": False,
        },
        {
            "ball_no": 2,
            "line": "off",
            "length": "good",
            "pitch_x": 5.5,
            "pitch_y": 0.2,
            "source": "auto",
            "confidence": 0.9,
            "hollow": False,
        },
    ]


@pytest.mark.safety
def test_manual_override_wins_over_auto_in_the_map(client: TestClient) -> None:
    """US-F4 AC: the manual mark, not the estimate, places the ball."""
    session_id = _make_session(client)
    _seed_mark(client, session_id, 1, line=Line.OFF, length=Length.GOOD)
    _seed_estimate(client, session_id, 1, line=Line.LEG, length=Length.SHORT, confidence=0.99)

    body = _get_heatmap(client, session_id)
    assert body["total_balls"] == 1  # never double-counted across sources
    (cell,) = body["cells"]
    assert (cell["line"], cell["length"]) == ("off", "good")  # the mark's cell
    assert cell["sources"] == {"manual": 1, "auto": 0}
    assert body["points"][0]["source"] == "manual"


def test_low_confidence_auto_excluded_by_default_included_hollow(client: TestClient) -> None:
    session_id = _make_session(client)
    _seed_estimate(client, session_id, 1, confidence=0.3)

    assert _get_heatmap(client, session_id)["total_balls"] == 0  # excluded (US-F4 AC)

    included = _get_heatmap(client, session_id, include_low_confidence="true")
    assert included["total_balls"] == 1
    assert included["points"][0]["hollow"] is True  # shown hollow, not solid
    assert included["cells"][0]["hollow"] == 1
    assert included["cells"][0]["sources"] == {"manual": 0, "auto": 1}

    # A lower threshold makes the same ball a solid, first-class point.
    lowered = _get_heatmap(client, session_id, min_confidence=0.2)
    assert lowered["total_balls"] == 1
    assert lowered["points"][0]["hollow"] is False
    assert lowered["cells"][0]["hollow"] == 0


def test_manual_marks_are_never_hollow_or_excluded(client: TestClient) -> None:
    session_id = _make_session(client)
    _seed_mark(client, session_id, 1)
    body = _get_heatmap(client, session_id, min_confidence=1.0)
    assert body["total_balls"] == 1
    assert body["points"][0]["hollow"] is False


def test_min_confidence_out_of_range_is_422(client: TestClient) -> None:
    session_id = _make_session(client)
    response = client.get(
        f"/sessions/{session_id}/heatmap",
        params={"min_confidence": 1.5},
        headers=auth(PARENT_TOKEN),
    )
    assert response.status_code == 422


def test_estimates_without_zone_classes_are_ignored(client: TestClient) -> None:
    session_id = _make_session(client)
    _seed_estimate(client, session_id, 1, line=None, length=None)
    body = _get_heatmap(client, session_id)
    assert body["total_balls"] == 0
    assert body["points"] == []


def test_auto_balls_respect_tag_filters(client: TestClient) -> None:
    session_id = _make_session(client)
    _seed_estimate(client, session_id, 1)  # tag-less auto ball
    _seed_estimate(client, session_id, 2)
    _seed_tag(client, session_id, 2, shot=Shot.CUT)

    assert _get_heatmap(client, session_id)["total_balls"] == 2
    body = _get_heatmap(client, session_id, shot="cut")
    assert body["total_balls"] == 1  # the tag-less auto ball is excluded
    assert body["points"][0]["ball_no"] == 2


def test_flagged_balls_stay_a_manual_review_concept(client: TestClient) -> None:
    session_id = _make_session(client)
    _seed_mark(client, session_id, 1, flagged=True)
    _seed_estimate(client, session_id, 2, confidence=0.9)
    body = _get_heatmap(client, session_id)
    assert body["total_balls"] == 2
    assert body["flagged_balls"] == [1]  # auto bounces never carry the flag


@pytest.mark.safety
def test_rejected_ball_estimate_leaves_the_heatmap_immediately(client: TestClient) -> None:
    """Rejecting an event hides its auto bounce at read time — before any
    operator re-runs estimate_bounces (audit bounce_resolve.py:83). Manual
    marks are ground truth and stay."""
    session_id = _make_session(client)
    _seed_estimate(client, session_id, 1)
    _seed_mark(client, session_id, 2)
    assert _get_heatmap(client, session_id)["total_balls"] == 2

    _reject_event(client, session_id, 1)
    body = _get_heatmap(client, session_id)
    assert body["total_balls"] == 1  # the phantom is gone without a job re-run
    (point,) = body["points"]
    assert (point["ball_no"], point["source"]) == (2, "manual")


def test_png_applies_the_same_confidence_params(client: TestClient) -> None:
    session_id = _make_session(client)
    _seed_estimate(client, session_id, 1, confidence=0.3)
    url = f"/sessions/{session_id}/heatmap.png"
    excluded = client.get(url, headers=auth(COACH_TOKEN))
    included = client.get(url, params={"include_low_confidence": "true"}, headers=auth(COACH_TOKEN))
    assert excluded.status_code == 200 and included.status_code == 200
    assert included.content.startswith(PNG_SIGNATURE)
    assert excluded.content != included.content  # the included ball renders


def test_player_trend_resolves_auto_bounces_with_the_same_rules(client: TestClient) -> None:
    player_id = _create_player(client)
    session_id = _create_session(client, player_id)
    _seed_mark(client, session_id, 1)
    _seed_estimate(client, session_id, 2, confidence=0.9)
    _seed_estimate(client, session_id, 3, confidence=0.3)  # excluded by default

    url = f"/players/{player_id}/heatmap"
    body = client.get(url, headers=auth(COACH_TOKEN)).json()
    assert body["total_balls"] == 2
    assert body["cells"][0]["sources"] == {"manual": 1, "auto": 1}

    included = client.get(
        url, params={"include_low_confidence": "true"}, headers=auth(COACH_TOKEN)
    ).json()
    assert included["total_balls"] == 3
    assert included["cells"][0]["hollow"] == 1


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_block_filter_keeps_only_that_blocks_tagged_balls(client: TestClient) -> None:
    session_id = _make_session(client)
    block_1 = _seed_block(client, session_id, 1)
    block_2 = _seed_block(client, session_id, 2)
    _seed_mark(client, session_id, 1)
    _seed_tag(client, session_id, 1, block_id=block_1)
    _seed_mark(client, session_id, 2)
    _seed_tag(client, session_id, 2, block_id=block_2)
    _seed_mark(client, session_id, 3)
    _seed_tag(client, session_id, 3)  # tagged, no block
    _seed_mark(client, session_id, 4)  # tag-less: excluded under tag filters

    body = _get_heatmap(client, session_id, block=1)
    assert body["total_balls"] == 1
    assert _get_heatmap(client, session_id)["total_balls"] == 4


def test_unknown_block_filter_is_404(client: TestClient) -> None:
    session_id = _make_session(client)
    response = client.get(
        f"/sessions/{session_id}/heatmap", params={"block": 99}, headers=auth(PARENT_TOKEN)
    )
    assert response.status_code == 404


def test_bowler_source_filter_uses_block_then_session_source(client: TestClient) -> None:
    session_id = _make_session(client, bowler_source="coach")
    machine_block = _seed_block(client, session_id, 1, bowler_source=BowlerSource.MACHINE)
    _seed_mark(client, session_id, 1)
    _seed_tag(client, session_id, 1, block_id=machine_block)  # block-level: machine
    _seed_mark(client, session_id, 2)
    _seed_tag(client, session_id, 2)  # un-blocked tag: session-level coach
    _seed_mark(client, session_id, 3)  # tag-less: session-level coach

    assert _get_heatmap(client, session_id, bowler_source="machine")["total_balls"] == 1
    assert _get_heatmap(client, session_id, bowler_source="coach")["total_balls"] == 2
    assert _get_heatmap(client, session_id, bowler_source="human")["total_balls"] == 0


def test_shot_contact_control_filters(client: TestClient) -> None:
    session_id = _make_session(client)
    _seed_mark(client, session_id, 1)
    _seed_tag(client, session_id, 1, shot=Shot.DRIVE, contact=Contact.EDGE, control=False)
    _seed_mark(client, session_id, 2)
    _seed_tag(client, session_id, 2, shot=Shot.CUT, contact=Contact.MIDDLE, control=True)
    _seed_mark(client, session_id, 3)  # tag-less

    assert _get_heatmap(client, session_id, shot="drive")["total_balls"] == 1
    assert _get_heatmap(client, session_id, contact="middle")["total_balls"] == 1
    assert _get_heatmap(client, session_id, control="true")["total_balls"] == 1
    assert _get_heatmap(client, session_id, control="false")["total_balls"] == 1
    assert _get_heatmap(client, session_id, shot="drive", control="true")["total_balls"] == 0
    assert _get_heatmap(client, session_id, shot="sweep")["total_balls"] == 0


# ---------------------------------------------------------------------------
# PNG export
# ---------------------------------------------------------------------------


def test_png_export_signature_filters_and_determinism(client: TestClient) -> None:
    session_id = _make_session(client)
    _seed_mark(client, session_id, 1)
    _seed_tag(client, session_id, 1, control=True)
    _seed_mark(client, session_id, 2)
    _seed_tag(client, session_id, 2, control=False)

    url = f"/sessions/{session_id}/heatmap.png"
    first = client.get(url, headers=auth(COACH_TOKEN))
    assert first.status_code == 200
    assert first.headers["content-type"] == "image/png"
    assert first.content.startswith(PNG_SIGNATURE)
    assert len(first.content) > 5_000

    again = client.get(url, headers=auth(COACH_TOKEN))
    assert again.content == first.content  # deterministic: daily report diffs bytes

    filtered = client.get(url, params={"control": "true"}, headers=auth(COACH_TOKEN))
    assert filtered.status_code == 200
    assert filtered.content != first.content  # filters change the rendered counts


def test_png_renders_bounce_points_to_scale(client: TestClient) -> None:
    """US-C6: identical zone counts but different mark positions render differently."""
    session_a = _make_session(client)
    session_b = _make_session(client)
    _seed_mark(client, session_a, 1, pitch_x=5.5, pitch_y=0.2)
    _seed_mark(client, session_b, 1, pitch_x=7.5, pitch_y=-0.2)  # same (off, good) cell

    png_a = client.get(f"/sessions/{session_a}/heatmap.png", headers=auth(COACH_TOKEN))
    png_b = client.get(f"/sessions/{session_b}/heatmap.png", headers=auth(COACH_TOKEN))
    assert png_a.status_code == 200 and png_b.status_code == 200
    # Same cells, counts, and title (same date + ball count): only the scattered
    # bounce points differ, so differing bytes prove marks render at true positions.
    assert png_a.content != png_b.content


def test_png_mark_outside_every_cell_still_renders(client: TestClient) -> None:
    session_id = _make_session(client)
    _seed_mark(client, session_id, 1, pitch_x=20.5, pitch_y=-1.7)  # off the shaded cells
    response = client.get(f"/sessions/{session_id}/heatmap.png", headers=auth(COACH_TOKEN))
    assert response.status_code == 200
    assert response.content.startswith(PNG_SIGNATURE)


def test_png_zone_config_changes_cell_geometry(client: TestClient) -> None:
    session_id = _make_session(client)
    _seed_mark(client, session_id, 1)  # (off, good): the single peak-density cell

    url = f"/sessions/{session_id}/heatmap.png"
    default = client.get(url, headers=auth(COACH_TOKEN))
    widened = ZoneConfig(
        length_bands_m={
            Length.YORKER: (0.0, 2.0),
            Length.FULL: (2.0, 5.0),
            Length.GOOD: (5.0, 12.0),  # 7 m wide vs the default 3 m
            Length.SHORT: (12.0, float("inf")),
        }
    )
    custom = client.get(
        url, params={"zone_config": json.dumps(widened.to_dict())}, headers=auth(COACH_TOKEN)
    )
    assert custom.status_code == 200
    assert custom.content != default.content
    # Geometric check: the widened GOOD band grows the peak cell's rendered area.
    assert _peak_cell_pixels(custom.content) > _peak_cell_pixels(default.content)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",  # not JSON at all
        '["not", "a", "dict"]',  # JSON, wrong shape
        '{"length_bands_m": {}}',  # missing line_channels_m
        (
            '{"length_bands_m": {"yorker": [0.0, 2.0]}, "line_channels_m": {}}'
        ),  # fails band validation
    ],
)
def test_png_invalid_zone_config_is_422(client: TestClient, raw: str) -> None:
    session_id = _make_session(client)
    response = client.get(
        f"/sessions/{session_id}/heatmap.png",
        params={"zone_config": raw},
        headers=auth(COACH_TOKEN),
    )
    assert response.status_code == 422
    assert "invalid zone config" in response.json()["detail"]


def test_png_export_scoping_matches_json(client: TestClient) -> None:
    guest_session = _create_session(client, _create_player(client, is_guest=True))
    response = client.get(f"/sessions/{guest_session}/heatmap.png", headers=auth(PLAYER_TOKEN))
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# RBAC & scoping
# ---------------------------------------------------------------------------


def test_heatmap_requires_auth(client: TestClient) -> None:
    session_id = _make_session(client)
    assert client.get(f"/sessions/{session_id}/heatmap").status_code == 401


def test_unknown_session_is_404(client: TestClient) -> None:
    response = client.get(f"/sessions/{uuid.uuid4()}/heatmap", headers=auth(PARENT_TOKEN))
    assert response.status_code == 404


def test_player_role_cannot_see_guest_session_heatmap(client: TestClient) -> None:
    guest_session = _create_session(client, _create_player(client, is_guest=True))
    _seed_mark(client, guest_session, 1)
    response = client.get(f"/sessions/{guest_session}/heatmap", headers=auth(PLAYER_TOKEN))
    assert response.status_code == 404  # existence hidden (US-L3)
    assert _get_heatmap(client, guest_session, token=PARENT_TOKEN)["total_balls"] == 1


def test_player_role_reads_own_nonguest_heatmap(client: TestClient) -> None:
    session_id = _make_session(client)
    _seed_mark(client, session_id, 1)
    assert _get_heatmap(client, session_id, token=PLAYER_TOKEN)["total_balls"] == 1


def test_unknown_player_heatmap_is_404(client: TestClient) -> None:
    response = client.get(f"/players/{uuid.uuid4()}/heatmap", headers=auth(PARENT_TOKEN))
    assert response.status_code == 404


def test_player_role_cannot_see_guest_player_trend(client: TestClient) -> None:
    guest_id = _create_player(client, is_guest=True)
    response = client.get(f"/players/{guest_id}/heatmap", headers=auth(PLAYER_TOKEN))
    assert response.status_code == 404
    parent = client.get(f"/players/{guest_id}/heatmap", headers=auth(PARENT_TOKEN))
    assert parent.status_code == 200


# ---------------------------------------------------------------------------
# Player trend aggregation & the US-C4 exclusion contract
# ---------------------------------------------------------------------------


def test_player_trend_aggregates_across_sessions(client: TestClient) -> None:
    player_id = _create_player(client)
    session_1 = _create_session(client, player_id)
    session_2 = _create_session(client, player_id)
    _seed_mark(client, session_1, 1)
    _seed_tag(client, session_1, 1, control=True)
    _seed_mark(client, session_2, 1)
    _seed_tag(client, session_2, 1, control=False)

    body = client.get(f"/players/{player_id}/heatmap", headers=auth(COACH_TOKEN)).json()
    assert body["total_balls"] == 2
    assert body["excluded_sessions"] == 0
    assert body["cells"] == [
        {
            "line": "off",
            "length": "good",
            "balls": 2,
            "control_pct": 50.0,
            "false_shot_pct": 0.0,
            "sources": {"manual": 2, "auto": 0},
            "hollow": 0,
        }
    ]


def test_suspect_session_excluded_from_trend_but_visible(client: TestClient) -> None:
    player_id = _create_player(client)
    good_session = _create_session(client, player_id)
    suspect_session = _create_session(client, player_id)
    _seed_mark(client, good_session, 1)
    _seed_mark(client, suspect_session, 1)
    _seed_mark(client, suspect_session, 2)
    _mark_suspect(client, suspect_session)

    body = client.get(f"/players/{player_id}/heatmap", headers=auth(PARENT_TOKEN)).json()
    assert body["total_balls"] == 1  # only the good session's ball
    assert body["excluded_sessions"] == 1

    # The suspect session's own heatmap stays reachable for review/re-verification.
    assert _get_heatmap(client, suspect_session)["total_balls"] == 2


@pytest.mark.safety
def test_suspect_marks_never_leak_into_trend_even_as_only_data(client: TestClient) -> None:
    """US-C4 exclusion contract: stale calibration must never corrupt trends."""
    player_id = _create_player(client)
    suspect_session = _create_session(client, player_id)
    _seed_mark(client, suspect_session, 1)
    _seed_mark(client, suspect_session, 2, line=Line.LEG, length=Length.SHORT)
    _seed_tag(client, suspect_session, 1, control=True)
    _mark_suspect(client, suspect_session)

    body = client.get(f"/players/{player_id}/heatmap", headers=auth(PARENT_TOKEN)).json()
    assert body["cells"] == []  # empty result, never suspect data
    assert body["total_balls"] == 0
    assert body["excluded_sessions"] == 1  # exclusion is visible, not silent
