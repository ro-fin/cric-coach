"""Demo-session builder (US-L2: synthetic demo so every story is testable lab-free).

The demo is a full 500-ball batting day using the US-H2 default quality split,
with the T5.1 golden fault planted: degraded control on full outside-off.
"""

from __future__ import annotations

import json
from pathlib import Path

from cricai_data.enums import BlockIntent, BowlerSource, Length, Line
from cricai_data.synthetic import BlockSpec, SyntheticSession, ZoneBias, generate_session

DEMO_SEED = 20260707

#: US-H2 default split: 150 technical / 150 decision / 100 match / 50 spin / 50 fun.
DEMO_BLOCKS: tuple[BlockSpec, ...] = (
    BlockSpec(n_balls=150, intent=BlockIntent.TECHNICAL, machine_speed_kph=85.0),
    BlockSpec(n_balls=150, intent=BlockIntent.DECISION, machine_speed_kph=90.0),
    BlockSpec(n_balls=100, intent=BlockIntent.MATCH_SCENARIO, machine_speed_kph=95.0),
    BlockSpec(n_balls=50, bowler_source=BowlerSource.COACH, intent=BlockIntent.SPIN_SPECIFIC),
    BlockSpec(n_balls=50, intent=BlockIntent.FUN, machine_speed_kph=75.0),
)

#: T5.1 planted fault: weak control on full outside-off (the cover-drive channel).
DEMO_FAULT = ZoneBias(Line.OUTSIDE_OFF, Length.FULL, control_rate=0.45, weight=4.0)


def build_demo_session() -> SyntheticSession:
    return generate_session(seed=DEMO_SEED, blocks=DEMO_BLOCKS, zone_biases=(DEMO_FAULT,))


def write_demo_session(target: Path) -> Path:
    """Write the demo session JSON under ``target`` and return the file path."""
    target.mkdir(parents=True, exist_ok=True)
    path = target / "demo_session.json"
    path.write_text(json.dumps(build_demo_session().to_dict(), indent=2) + "\n")
    return path
