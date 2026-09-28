"""The migration's seeded v1 must equal the canonical code constant (US-H1)."""

import importlib.util
from pathlib import Path
from types import ModuleType

from cricai_coaching.safety_config import DEFAULT_SAFETY_CONFIG, SAFETY_CONFIG_SEED_VERSION

_VERSIONS_DIR = Path(__file__).resolve().parents[2] / "data" / "alembic" / "versions"


def _load_phase5_migration() -> ModuleType:
    (path,) = _VERSIONS_DIR.glob("*_phase_5_coaching_safety_agents_pipeline.py")
    spec = importlib.util.spec_from_file_location("phase5_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_seed_matches_canonical_default() -> None:
    """Raising a ceiling by editing only one side must fail loudly."""
    migration = _load_phase5_migration()
    assert migration._SAFETY_CONFIG_V1 == DEFAULT_SAFETY_CONFIG
    assert migration._SAFETY_CONFIG_SEED_VERSION == SAFETY_CONFIG_SEED_VERSION


def test_defaults_match_docs_safety_workload() -> None:
    """Pin the documented ECB/CA numbers themselves, not just seed parity."""
    workload = DEFAULT_SAFETY_CONFIG["workload"]
    assert workload["age_bands"][0] == {
        "max_age": 11,
        "weekly_overs_target": [12, 16],
        "weekly_overs_ceiling": 16,
    }
    assert workload["age_bands"][1] == {
        "max_age": 13,
        "weekly_overs_target": [16, 20],
        "weekly_overs_ceiling": 20,
    }
    assert workload["max_bowling_days_per_rolling_7"] == 4
    assert workload["max_consecutive_day_pairs_per_rolling_7"] == 1
    blocks = DEFAULT_SAFETY_CONFIG["batting_split"]["blocks"]
    assert sum(blocks.values()) == DEFAULT_SAFETY_CONFIG["batting_split"]["daily_balls"]
    assert DEFAULT_SAFETY_CONFIG["batting_split"]["fun_block_protected"] is True
    assert DEFAULT_SAFETY_CONFIG["wellness"] == {
        "pain_escalation_count": 2,
        "pain_escalation_window_days": 14,
    }
