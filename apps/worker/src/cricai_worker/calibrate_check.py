"""Pipeline ``calibrate_check`` stage (US-J1): report the calibration state.

Thin and honest, like ``probe``: it does not recalibrate, it reports whether
the session has a calibration attached, whether that calibration is
:attr:`~cricai_data.models.Calibration.valid`, whether US-C4 drift marked the
session ``calibration_suspect``, and the calibration's age in days at session
time. A missing (or dangling) calibration is DATA, never a failure — the
US-L4 quality scorer's ``calibration_freshness`` component turns that ``None``
age into a zero score, so the honesty banner surfaces it downstream rather than
the pipeline crashing here. Only a missing session raises (runner convention).
"""

from __future__ import annotations

import uuid
from typing import Any

from cricai_data.models import Calibration
from cricai_data.models import Session as SessionRow

from cricai_worker.context import WorkerContext


def check_session_calibration(ctx: WorkerContext, session_id: uuid.UUID) -> dict[str, Any]:
    """Report the session's calibration freshness state (``calibrate_check``).

    Returns whether a calibration is attached and resolvable, its validity, the
    US-C4 suspect flag, and its age in whole days relative to the session date
    (``None`` when no calibration is attached or the row is gone). Never raises
    on a missing calibration.
    """
    with ctx.session_factory() as db:
        session = db.get(SessionRow, session_id)
        if session is None:
            raise LookupError(f"session {session_id} not found")
        calibration = (
            None if session.calibration_id is None else db.get(Calibration, session.calibration_id)
        )
        age_days = (
            None
            if calibration is None
            else max(0.0, float((session.session_date - calibration.created_at.date()).days))
        )
        return {
            "calibration_id": None if calibration is None else str(calibration.id),
            "has_calibration": calibration is not None,
            "valid": None if calibration is None else calibration.valid,
            "calibration_suspect": session.calibration_suspect,
            "age_days": age_days,
        }
