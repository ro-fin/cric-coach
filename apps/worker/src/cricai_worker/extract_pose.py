"""Per-ball pose extraction job (US-E1): valid ball events x cameras -> pose tracks.

For every valid :class:`~cricai_data.models.BallEvent` and each requested camera
with footage, the injected :class:`~cricai_vision.pose.PoseProvider` extracts the
batter's track. The versioned JSON payload lands at the pinned object-store key
``sessions/{session_id}/balls/{ball_no}/pose-{camera_id}.json`` (retention US-B6
protects the ``sessions/{sid}/balls/{ball_no}/`` prefix for ground-truth balls)
and a ``pose_tracks`` row is upserted on the (session_id, ball_no, camera_id)
unique triple — model name/version, frame count, availability and batter
selection confidence (US-E1: linkage + schema versioned per run).

Idempotent: re-running overwrites payloads and updates rows in place, so
crash-resume is a plain re-run. Each track's store write and row upsert commit
together, per ball x camera — a mid-run crash never strands an overwritten
payload behind a stale committed row (rows and payloads are versioned
provenance, so their metadata must always describe the stored bytes). Cameras
without footage and low-availability tracks are reported loudly in the summary
— frames are never fabricated to pad availability (US-E1 AC).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from cricai_data.db import session_scope
from cricai_data.models import BallEvent
from cricai_data.models import PoseTrack as PoseTrackRow
from cricai_data.models import Session as SessionRow
from cricai_vision.pose import PoseProvider
from sqlalchemy import select
from sqlalchemy.orm import Session

from cricai_worker.context import WorkerContext

#: Side-on reference camera first (C1), then the secondary angle (C3).
DEFAULT_CAMERAS: tuple[str, ...] = ("C1", "C3")

#: US-E1 AC: landmark availability >= 95% in the ball window; below this is flagged.
MIN_AVAILABILITY = 0.95

#: Returns (frames, fps) for one (session, ball, camera), or None when no footage exists.
FramesResolver = Callable[[uuid.UUID, int, str], "tuple[Sequence[Any], float] | None"]


def pose_key(session_id: uuid.UUID, ball_no: int, camera_id: str) -> str:
    """Pinned object-store key of a per-ball pose payload (US-E1 storage layout)."""
    if ball_no < 1:
        raise ValueError(f"ball_no must be >= 1, got {ball_no}")
    return f"sessions/{session_id}/balls/{ball_no}/pose-{camera_id}.json"


@dataclass(frozen=True)
class PoseExtractionSummary:
    """What one run did: tracks written, footage gaps, low-availability flags."""

    session_id: str
    extracted: int
    missing: tuple[tuple[int, str], ...]
    low_availability: tuple[tuple[int, str], ...]


def _track_row(db: Session, session_id: uuid.UUID, ball_no: int, camera_id: str) -> PoseTrackRow:
    """Existing (session_id, ball_no, camera_id) pose_tracks row, or a fresh one (upsert)."""
    row = db.execute(
        select(PoseTrackRow).where(
            PoseTrackRow.session_id == session_id,
            PoseTrackRow.ball_no == ball_no,
            PoseTrackRow.camera_id == camera_id,
        )
    ).scalar_one_or_none()
    if row is None:
        row = PoseTrackRow(session_id=session_id, ball_no=ball_no, camera_id=camera_id)
        db.add(row)
    return row


def extract_session_pose(
    ctx: WorkerContext,
    session_id: uuid.UUID,
    *,
    provider: PoseProvider,
    frames_resolver: FramesResolver,
    cameras: Sequence[str] = DEFAULT_CAMERAS,
) -> PoseExtractionSummary:
    """Extract pose tracks for every valid ball event x camera with footage."""
    extracted = 0
    missing: list[tuple[int, str]] = []
    low_availability: list[tuple[int, str]] = []
    with session_scope(ctx.session_factory) as db:
        if db.get(SessionRow, session_id) is None:
            raise ValueError(f"session not found: {session_id}")
        events = (
            db.execute(
                select(BallEvent)
                .where(BallEvent.session_id == session_id, BallEvent.valid.is_(True))
                .order_by(BallEvent.ball_no)
            )
            .scalars()
            .all()
        )
        for event in events:
            for camera_id in cameras:
                resolved = frames_resolver(session_id, event.ball_no, camera_id)
                if resolved is None:
                    missing.append((event.ball_no, camera_id))
                    continue
                frames, fps = resolved
                track = provider.extract(frames, fps=fps)
                key = pose_key(session_id, event.ball_no, camera_id)
                ctx.store.put(key, json.dumps(track.to_payload()).encode("utf-8"))
                availability = track.availability()
                row = _track_row(db, session_id, event.ball_no, camera_id)
                row.model_name = track.model_name
                row.model_version = track.model_version
                row.landmarks_key = key
                row.frame_count = track.frame_count
                row.availability = availability
                row.subject_confidence = track.subject_confidence
                db.commit()  # per-track durability: payload + row land together
                extracted += 1
                if availability < MIN_AVAILABILITY:
                    low_availability.append((event.ball_no, camera_id))
    return PoseExtractionSummary(
        session_id=str(session_id),
        extracted=extracted,
        missing=tuple(missing),
        low_availability=tuple(low_availability),
    )
