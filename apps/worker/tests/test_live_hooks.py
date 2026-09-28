"""US-L5 live hook seam: flag-gated, allow-listed, ≤30 s fatigue-nudge budget."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cricai_coaching.app_settings import DEFAULT_APP_SETTINGS
from cricai_coaching.live_allowlist import LiveSurfaceViolation
from cricai_worker.live_hooks import (
    FATIGUE_NUDGE_KEY,
    NUDGE_BUDGET_SECONDS,
    HookResult,
    LiveBallEvent,
    LiveBallHook,
)

SESSION_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
BALL_END = datetime(2026, 7, 11, 10, 0, 0, tzinfo=UTC)
ALLOWLIST: list[str] = list(DEFAULT_APP_SETTINGS["live_mode"]["allowlist"])


class FakeClock:
    """A settable clock: tests replay 'processing time' deterministically."""

    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


class CollectingEmitter:
    def __init__(self) -> None:
        self.events: list[LiveBallEvent] = []

    def emit(self, event: LiveBallEvent) -> None:
        self.events.append(event)


def _settings(*, enabled: bool = True, allowlist: list[str] | None = None) -> dict[str, Any]:
    return {
        "report_review": {"mode": "auto_publish", "timeout_hours": 24},
        "live_mode": {
            "enabled": enabled,
            "allowlist": ALLOWLIST if allowlist is None else allowlist,
        },
    }


def _hook(
    settings: dict[str, Any] | None = None,
    *,
    clock_at: datetime = BALL_END,
) -> tuple[LiveBallHook, CollectingEmitter, FakeClock]:
    emitter = CollectingEmitter()
    clock = FakeClock(clock_at)
    hook = LiveBallHook(settings=settings or _settings(), emitter=emitter, clock=clock)
    return hook, emitter, clock


def _counters(**extra: Any) -> dict[str, Any]:
    return {"ball_count": 12, "target_hit_tally": 4, "workload_remaining_balls": 88, **extra}


class TestFlagGate:
    def test_disabled_emits_nothing(self) -> None:
        """US-L5: the shipped default (live_mode off) is a fully dark surface."""
        hook, emitter, _clock = _hook(_settings(enabled=False))
        result = hook.on_ball_end(SESSION_ID, 1, _counters(), ball_end_at=BALL_END)
        assert result == HookResult(
            emitted=False,
            reason="live_mode_disabled",
            nudge_latency_s=None,
            dropped_stale_nudge=False,
        )
        assert emitter.events == []

    def test_enabled_emits_allow_listed_counters(self) -> None:
        hook, emitter, _clock = _hook()
        result = hook.on_ball_end(SESSION_ID, 12, _counters(), ball_end_at=BALL_END)
        assert result.emitted is True
        assert result.reason is None
        (event,) = emitter.events
        assert event.session_id == SESSION_ID
        assert event.ball_no == 12
        assert event.payload == _counters()
        assert event.emitted_at == BALL_END

    def test_default_clock_is_current_utc(self) -> None:
        emitter = CollectingEmitter()
        hook = LiveBallHook(settings=_settings(), emitter=emitter)
        before = datetime.now(tz=UTC)
        result = hook.on_ball_end(SESSION_ID, 1, {"ball_count": 1}, ball_end_at=before)
        assert result.emitted is True
        (event,) = emitter.events
        assert before <= event.emitted_at <= datetime.now(tz=UTC)


@pytest.mark.safety
class TestAllowList:
    def test_key_outside_allowlist_raises_and_emits_nothing(self) -> None:
        hook, emitter, _clock = _hook()
        with pytest.raises(LiveSurfaceViolation, match="outside the stored allow-list"):
            hook.on_ball_end(SESSION_ID, 1, _counters(score=4), ball_end_at=BALL_END)
        assert emitter.events == []

    def test_technique_key_raises_even_when_allow_listed(self) -> None:
        """SAF: technique corrections are never live — a stored allowlist
        carrying a technique key does not open the gate."""
        smuggled = _settings(allowlist=[*ALLOWLIST, "brace_state"])
        hook, emitter, _clock = _hook(smuggled)
        with pytest.raises(LiveSurfaceViolation, match="can never go live: brace_state"):
            hook.on_ball_end(
                SESSION_ID, 1, _counters(brace_state="collapsed"), ball_end_at=BALL_END
            )
        assert emitter.events == []

    def test_narrowed_allowlist_governs(self) -> None:
        hook, emitter, _clock = _hook(_settings(allowlist=["ball_count"]))
        with pytest.raises(LiveSurfaceViolation, match="target_hit_tally"):
            hook.on_ball_end(
                SESSION_ID, 1, {"ball_count": 1, "target_hit_tally": 0}, ball_end_at=BALL_END
            )
        assert hook.on_ball_end(SESSION_ID, 1, {"ball_count": 1}, ball_end_at=BALL_END).emitted
        assert len(emitter.events) == 1


class TestNudgeBudget:
    def test_nudge_within_budget_ships_with_latency(self) -> None:
        hook, emitter, clock = _hook()
        clock.advance(12.5)  # processing time: ball end -> emission
        result = hook.on_ball_end(
            SESSION_ID, 55, _counters(fatigue_nudge={"suggestion": "water"}), ball_end_at=BALL_END
        )
        assert result.emitted is True
        assert result.nudge_latency_s == 12.5
        assert result.dropped_stale_nudge is False
        (event,) = emitter.events
        assert event.payload[FATIGUE_NUDGE_KEY] == {"suggestion": "water"}

    def test_nudge_at_exactly_the_budget_still_ships(self) -> None:
        hook, _emitter, clock = _hook()
        clock.advance(NUDGE_BUDGET_SECONDS)
        result = hook.on_ball_end(
            SESSION_ID, 55, _counters(fatigue_nudge={"suggestion": "water"}), ball_end_at=BALL_END
        )
        assert result.nudge_latency_s == NUDGE_BUDGET_SECONDS
        assert result.dropped_stale_nudge is False

    def test_stale_nudge_is_dropped_but_counters_still_ship(self) -> None:
        """US-L5 AC: ball end -> nudge <= 30 s. A later nudge is stale coaching,
        not live coaching — dropped and reported, never delivered late."""
        hook, emitter, clock = _hook()
        clock.advance(NUDGE_BUDGET_SECONDS + 0.5)
        result = hook.on_ball_end(
            SESSION_ID, 55, _counters(fatigue_nudge={"suggestion": "water"}), ball_end_at=BALL_END
        )
        assert result.emitted is True
        assert result.reason == "stale_fatigue_nudge_dropped"
        assert result.dropped_stale_nudge is True
        assert result.nudge_latency_s is None
        (event,) = emitter.events
        assert FATIGUE_NUDGE_KEY not in event.payload
        assert event.payload == _counters()

    def test_no_nudge_means_no_latency_bookkeeping(self) -> None:
        hook, _emitter, clock = _hook()
        clock.advance(3600)  # counters alone have no freshness budget
        result = hook.on_ball_end(SESSION_ID, 1, _counters(), ball_end_at=BALL_END)
        assert result.emitted is True
        assert result.nudge_latency_s is None
        assert result.dropped_stale_nudge is False


class TestReplayedSession:
    def test_flag_on_incremental_path_over_a_replayed_session(self) -> None:
        """US-L5 IT: replay 60 balls 'live' against a fake clock — every event
        allow-listed, counters monotonic, the fatigue nudge inside its budget."""
        hook, emitter, clock = _hook()
        nudge_balls = {50, 55}
        for ball_no in range(1, 61):
            ball_end = clock.at
            clock.advance(1.5)  # simulated per-ball processing latency
            counters: dict[str, Any] = {
                "ball_count": ball_no,
                "target_hit_tally": ball_no // 3,
                "workload_remaining_balls": 120 - ball_no,
            }
            if ball_no in nudge_balls:
                counters["fatigue_nudge"] = {"suggestion": "water and a five-minute break"}
            result = hook.on_ball_end(SESSION_ID, ball_no, counters, ball_end_at=ball_end)
            assert result.emitted is True
            assert result.dropped_stale_nudge is False

        assert len(emitter.events) == 60
        counts = [event.payload["ball_count"] for event in emitter.events]
        assert counts == sorted(counts)
        nudged = [e for e in emitter.events if FATIGUE_NUDGE_KEY in e.payload]
        assert [e.ball_no for e in nudged] == [50, 55]
        for event in nudged:
            ball_end = event.emitted_at - timedelta(seconds=1.5)
            assert (event.emitted_at - ball_end).total_seconds() <= NUDGE_BUDGET_SECONDS


class TestSettingsValidation:
    @pytest.mark.parametrize(
        ("live_mode", "fragment"),
        [
            (None, "no 'live_mode' section"),
            ("on", "no 'live_mode' section"),
            ({"enabled": "yes", "allowlist": []}, "enabled must be a boolean"),
            ({"enabled": True, "allowlist": "ball_count"}, "must be a list of strings"),
            ({"enabled": True, "allowlist": ["ball_count", 3]}, "must be a list of strings"),
        ],
    )
    def test_malformed_live_settings_fail_loudly(self, live_mode: Any, fragment: str) -> None:
        settings: dict[str, Any] = {"report_review": {"mode": "auto_publish"}}
        if live_mode is not None:
            settings["live_mode"] = live_mode
        hook, emitter, _clock = _hook(settings)
        with pytest.raises(ValueError, match=fragment):
            hook.on_ball_end(SESSION_ID, 1, {"ball_count": 1}, ball_end_at=BALL_END)
        assert emitter.events == []

    def test_naive_ball_end_rejected(self) -> None:
        hook, _emitter, _clock = _hook()
        with pytest.raises(ValueError, match="ball_end_at must be timezone-aware"):
            hook.on_ball_end(
                SESSION_ID, 1, {"ball_count": 1}, ball_end_at=BALL_END.replace(tzinfo=None)
            )

    def test_naive_clock_rejected(self) -> None:
        hook, _emitter, clock = _hook()
        clock.at = clock.at.replace(tzinfo=None)
        with pytest.raises(ValueError, match=r"clock\(\) must be timezone-aware"):
            hook.on_ball_end(SESSION_ID, 1, {"ball_count": 1}, ball_end_at=BALL_END)
