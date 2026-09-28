"""US-F5 fusion job: merge-by-key writes, degradation, idempotency (unit, SQLite)."""

import json
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from cricai_data.db import create_all, make_engine, make_session_factory
from cricai_data.enums import BowlerSource, LabelClass, MetricPhase, SessionType
from cricai_data.models import BallEvent, BallMetrics, BallTrack, Player, Session
from cricai_data.storage import FsObjectStore
from cricai_vision.audio_onset import Onset, OnsetKind
from cricai_vision.detect import Detection
from cricai_worker.context import WorkerContext
from cricai_worker.fuse_contacts import (
    SKIP_NO_PAYLOAD,
    SKIP_NO_TRACK_ROW,
    FuseInputs,
    fuse_session_contacts,
)
from sqlalchemy import select

FUSION = "contact-fusion-1.0.0"


@pytest.fixture
def ctx(tmp_path: Path) -> WorkerContext:
    engine = make_engine(f"sqlite:///{tmp_path / 'fuse.sqlite'}")
    create_all(engine)
    return WorkerContext(
        session_factory=make_session_factory(engine),
        store=FsObjectStore(tmp_path / "store"),
    )


def seed_session(ctx: WorkerContext) -> uuid.UUID:
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=date(2014, 11, 20))
        db.add(player)
        db.flush()
        session = Session(
            player_id=player.id,
            session_date=date(2026, 7, 8),
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
        )
        db.add(session)
        db.commit()
        return session.id


def add_event(
    ctx: WorkerContext,
    session_id: uuid.UUID,
    ball_no: int,
    *,
    contact_ms: int | None = 2000,
    valid: bool = True,
) -> None:
    with ctx.session_factory() as db:
        db.add(
            BallEvent(
                session_id=session_id,
                ball_no=ball_no,
                start_ms=0,
                release_ms=100,
                contact_ms=contact_ms,
                end_ms=3000,
                confidence=0.8,
                valid=valid,
            )
        )
        db.commit()


def pt(ts_ms: float, px_x: float, px_y: float) -> dict[str, Any]:
    return {
        "frame_no": round(ts_ms / 10),
        "ts_ms": ts_ms,
        "px_x": px_x,
        "px_y": px_y,
        "score": 0.9,
        "bridged": False,
    }


def middled_payload() -> dict[str, Any]:
    """Pre-contact velocity (100, 0), post-contact straight up: 90 deg deviation."""
    return {
        "points": [
            pt(1900.0, 50.0, 50.0),
            pt(1950.0, 100.0, 50.0),
            pt(2000.0, 150.0, 50.0),
            pt(2010.0, 150.0, 40.0),
            pt(2030.0, 150.0, 20.0),
        ],
        "segments": [
            {"kind": "post_contact", "start_ms": 2000.0, "end_ms": 2400.0, "confidence": 0.9}
        ],
        "flags": {"identity_risk": False, "long_gap": False},
    }


def bridged(ts_ms: float, px_x: float, px_y: float) -> dict[str, Any]:
    return pt(ts_ms, px_x, px_y) | {"score": 0.0, "bridged": True}


def occluded_contact_payload() -> dict[str, Any]:
    """Real ball occluded across the contact instant (2000 ms): bridged chord only
    within the contact tolerance, real points either side of the gap."""
    return {
        "points": [
            pt(1900.0, 50.0, 50.0),
            pt(1950.0, 100.0, 50.0),
            bridged(1975.0, 125.0, 37.5),
            bridged(2000.0, 150.0, 25.0),
            bridged(2025.0, 175.0, 12.5),
            pt(2050.0, 200.0, 0.0),
            pt(2075.0, 200.0, -50.0),
        ],
        "segments": [
            {"kind": "post_contact", "start_ms": 2000.0, "end_ms": 2400.0, "confidence": 0.9}
        ],
        "flags": {"identity_risk": False, "long_gap": False},
    }


def miss_payload() -> dict[str, Any]:
    """Unbroken flight: no post-contact segment at all."""
    return {
        "points": [pt(1900.0, 50.0, 50.0), pt(2000.0, 150.0, 50.0)],
        "segments": [{"kind": "pre_bounce", "start_ms": 0.0, "end_ms": 1500.0, "confidence": 0.9}],
        "flags": {"identity_risk": False, "long_gap": False},
    }


