"""Shared capture-state recomputation (US-A3 / US-B2 seam).

Both the lifecycle router (stop / re-stop) and the videos router (upload
completion after stop) must agree on what counts as footage evidence and how
degraded/missing_views are derived — so the rule lives in exactly one place:
the evidence set itself is :data:`cricai_data.enums.EVIDENCE_STATUSES`
(re-exported here for the routers), shared with the worker's clip cutter.
"""

from __future__ import annotations

from cricai_data.enums import EVIDENCE_STATUSES
from cricai_data.models import Session, Video
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

__all__ = ["EVIDENCE_STATUSES", "cameras_with_footage", "recompute_missing_views"]


def cameras_with_footage(db: OrmSession, session: Session) -> set[str]:
    rows = db.scalars(
        select(Video.camera_id).where(
            Video.session_id == session.id,
            Video.status.in_(list(EVIDENCE_STATUSES)),
        )
    ).all()
    return set(rows)


def recompute_missing_views(db: OrmSession, session: Session) -> None:
    """Re-derive missing_views/degraded from persisted expectations + evidence.

    No-op until the session has recorded expected cameras (i.e., before start).
    Late uploads can clear a camera from missing_views; cameras can never be
    silently added back once footage exists.
    """
    expected = set(session.expected_cameras)
    if not expected:
        return
    missing = sorted(expected - cameras_with_footage(db, session))
    session.missing_views = missing
    session.degraded = bool(missing)
