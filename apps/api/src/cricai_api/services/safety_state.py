"""Server-side safety truth for the coaching API (US-H1/H4/H5, SAF).

ONE function — :func:`compute_safety_state` — derives the authoritative
workload + wellness + SafetyVerdict for a player as of a date straight from the
player's REAL ledger, wellness rows and the governing ``safety_configs`` row.
It is the exact computation the worker's ``agent_stages._evaluate_safety``
performs (workload rolling summary + wellness state machine + safety supremacy
evaluate), lifted so the drill-plan and report endpoints consult server state
instead of trusting caller-supplied allowances or verdicts.

This closes the adversarial-review bypass where a parent injected a fat H1
allowance plus a forged inactive verdict while a pain flag or workload ceiling
was active in the DB: the endpoints now embed THIS verdict, and a client
allowance can only restrict it. It overrides the phase-5 plan's "injected
safety inputs" scope decision for the ``drills`` and ``reports`` seams.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from cricai_coaching import safety_agent, wellness, workload
from cricai_coaching.safety_config import DEFAULT_SAFETY_CONFIG
from cricai_data.models import (
    BowlingLedgerEntry,
    PainClearance,
    Player,
    SafetyConfig,
    WellnessCheckin,
)
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

__all__ = ["SafetyState", "active_safety_config", "compute_safety_state"]


def active_safety_config(db: OrmSession) -> dict[str, Any]:
    """The governing config: latest ``safety_configs`` version over the canonical
    defaults, else the defaults when unseeded (mirrors ``routers/workload.py``
    and ``agent_stages._evaluate_safety``).

    The latest row is shallow-merged over :data:`DEFAULT_SAFETY_CONFIG` so any
    section a stored config omits still resolves to a sane default (a valid,
    API-posted config carries all three sections, so this is a no-op for them
    and a safe floor for anything seeded directly).
    """
    row = db.scalar(select(SafetyConfig).order_by(SafetyConfig.version.desc()).limit(1))
    if row is None:
        return dict(DEFAULT_SAFETY_CONFIG)
    return {**DEFAULT_SAFETY_CONFIG, **row.config}


@dataclass(frozen=True)
class SafetyState:
    """The server's authoritative H1/H4/H5 state for a player as of a date."""

    as_of: date
    config: dict[str, Any]
    summary: workload.WindowSummary
    wellness_state: wellness.WellnessState
    verdict: dict[str, Any]

    @property
    def bowling_allowance(self) -> int:
        """Remaining H1 balls before the ceiling — conservatively zero when no
        age-band ceiling applies (mirrors the worker planner's fallback)."""
        remaining = self.summary.remaining_balls
        return remaining if remaining is not None else 0


def compute_safety_state(db: OrmSession, player: Player, as_of: date) -> SafetyState:
    """Derive the server-side SafetyVerdict + allowance for ``player`` as of ``as_of``.

    Reads the player's real bowling ledger and wellness rows, evaluates the H1
    rolling-7 summary and the H4 pain state machine under the governing config,
    and folds both into the pinned SafetyVerdict via ``safety_agent.evaluate``.
    The verdict is canonical (its text is byte-identical to the warning wording
    for its codes), so callers embed it directly.
    """
    config = active_safety_config(db)
    entries = list(
        db.scalars(select(BowlingLedgerEntry).where(BowlingLedgerEntry.player_id == player.id))
    )
    summary = workload.summarize_window(
        entries, birthdate=player.birthdate, end=as_of, config=config
    )
    checkins = list(
        db.scalars(select(WellnessCheckin).where(WellnessCheckin.player_id == player.id))
    )
    clearances = list(db.scalars(select(PainClearance).where(PainClearance.player_id == player.id)))
    wellness_state = wellness.evaluate_wellness(
        checkins, clearances, as_of=as_of, config=config.get("wellness")
    )
    ledger_summary = {"violations": [code.value for code in summary.violations]}
    verdict = safety_agent.evaluate(ledger_summary, wellness_state, config)
    return SafetyState(
        as_of=as_of,
        config=config,
        summary=summary,
        wellness_state=wellness_state,
        verdict=verdict,
    )