def add_track(
    ctx: WorkerContext,
    session_id: uuid.UUID,
    ball_no: int,
    *,
    raw: bytes | None,
    camera_id: str = "C1",
) -> str:
    """Insert a ball_tracks row; ``raw=None`` leaves the store payload missing."""
    key = f"sessions/{session_id}/balls/{ball_no}/track-{camera_id}.json"
    with ctx.session_factory() as db:
        db.add(
            BallTrack(
                session_id=session_id,
                ball_no=ball_no,
                camera_id=camera_id,
                tracker_version="trk-test-1",
                points_key=key,
                coverage=0.95,
                segments=[],
                flags={},
                confidence=0.9,
            )
        )
        db.commit()
    if raw is not None:
        ctx.store.put(key, raw)
    return key


def contact_detections() -> list[Detection]:
    """Ball+bat overlapping at the contact instant, plus an earlier bat box.

    The swing (0.6, 0.4) -> (0.5, 0.5) has theta = atan2(0.1, 0.1) = 45 deg
    (straight) under the default toward_ball_sign of -1.
    """
    return [
        Detection(60, 2000.0, LabelClass.BALL, 0.5, 0.5, 0.02, 0.02, 0.9),
        Detection(60, 2000.0, LabelClass.BAT, 0.5, 0.5, 0.05, 0.12, 0.85),
        Detection(54, 1800.0, LabelClass.BAT, 0.6, 0.4, 0.05, 0.12, 0.85),
    ]


@dataclass
class ScriptedDetectionProvider:
    """Deterministic story-local provider satisfying the DetectionProvider protocol."""

    detections: list[Detection] = field(default_factory=list)
    version: str = "scripted-detect-1"
    calls: list[tuple[float, float, float]] = field(default_factory=list)

    def detect(self, *, start_ms: float, end_ms: float, fps: float) -> list[Detection]:
        self.calls.append((start_ms, end_ms, fps))
        return [d for d in self.detections if start_ms <= d.ts_ms <= end_ms]


def crack(time_ms: float = 2005.0) -> Onset:
    return Onset(time_ms=time_ms, strength=0.9, kind=OnsetKind.BAT_CRACK)


def read_metrics_rows(ctx: WorkerContext, session_id: uuid.UUID, ball_no: int) -> list[BallMetrics]:
    with ctx.session_factory() as db:
        return list(
            db.execute(
                select(BallMetrics).where(
                    BallMetrics.session_id == session_id,
                    BallMetrics.ball_no == ball_no,
                    BallMetrics.phase == MetricPhase.CONTACT,
                )
            )
            .scalars()
            .all()
        )


def seed_e3_metrics(ctx: WorkerContext, session_id: uuid.UUID, ball_no: int) -> None:
    with ctx.session_factory() as db:
        db.add(
            BallMetrics(
                session_id=session_id,
                ball_no=ball_no,
                phase=MetricPhase.CONTACT,
                metrics={
                    "contact_point_class": {
                        "value": "under_eyes",
                        "unit": "class",
                        "confidence": 0.8,
                        "proxy": True,
                    }
                },
            )
        )
        db.commit()


def test_full_fusion_merges_into_existing_contact_row_preserving_e3_keys(
    ctx: WorkerContext,
) -> None:
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1)
    add_track(ctx, session_id, 1, raw=json.dumps(middled_payload()).encode("utf-8"))
    seed_e3_metrics(ctx, session_id, 1)
    provider = ScriptedDetectionProvider(detections=contact_detections())

    summary = fuse_session_contacts(
        ctx, session_id, inputs=FuseInputs(onsets=[crack()], detector=provider)
    )

    assert summary.fused_count == 1
    assert summary.skipped == ()
    ball = summary.fused[0]
    assert ball.ball_no == 1
    assert ball.contact_quality == "middle"
    assert ball.bat_path == "straight"
    assert ball.modalities == ("track", "audio", "bat")
    assert 0.0 < ball.confidence < 1.0
    assert provider.calls == [(0.0, 3000.0, 30.0)]

    rows = read_metrics_rows(ctx, session_id, 1)
    assert len(rows) == 1  # merged into the seeded row, not a second one
    metrics = rows[0].metrics
    # the US-E3 pose-proxy key survives untouched (pinned MERGE contract):
    assert metrics["contact_point_class"]["value"] == "under_eyes"
    assert metrics["contact_point_class"]["proxy"] is True
    quality = metrics["contact_quality"]
    assert quality["value"] == "middle"
    assert quality["unit"] == "class"
    assert quality["source"] == f"{FUSION}(track+audio+bat)"  # provenance names inputs
    assert "proxy" not in quality  # proxy=false serializes by omission (US-E3 payload rule)
    assert 0.0 < quality["confidence"] < 1.0
    path = metrics["bat_path"]
    assert path == {
        "value": "straight",
        "unit": "class",
        "confidence": 0.85,
        "source": f"{FUSION}(bat:2f)",
    }


