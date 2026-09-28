"""Canonical workload/safety threshold defaults (US-H1/H2/H4).

This is the single code-side source of the ``safety_configs`` v1 seed; the
Phase-5 migration embeds the same literal and a drift test keeps them equal.
Values come from ``docs/safety_workload.md`` (ECB junior fast-bowling
directives; Cricket Australia lumbar bone-stress guidance). Runtime code reads
thresholds from the ``safety_configs`` table (latest version wins) — this
constant is the seed and the shape reference, never a bypass.
"""

from typing import Any

#: The ``safety_configs.version`` the migration seeds.
SAFETY_CONFIG_SEED_VERSION = 1

#: v1 defaults. Age bands are inclusive upper bounds on age in years at the
#: window's end; overs are ceiling-checked after intensity weighting.
#: Throwdowns are arm throws, not a bowling action — they are recorded in the
#: ledger but weigh 0 toward the lumbar-stress overs ceiling.
DEFAULT_SAFETY_CONFIG: dict[str, Any] = {
    "workload": {
        "age_bands": [
            {"max_age": 11, "weekly_overs_target": [12, 16], "weekly_overs_ceiling": 16},
            {"max_age": 13, "weekly_overs_target": [16, 20], "weekly_overs_ceiling": 20},
        ],
        "balls_per_over": 6,
        "max_bowling_days_per_rolling_7": 4,
        "max_consecutive_day_pairs_per_rolling_7": 1,
        "intensity_weights": {"spin": 1.0, "pace_intent": 1.0, "throwdown": 0.0},
    },
    "batting_split": {
        "daily_balls": 500,
        "blocks": {
            "technical": 150,
            "decision": 150,
            "match_scenario": 100,
            "spin_specific": 50,
            "fun": 50,
        },
        "fun_block_protected": True,
        "deviation_alert_pct": 25,
    },
    "wellness": {
        "pain_escalation_count": 2,
        "pain_escalation_window_days": 14,
    },
}
