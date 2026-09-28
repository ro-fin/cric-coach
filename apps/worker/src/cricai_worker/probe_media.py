"""Pipeline ``probe`` stage (US-J1): is there usable footage for this session?

The first DAG stage (:data:`cricai_worker.pipeline.STAGES`) is deliberately
thin and honest: it does not decode video, it reports which of the session's
:attr:`~cricai_data.models.Session.expected_cameras` actually have a
:class:`~cricai_data.models.Video` row whose status proves usable bytes exist
(:data:`cricai_data.enums.EVIDENCE_STATUSES`; FAILED/PENDING rows never count).

A missing expected camera is DATA, not failure (US-J1 degraded-but-honest):
the stage returns the missing list and lets downstream stages degrade around
it — ``Session.degraded`` / ``Session.missing_views`` already carry the same
truth for the API. The stage only raises :class:`LookupError` when the session
itself is gone (the runner's missing-session convention).
"""

from __future__ import annotations

import uuid
from typing import Any

from cricai_data.enums import EVIDENCE_STATUSES
from cricai_data.models import Session as SessionRow
from cricai_data.models import Video
from sqlalchemy import select

from cricai_worker.context import WorkerContext


def probe_session_media(ctx: WorkerContext, session_id: uuid.UUID) -> dict[str, Any]:
    """Report per-camera footage evidence for one session (``probe`` stage).

    Returns the expected cameras, those with usable footage, the missing ones
    (empty when complete), the raw video/evidence counts, and every evidence
    video's byte identity (``video_checksums``: sorted ``[camera, checksum]``
    pairs). The checksums make this payload — the digest currency every
    downstream media stage resumes against — track the actual stored bytes:
    replacing a video in place changes the probe output digest, so events/
    clips/pose/detect_track/metrics re-execute instead of resuming stale CV
    work (US-J1/L1 honest resume). Never raises on a degraded session — a
    missing view is reported, not fatal.
    """
    with ctx.session_factory() as db:
        session = db.get(SessionRow, session_id)
        if session is None:
            raise LookupError(f"session {session_id} not found")
        videos = list(db.scalars(select(Video).where(Video.session_id == session_id)))
        evidence = [video for video in videos if video.status in EVIDENCE_STATUSES]
        with_evidence = {video.camera_id for video in evidence}
        expected = list(session.expected_cameras)
        present = [camera for camera in expected if camera in with_evidence]
        missing = [camera for camera in expected if camera not in with_evidence]
        return {
            "expected_cameras": expected,
            "cameras_with_evidence": present,
            "missing_cameras": missing,
            "video_count": len(videos),
            "evidence_count": len(with_evidence),
            "video_checksums": sorted(
                [video.camera_id, video.checksum_sha256] for video in evidence
            ),
            "degraded": bool(missing),
        }