def test_rerun_is_idempotent_and_creates_row_when_none_exists(ctx: WorkerContext) -> None:
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1)
    add_track(ctx, session_id, 1, raw=json.dumps(middled_payload()).encode("utf-8"))
    inputs = FuseInputs(
        onsets=[crack()], detector=ScriptedDetectionProvider(detections=contact_detections())
    )

    first = fuse_session_contacts(ctx, session_id, inputs=inputs)
    after_first = read_metrics_rows(ctx, session_id, 1)
    second = fuse_session_contacts(ctx, session_id, inputs=inputs)
    after_second = read_metrics_rows(ctx, session_id, 1)

    assert len(after_first) == 1  # created fresh (no pre-existing contact row)
    assert len(after_second) == 1
    assert after_first[0].metrics == after_second[0].metrics
    assert first.fused == second.fused


def test_skips_balls_without_track_row_or_stored_payload(ctx: WorkerContext) -> None:
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1)  # no ball_tracks row at all
    add_event(ctx, session_id, 2)
    add_track(ctx, session_id, 2, raw=None)  # row exists, payload bytes missing
    add_event(ctx, session_id, 3, valid=False)  # rejected events are not balls

    summary = fuse_session_contacts(ctx, session_id)

    assert summary.fused == ()
    assert summary.skipped == ((1, SKIP_NO_TRACK_ROW), (2, SKIP_NO_PAYLOAD))
    assert read_metrics_rows(ctx, session_id, 1) == []
    assert read_metrics_rows(ctx, session_id, 2) == []


@pytest.mark.parametrize(
    "raw",
    [
        b"not json at all",
        b"[1, 2]",  # JSON, but not an object
        json.dumps({"points": {}, "segments": [], "flags": {}}).encode(),  # bad points
    ],
)
def test_malformed_track_payload_degrades_to_audio_and_bat(ctx: WorkerContext, raw: bytes) -> None:
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1)
    add_track(ctx, session_id, 1, raw=raw)
    inputs = FuseInputs(
        onsets=[crack()], detector=ScriptedDetectionProvider(detections=contact_detections())
    )

    summary = fuse_session_contacts(ctx, session_id, inputs=inputs)

    assert summary.fused[0].modalities == ("audio", "bat")
    quality = read_metrics_rows(ctx, session_id, 1)[0].metrics["contact_quality"]
    assert quality["source"] == f"{FUSION}(audio+bat)"
    assert quality["value"] == "middle"


def test_decoy_ball_does_not_vote_when_real_ball_is_occluded_at_contact(
    ctx: WorkerContext,
) -> None:
    """US-F3 identity stress: the real ball's detection drops at the contact frame
    (bat occlusion, bridged track points) while a static decoy ball sits in the
    net. The decoy must not pair with the bat into a far/miss vote — the bat
    modality goes missing; the ball-independent bat_path still computes."""
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1)
    add_track(ctx, session_id, 1, raw=json.dumps(occluded_contact_payload()).encode("utf-8"))
    provider = ScriptedDetectionProvider(
        detections=[
            Detection(60, 2000.0, LabelClass.BALL, 0.15, 0.95, 0.02, 0.02, 0.8),  # decoy
            Detection(60, 2000.0, LabelClass.BAT, 0.85, 0.65, 0.05, 0.12, 0.85),
            Detection(54, 1800.0, LabelClass.BAT, 0.95, 0.55, 0.05, 0.12, 0.85),
        ]
    )

    summary = fuse_session_contacts(ctx, session_id, inputs=FuseInputs(detector=provider))

    ball = summary.fused[0]
    assert ball.modalities == ("track",)  # no impostor bat vote
    assert ball.contact_quality == "middle"  # real points still show the 90-deg turn
    metrics = read_metrics_rows(ctx, session_id, 1)[0].metrics
    assert metrics["contact_quality"]["source"] == f"{FUSION}(track)"
    assert metrics["bat_path"]["value"] == "straight"


def test_track_only_fusion_nulls_bat_path_without_provider(ctx: WorkerContext) -> None:
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1)
    add_track(ctx, session_id, 1, raw=json.dumps(middled_payload()).encode("utf-8"))

    summary = fuse_session_contacts(ctx, session_id)  # no onsets, no detector

    ball = summary.fused[0]
    assert ball.modalities == ("track",)
    assert ball.contact_quality == "middle"
    assert ball.bat_path is None
    metrics = read_metrics_rows(ctx, session_id, 1)[0].metrics
    assert metrics["contact_quality"]["source"] == f"{FUSION}(track)"
    assert metrics["bat_path"]["value"] is None
    assert metrics["bat_path"]["reason"] == "no detection provider configured"


