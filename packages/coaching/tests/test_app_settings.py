"""The migration's seeded app_settings v1 must equal the code constant (US-J5/L5)."""

import importlib.util
from pathlib import Path
from types import ModuleType

from cricai_coaching.app_settings import APP_SETTINGS_SEED_VERSION, DEFAULT_APP_SETTINGS

_VERSIONS_DIR = Path(__file__).resolve().parents[2] / "data" / "alembic" / "versions"


def _load_phase6_migration() -> ModuleType:
    (path,) = _VERSIONS_DIR.glob("*_phase_6_legspin_dashboard.py")
    spec = importlib.util.spec_from_file_location("phase6_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_seed_matches_canonical_default() -> None:
    """Changing a setting on only one side must fail loudly."""
    migration = _load_phase6_migration()
    assert migration._APP_SETTINGS_V1 == DEFAULT_APP_SETTINGS
    assert migration._APP_SETTINGS_SEED_VERSION == APP_SETTINGS_SEED_VERSION


def test_defaults_match_documented_gate_and_live_mode() -> None:
    """Pin the documented US-J5/L5 shape itself, not just seed parity."""
    review = DEFAULT_APP_SETTINGS["report_review"]
    assert review == {"mode": "auto_publish", "timeout_hours": 24}
    live = DEFAULT_APP_SETTINGS["live_mode"]
    assert live["enabled"] is False
    # SAF: the live seam may only ever surface counts and safety nudges — never
    # a technique verdict. Pin the exact allow-list.
    assert live["allowlist"] == [
        "ball_count",
        "target_hit_tally",
        "workload_remaining_balls",
        "fatigue_nudge",
    ]
