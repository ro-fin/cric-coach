import json
from pathlib import Path

from cricai_data.demo import DEMO_BLOCKS, build_demo_session, write_demo_session
from cricai_data.enums import Length, Line, Shot


def test_demo_session_is_500_balls_with_h2_split() -> None:
    session = build_demo_session()
    assert len(session.balls) == 500
    assert [b.n_balls for b in DEMO_BLOCKS] == [150, 150, 100, 50, 50]


def test_demo_session_is_deterministic() -> None:
    assert build_demo_session().to_dict() == build_demo_session().to_dict()


def test_demo_fault_zone_control_is_degraded() -> None:
    session = build_demo_session()
    in_zone = [
        b
        for b in session.balls
        if b.line is Line.OUTSIDE_OFF and b.length is Length.FULL and b.shot is not Shot.LEAVE
    ]
    assert len(in_zone) >= 30
    control = sum(b.control for b in in_zone) / len(in_zone)
    assert control < 0.6


def test_write_demo_session(tmp_path: Path) -> None:
    path = write_demo_session(tmp_path / "demo")
    payload = json.loads(path.read_text())
    assert len(payload["balls"]) == 500
    # Idempotent rewrite
    assert write_demo_session(tmp_path / "demo") == path
