"""US-A3/US-C3: session state machine — exhaustive transition matrix."""

import itertools

import pytest
from cricai_data.lifecycle import (
    ALLOWED_TRANSITIONS,
    SessionLifecycleError,
    SessionState,
    ensure_transition,
    is_noop,
)

ALL = list(SessionState)


def test_happy_path() -> None:
    order = [
        SessionState.CREATED,
        SessionState.RECORDING,
        SessionState.CAPTURED,
        SessionState.PROCESSING,
        SessionState.ANALYZED,
    ]
    for current, nxt in itertools.pairwise(order):
        # Entering ANALYZED requires an explicit calibration status (US-C3).
        calibration = True if nxt is SessionState.ANALYZED else None
        assert ensure_transition(current, nxt, has_calibration=calibration) is nxt


def test_double_start_and_double_stop_are_idempotent_noops() -> None:
    assert is_noop(SessionState.RECORDING, SessionState.RECORDING)
    assert ensure_transition(SessionState.RECORDING, SessionState.RECORDING) is (
        SessionState.RECORDING
    )
    assert ensure_transition(SessionState.CAPTURED, SessionState.CAPTURED) is (
        SessionState.CAPTURED
    )


def test_failed_can_retry_into_processing() -> None:
    assert ensure_transition(SessionState.FAILED, SessionState.PROCESSING) is (
        SessionState.PROCESSING
    )


def test_analyzed_is_terminal() -> None:
    assert ALLOWED_TRANSITIONS[SessionState.ANALYZED] == frozenset()


@pytest.mark.parametrize("current", ALL)
@pytest.mark.parametrize("requested", ALL)
def test_full_matrix(current: SessionState, requested: SessionState) -> None:
    # The matrix is tested with the US-C3 gate satisfied where it applies:
    # entering ANALYZED demands an explicit calibration status (see below).
    calibration = True if requested is SessionState.ANALYZED else None
    allowed = requested in ALLOWED_TRANSITIONS[current] or is_noop(current, requested)
    if allowed:
        assert ensure_transition(current, requested, has_calibration=calibration) in (
            current,
            requested,
        )
    else:
        with pytest.raises(SessionLifecycleError) as exc:
            ensure_transition(current, requested, has_calibration=calibration)
        assert exc.value.current is current
        assert exc.value.requested is requested


@pytest.mark.safety
def test_analyzed_requires_calibration() -> None:
    """US-C3 gate: no session reaches ANALYZED without linked calibration."""
    with pytest.raises(SessionLifecycleError) as exc:
        ensure_transition(SessionState.PROCESSING, SessionState.ANALYZED, has_calibration=False)
    assert str(exc.value) == "calibration required before analysis (US-C3)"
    assert exc.value.current is SessionState.PROCESSING
    assert exc.value.requested is SessionState.ANALYZED


@pytest.mark.safety
def test_calibration_gate_applies_from_any_state() -> None:
    for current in ALL:
        with pytest.raises(SessionLifecycleError) as exc:
            ensure_transition(current, SessionState.ANALYZED, has_calibration=False)
        assert str(exc.value) == "calibration required before analysis (US-C3)"


def test_analyzed_allowed_with_calibration() -> None:
    assert ensure_transition(
        SessionState.PROCESSING, SessionState.ANALYZED, has_calibration=True
    ) is (SessionState.ANALYZED)


def test_has_calibration_true_never_widens_the_matrix() -> None:
    """The gate only blocks; it must not admit otherwise-invalid transitions."""
    with pytest.raises(SessionLifecycleError) as exc:
        ensure_transition(SessionState.CREATED, SessionState.ANALYZED, has_calibration=True)
    assert exc.value.current is SessionState.CREATED
    assert str(exc.value) == (
        "cannot transition session from "
        "<SessionState.CREATED: 'created'> to <SessionState.ANALYZED: 'analyzed'>"
    )


def test_has_calibration_ignored_for_non_analyzed_targets() -> None:
    assert ensure_transition(
        SessionState.CREATED, SessionState.RECORDING, has_calibration=False
    ) is (SessionState.RECORDING)
    # Idempotent no-ops stay no-ops whatever the calibration flag says.
    assert ensure_transition(
        SessionState.RECORDING, SessionState.RECORDING, has_calibration=False
    ) is (SessionState.RECORDING)


@pytest.mark.safety
def test_analyzed_requires_explicit_calibration_status() -> None:
    """US-C3: the gate is not opt-in — omitting the status must never slip a
    session into ANALYZED (a forgetful Phase 3 worker fails loudly)."""
    for current in ALL:
        with pytest.raises(SessionLifecycleError) as exc:
            ensure_transition(current, SessionState.ANALYZED)
        assert str(exc.value) == "calibration status required to enter analyzed (US-C3)"
        assert exc.value.current is current
        assert exc.value.requested is SessionState.ANALYZED
    with pytest.raises(SessionLifecycleError):
        ensure_transition(SessionState.PROCESSING, SessionState.ANALYZED, has_calibration=None)


def test_default_none_preserves_behavior_for_non_analyzed_targets() -> None:
    """Callers not targeting ANALYZED keep the exact old semantics."""
    assert ensure_transition(SessionState.CREATED, SessionState.RECORDING) is (
        SessionState.RECORDING
    )
    assert ensure_transition(
        SessionState.PROCESSING, SessionState.FAILED, has_calibration=None
    ) is (SessionState.FAILED)


def test_every_state_reachable_from_created() -> None:
    reachable = {SessionState.CREATED}
    frontier = [SessionState.CREATED]
    while frontier:
        nxt = ALLOWED_TRANSITIONS[frontier.pop()] - reachable
        reachable |= nxt
        frontier.extend(nxt)
    assert reachable == set(ALL)
