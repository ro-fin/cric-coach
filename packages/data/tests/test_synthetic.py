"""Synthetic session generator tests: determinism, structure, fault planting."""

import json

from cricai_data.enums import BlockIntent, BowlerSource, Contact, Footwork, Length, Line, Shot
from cricai_data.synthetic import (
    DEFAULT_MACHINE_SPEED_KPH,
    BlockSpec,
    SyntheticBall,
    ZoneBias,
    generate_session,
)


def _standard_blocks() -> list[BlockSpec]:
    return [
        BlockSpec(n_balls=60, intent=BlockIntent.TECHNICAL, machine_speed_kph=85.0),
        BlockSpec(n_balls=40, bowler_source=BowlerSource.COACH, intent=BlockIntent.DECISION),
    ]


def test_same_seed_is_byte_identical() -> None:
    a = generate_session(seed=42, blocks=_standard_blocks())
    b = generate_session(seed=42, blocks=_standard_blocks())
    assert json.dumps(a.to_dict()) == json.dumps(b.to_dict())


def test_different_seed_differs() -> None:
    a = generate_session(seed=1, blocks=_standard_blocks())
    b = generate_session(seed=2, blocks=_standard_blocks())
    assert a.to_dict() != b.to_dict()


def test_ball_numbering_is_sequential_across_blocks() -> None:
    session = generate_session(seed=7, blocks=_standard_blocks())
    assert [ball.ball_no for ball in session.balls] == list(range(1, 101))
    assert {ball.block_no for ball in session.balls} == {1, 2}
    assert sum(1 for ball in session.balls if ball.block_no == 1) == 60


def test_block_context_is_inherited_by_balls() -> None:
    session = generate_session(seed=7, blocks=_standard_blocks())
    for ball in session.balls:
        if ball.block_no == 1:
            assert ball.bowler_source is BowlerSource.MACHINE
            assert ball.intent is BlockIntent.TECHNICAL
        else:
            assert ball.bowler_source is BowlerSource.COACH
            assert ball.intent is BlockIntent.DECISION


def test_machine_speed_defaults_and_stays_tight() -> None:
    session = generate_session(seed=3, blocks=[BlockSpec(n_balls=50)])
    for ball in session.balls:
        assert abs(ball.speed_kph - DEFAULT_MACHINE_SPEED_KPH) <= 2.0


def test_human_bowler_speed_varies_more_than_machine() -> None:
    machine = generate_session(seed=5, blocks=[BlockSpec(n_balls=200)])
    human = generate_session(
        seed=5, blocks=[BlockSpec(n_balls=200, bowler_source=BowlerSource.HUMAN)]
    )

    def spread(balls: tuple[SyntheticBall, ...]) -> float:
        speeds = [b.speed_kph for b in balls]
        return max(speeds) - min(speeds)

    assert spread(human.balls) > spread(machine.balls)


def test_zone_bias_plants_low_control_fault() -> None:
    """T5.1 seed: a planted fault zone must show clearly degraded control."""
    fault = ZoneBias(Line.OUTSIDE_OFF, Length.FULL, control_rate=0.30, weight=8.0)
    session = generate_session(seed=11, blocks=[BlockSpec(n_balls=300)], zone_biases=[fault])

    in_zone = [
        b
        for b in session.balls
        if b.line is Line.OUTSIDE_OFF and b.length is Length.FULL and b.shot is not Shot.LEAVE
    ]
    out_zone = [
        b
        for b in session.balls
        if (b.line, b.length) != (Line.OUTSIDE_OFF, Length.FULL) and b.shot is not Shot.LEAVE
    ]
    assert len(in_zone) >= 30, "weight boost must give the fault zone sample size"

    control_in = sum(b.control for b in in_zone) / len(in_zone)
    control_out = sum(b.control for b in out_zone) / len(out_zone)
    assert control_in < 0.5 < control_out


def test_leaves_are_controlled_misses_with_leave_footwork() -> None:
    session = generate_session(seed=13, blocks=[BlockSpec(n_balls=400)])
    leaves = [b for b in session.balls if b.shot is Shot.LEAVE]
    assert leaves, "seeded session should contain leaves"
    for ball in leaves:
        assert ball.footwork is Footwork.LEAVE
        assert ball.contact is Contact.MISS
        assert ball.control is True
        assert ball.outcome.value == "left_alone"


def test_footwork_follows_length_for_played_shots() -> None:
    session = generate_session(seed=17, blocks=[BlockSpec(n_balls=400)])
    for ball in session.balls:
        if ball.shot is Shot.LEAVE:
            continue
        if ball.length in (Length.FULL, Length.YORKER):
            assert ball.footwork is Footwork.FRONT
        else:
            assert ball.footwork is Footwork.BACK


def test_outcome_consistency() -> None:
    session = generate_session(seed=19, blocks=[BlockSpec(n_balls=400)])
    for ball in session.balls:
        if ball.shot is Shot.LEAVE:
            continue
        if ball.contact is Contact.MISS:
            assert ball.outcome.value == "beaten"
        elif ball.contact is Contact.EDGE:
            assert ball.outcome.value == "edged"
        elif ball.control:
            assert ball.outcome.value in ("controlled_ground_shot", "controlled_aerial")
        else:
            assert ball.outcome.value == "uncontrolled"


def test_controlled_lofts_are_aerial() -> None:
    session = generate_session(seed=29, blocks=[BlockSpec(n_balls=600)])
    controlled_lofts = [
        b
        for b in session.balls
        if b.shot is Shot.LOFT and b.control and b.contact is Contact.MIDDLE
    ]
    assert controlled_lofts, "600-ball session should contain controlled lofts"
    for ball in controlled_lofts:
        assert ball.outcome.value == "controlled_aerial"


def test_to_dict_round_trips_through_json() -> None:
    session = generate_session(seed=23, blocks=_standard_blocks())
    payload = json.loads(json.dumps(session.to_dict()))
    assert payload["seed"] == 23
    assert len(payload["balls"]) == 100
    assert payload["blocks"][0]["machine_speed_kph"] == 85.0
    assert payload["blocks"][1]["machine_speed_kph"] is None
