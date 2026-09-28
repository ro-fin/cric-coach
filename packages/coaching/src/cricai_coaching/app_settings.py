"""Canonical app-settings defaults (US-J5 review gate, US-L5 live mode).

This is the single code-side source of the ``app_settings`` v1 seed; the
Phase-6 migration embeds the same literal and a drift test keeps them equal
(mirrors the ``safety_configs`` pattern). Runtime code reads settings from the
``app_settings`` table (latest version wins) — this constant is the seed and
the shape reference, never a bypass.

``report_review.mode`` is a :class:`~cricai_data.enums.ReviewMode` value; v1
ships ``auto_publish`` (reports publish immediately) so the coach-gate path is
opt-in. ``live_mode`` ships disabled with the ONLY per-ball fields the live
seam may ever emit (US-L5 SAF: technique corrections are never live).
"""

from typing import Any

#: The ``app_settings.version`` the migration seeds.
APP_SETTINGS_SEED_VERSION = 1

#: v1 defaults. ``report_review.timeout_hours`` is how long a coach-gated draft
#: waits before the publish sweep auto-publishes it. ``live_mode.allowlist`` is
#: the exhaustive set of per-ball fields the live streaming seam may surface —
#: counts and safety nudges only, never a technique verdict.
DEFAULT_APP_SETTINGS: dict[str, Any] = {
    "report_review": {"mode": "auto_publish", "timeout_hours": 24},
    "live_mode": {
        "enabled": False,
        "allowlist": [
            "ball_count",
            "target_hit_tally",
            "workload_remaining_balls",
            "fatigue_nudge",
        ],
    },
}
