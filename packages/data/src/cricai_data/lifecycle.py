"""Session lifecycle state machine (US-A3, US-C3, US-L1).

created → recording → captured → processing → analyzed, with failure edges.
Start is idempotent; a session that lost cameras is flagged degraded (not failed).
"""

from __future__ import annotations

from enum import StrEnum


class SessionState(StrEnum):
    CREATED = "created"
    RECORDING = "recording"
    CAPTURED = "captured"
    PROCESSING = "processing"
    ANALYZED = "analyzed"
    FAILED = "failed"


ALLOWED_TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.CREATED: frozenset({SessionState.RECORDING, SessionState.FAILED}),
    SessionState.RECORDING: frozenset({SessionState.CAPTURED, SessionState.FAILED}),
    SessionState.CAPTURED: frozenset({SessionState.PROCESSING, SessionState.FAILED}),
    SessionState.PROCESSING: frozenset({SessionState.ANALYZED, SessionState.FAILED}),
    SessionState.ANALYZED: frozenset(),
    SessionState.FAILED: frozenset({SessionState.PROCESSING}),  # retry after fix
}

#: Transitions that are silently ignored instead of rejected (idempotency, US-A3).
IDEMPOTENT_NOOPS: frozenset[tuple[SessionState, SessionState]] = frozenset(
    {
        (SessionState.RECORDING, SessionState.RECORDING),  # double-start
        (SessionState.CAPTURED, SessionState.CAPTURED),  # double-stop
    }
)


class SessionLifecycleError(Exception):
    def __init__(
        self, current: SessionState, requested: SessionState, reason: str | None = None
    ) -> None:
        super().__init__(reason or f"cannot transition session from {current!r} to {requested!r}")
        self.current = current
        self.requested = requested


def is_noop(current: SessionState, requested: SessionState) -> bool:
    return (current, requested) in IDEMPOTENT_NOOPS


def ensure_transition(
    current: SessionState,
    requested: SessionState,
    *,
    has_calibration: bool | None = None,
) -> SessionState:
    """Validate a transition; returns the resulting state.

    Idempotent no-ops return the current state unchanged; anything not
    allow-listed raises — sessions must never wander between states silently.

    US-C3 gate: a session must never reach ANALYZED without linked calibration
    parameters, and the gate is NOT opt-in. Callers transitioning into
    ANALYZED must pass ``has_calibration=session.calibration_id is not None``;
    ``False`` always raises, ``True`` merely lifts the gate (the transition
    matrix still applies), and ``None`` (the default) raises too — a caller
    that forgot to state the calibration status must not slip a session into
    ANALYZED. ``None`` keeps prior behavior for every other target.
    """
    if requested is SessionState.ANALYZED:
        if has_calibration is None:
            raise SessionLifecycleError(
                current,
                requested,
                reason="calibration status required to enter analyzed (US-C3)",
            )
        if not has_calibration:
            raise SessionLifecycleError(
                current, requested, reason="calibration required before analysis (US-C3)"
            )
    if is_noop(current, requested):
        return current
    if requested not in ALLOWED_TRANSITIONS[current]:
        raise SessionLifecycleError(current, requested)
    return requested
