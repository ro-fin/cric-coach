"""US-L5: the per-ball live hook seam — allow-listed counters only, budgeted nudges.

Design-now/build-light: no streaming stack ships in Phase 6. This module IS
the documented seam a future live transport plugs into — the per-ball
incremental path calls :meth:`LiveBallHook.on_ball_end` after each ball, and
whatever implements :class:`LiveEmitter` (today: a test collector; later: a
LAN push channel) receives the event. Three properties are pinned here and
cannot rot silently:

1. **Flag-gated** — ``live_mode.enabled`` off (the shipped default) emits
   nothing at all.
2. **Allow-listed** — every payload key must pass
   :func:`cricai_coaching.live_allowlist.validate_live_payload`; per-ball
   technique verdicts raise even if smuggled into the stored allowlist (SAF).
3. **Budgeted** — a fatigue nudge is only live within
   :data:`NUDGE_BUDGET_SECONDS` of the ball ending (US-L5 AC: ball end ->
   nudge <= 30 s). A later nudge is stale coaching, not live coaching: it is
   dropped (the remaining counters still ship) and reported, never delivered
   late as if it were timely.

Counters are computed by the caller (the future incremental pipeline); the
hook owns policy, not analytics. All timestamps are timezone-aware UTC; the
injectable ``clock`` lets tests replay a session against a fake clock.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from cricai_coaching.live_allowlist import validate_live_payload
from cricai_data.models import utcnow

#: US-L5 latency budget (initial): ball end -> nudge on a live surface.
NUDGE_BUDGET_SECONDS = 30.0

#: The payload key carrying the fatigue nudge (in the shipped allow-list).
FATIGUE_NUDGE_KEY = "fatigue_nudge"

#: Injectable time source; production uses tz-aware UTC now.
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class LiveBallEvent:
    """One emitted per-ball live event: allow-listed counters, nothing else."""

    session_id: uuid.UUID
    ball_no: int
    payload: dict[str, Any]
    emitted_at: datetime


class LiveEmitter(Protocol):
    """Where live events go (the transport seam)."""

    def emit(self, event: LiveBallEvent) -> None: ...


@dataclass(frozen=True)
class HookResult:
    """What one ``on_ball_end`` call did, fully inspectable."""

    emitted: bool
    reason: str | None  # why nothing (or less) shipped; None on a clean emit
    nudge_latency_s: float | None  # ball end -> emit, only when a nudge shipped
    dropped_stale_nudge: bool


def _require_aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware UTC, got naive {value!r}")
    return value


def _live_settings(settings: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Parse and validate the ``live_mode`` section: ``(enabled, allowlist)``."""
    section = settings.get("live_mode")
    if not isinstance(section, Mapping):
        raise ValueError("app settings have no 'live_mode' section")
    enabled = section.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError(f"live_mode.enabled must be a boolean, got {enabled!r}")
    allowlist = section.get("allowlist")
    if not isinstance(allowlist, list) or not all(isinstance(k, str) for k in allowlist):
        raise ValueError(f"live_mode.allowlist must be a list of strings, got {allowlist!r}")
    return enabled, list(allowlist)


@dataclass(frozen=True)
class LiveBallHook:
    """The per-ball hook: apply live-mode policy to one ball's counters.

    ``settings`` is the governing ``app_settings`` mapping (the caller reads it
    once per session via ``cricai_coaching.review_gate.active_app_settings``);
    ``emitter`` is the transport seam. Policy is re-read per call so a
    mid-session settings flip takes effect on the next ball.
    """

    settings: Mapping[str, Any]
    emitter: LiveEmitter
    clock: Clock = field(default=utcnow)

    def on_ball_end(
        self,
        session_id: uuid.UUID,
        ball_no: int,
        counters: Mapping[str, Any],
        *,
        ball_end_at: datetime,
    ) -> HookResult:
        """Emit this ball's allow-listed counters, or nothing (US-L5).

        Raises :class:`~cricai_coaching.live_allowlist.LiveSurfaceViolation`
        when the caller assembled a payload the allow-list forbids — a coding
        error upstream must be loud, never filtered into silence.
        """
        _require_aware(ball_end_at, "ball_end_at")
        enabled, allowlist = _live_settings(self.settings)
        if not enabled:
            return HookResult(
                emitted=False,
                reason="live_mode_disabled",
                nudge_latency_s=None,
                dropped_stale_nudge=False,
            )
        payload = dict(counters)
        validate_live_payload(payload, allowlist)
        now = _require_aware(self.clock(), "clock()")
        latency: float | None = None
        dropped = False
        if FATIGUE_NUDGE_KEY in payload:
            latency = (now - ball_end_at).total_seconds()
            if latency > NUDGE_BUDGET_SECONDS:
                del payload[FATIGUE_NUDGE_KEY]
                latency = None
                dropped = True
        self.emitter.emit(
            LiveBallEvent(session_id=session_id, ball_no=ball_no, payload=payload, emitted_at=now)
        )
        return HookResult(
            emitted=True,
            reason="stale_fatigue_nudge_dropped" if dropped else None,
            nudge_latency_s=latency,
            dropped_stale_nudge=dropped,
        )