def test_all_modalities_missing_writes_null_with_reason(ctx: WorkerContext) -> None:
    # A ball WITH a track payload whose content is malformed, no audio, no detector:
    # every modality degrades away and the metric is null-with-reason, never a guess.
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1, contact_ms=None)
    add_track(ctx, session_id, 1, raw=b"not json at all")

    summary = fuse_session_contacts(ctx, session_id)

    ball = summary.fused[0]
    assert ball.contact_quality is None
    assert ball.confidence == 0.0
    assert ball.modalities == ()
    quality = read_metrics_rows(ctx, session_id, 1)[0].metrics["contact_quality"]
    assert quality["value"] is None
    assert "all fusion modalities missing" in quality["reason"]


def test_contact_time_falls_back_to_post_contact_segment_start(ctx: WorkerContext) -> None:
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1, contact_ms=None)  # detector missed the contact time
    add_track(ctx, session_id, 1, raw=json.dumps(middled_payload()).encode("utf-8"))
    provider = ScriptedDetectionProvider(detections=contact_detections())

    summary = fuse_session_contacts(ctx, session_id, inputs=FuseInputs(detector=provider))

    ball = summary.fused[0]
    assert ball.modalities == ("track", "bat")  # bat plane = segment start (2000 ms)
    assert ball.contact_quality == "middle"
    assert ball.bat_path == "straight"


@pytest.mark.parametrize(
    "payload",
    [
        miss_payload(),  # well-formed, simply no post_contact segment
        # malformed segment shapes the tolerant fallback must skip over:
        {
            "points": [],
            "segments": [42, {"kind": "pre_bounce"}, {"kind": "post_contact", "start_ms": "x"}],
            "flags": {},
        },
        {"points": [], "flags": {}},  # segments key missing entirely
    ],
)
def test_no_contact_time_nulls_bat_path_with_reason(
    ctx: WorkerContext, payload: dict[str, Any]
) -> None:
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1, contact_ms=None)
    add_track(ctx, session_id, 1, raw=json.dumps(payload).encode("utf-8"))
    provider = ScriptedDetectionProvider(detections=contact_detections())

    summary = fuse_session_contacts(ctx, session_id, inputs=FuseInputs(detector=provider))

    assert "bat" not in summary.fused[0].modalities
    bat_path = read_metrics_rows(ctx, session_id, 1)[0].metrics["bat_path"]
    assert bat_path["value"] is None
    assert "no contact time" in bat_path["reason"]


def test_well_formed_miss_track_classifies_miss(ctx: WorkerContext) -> None:
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1, contact_ms=None)
    add_track(ctx, session_id, 1, raw=json.dumps(miss_payload()).encode("utf-8"))

    summary = fuse_session_contacts(ctx, session_id)

    ball = summary.fused[0]
    assert ball.modalities == ("track",)
    assert ball.contact_quality == "miss"


def test_onsets_callable_is_resolved_and_out_of_window_onsets_do_not_count(
    ctx: WorkerContext,
) -> None:
    session_id = seed_session(ctx)
    add_event(ctx, session_id, 1)
    add_track(ctx, session_id, 1, raw=json.dumps(middled_payload()).encode("utf-8"))

    with_audio = fuse_session_contacts(ctx, session_id, inputs=FuseInputs(onsets=lambda: [crack()]))
    assert with_audio.fused[0].modalities == ("track", "audio")

    # onset at 50 ms is before release (100 ms): never contact evidence (US-D3 rule)
    pre_release = fuse_session_contacts(
        ctx, session_id, inputs=FuseInputs(onsets=[crack(time_ms=50.0)])
    )
    assert pre_release.fused[0].modalities == ("track",)

    no_audio = fuse_session_contacts(ctx, session_id, inputs=FuseInputs(onsets=lambda: None))
    assert no_audio.fused[0].modalities == ("track",)


def test_session_not_found_raises(ctx: WorkerContext) -> None:
    with pytest.raises(ValueError, match="session not found"):
        fuse_session_contacts(ctx, uuid.uuid4())


def test_fuse_inputs_reject_non_positive_fps() -> None:
    with pytest.raises(ValueError, match="fps must be positive"):
        FuseInputs(fps=0.0)
