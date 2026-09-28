"""Deterministic synthetic session generator (US-L2: every story testable without the lab).

Given the same seed and specs, output is byte-identical — golden fixtures and
regression snapshots depend on this. Zone biases let tests plant faults
(e.g., T5.1: low control on full outside-off) that downstream analysis must find.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from cricai_data.enums import (
    BlockIntent,
    BowlerSource,
    Contact,
    Footwork,
    Length,
    Line,
    Outcome,
    Shot,
)

#: Default machine speed when a block does not specify one (kph).
DEFAULT_MACHINE_SPEED_KPH = 85.0


@dataclass(frozen=True)
class ZoneBias:
    """Planted behavior for balls landing in (line, length): drives fault injection."""

    line: Line
    length: Length
    control_rate: float  # probability a ball in this zone is controlled
    weight: float = 1.0  # sampling weight boost for this zone


@dataclass(frozen=True)
class BlockSpec:
    n_balls: int
    bowler_source: BowlerSource = BowlerSource.MACHINE
    intent: BlockIntent = BlockIntent.TECHNICAL
    machine_speed_kph: float | None = None


@dataclass(frozen=True)
class SyntheticBall:
    ball_no: int
    block_no: int
    bowler_source: BowlerSource
    intent: BlockIntent
    speed_kph: float
    line: Line
    length: Length
    shot: Shot
    footwork: Footwork
    contact: Contact
    control: bool
    outcome: Outcome

    def to_dict(self) -> dict[str, object]:
        return {
            "ball_no": self.ball_no,
            "block_no": self.block_no,
            "bowler_source": self.bowler_source.value,
            "intent": self.intent.value,
            "speed_kph": self.speed_kph,
            "line": self.line.value,
            "length": self.length.value,
            "shot": self.shot.value,
            "footwork": self.footwork.value,
            "contact": self.contact.value,
            "control": self.control,
            "outcome": self.outcome.value,
        }


@dataclass(frozen=True)
class SyntheticSession:
    seed: int
    blocks: tuple[BlockSpec, ...]
    balls: tuple[SyntheticBall, ...] = field(default=())

    def to_dict(self) -> dict[str, object]:
        return {
            "seed": self.seed,
            "blocks": [
                {
                    "n_balls": b.n_balls,
                    "bowler_source": b.bowler_source.value,
                    "intent": b.intent.value,
                    "machine_speed_kph": b.machine_speed_kph,
                }
                for b in self.blocks
            ],
            "balls": [ball.to_dict() for ball in self.balls],
        }


_BASE_CONTROL_RATE = 0.78

_SHOT_BY_ZONE: dict[tuple[Line, Length], tuple[Shot, ...]] = {
    (Line.OUTSIDE_OFF, Length.FULL): (Shot.COVER_DRIVE, Shot.DRIVE, Shot.LEAVE),
    (Line.OUTSIDE_OFF, Length.GOOD): (Shot.LEAVE, Shot.DEFEND, Shot.CUT),
    (Line.OUTSIDE_OFF, Length.SHORT): (Shot.CUT, Shot.LEAVE),
    (Line.OUTSIDE_OFF, Length.YORKER): (Shot.DEFEND,),
    (Line.OFF, Length.FULL): (Shot.STRAIGHT_DRIVE, Shot.DRIVE, Shot.DEFEND),
    (Line.OFF, Length.GOOD): (Shot.DEFEND, Shot.DRIVE),
    (Line.OFF, Length.SHORT): (Shot.CUT, Shot.PULL, Shot.DEFEND),
    (Line.OFF, Length.YORKER): (Shot.DEFEND,),
    (Line.MIDDLE, Length.FULL): (Shot.ON_DRIVE, Shot.FLICK, Shot.DEFEND, Shot.LOFT),
    (Line.MIDDLE, Length.GOOD): (Shot.DEFEND, Shot.FLICK),
    (Line.MIDDLE, Length.SHORT): (Shot.PULL, Shot.HOOK, Shot.DEFEND),
    (Line.MIDDLE, Length.YORKER): (Shot.DEFEND,),
    (Line.LEG, Length.FULL): (Shot.FLICK, Shot.ON_DRIVE, Shot.SWEEP),
    (Line.LEG, Length.GOOD): (Shot.FLICK, Shot.DEFEND, Shot.SWEEP),
    (Line.LEG, Length.SHORT): (Shot.PULL, Shot.HOOK),
    (Line.LEG, Length.YORKER): (Shot.FLICK, Shot.DEFEND),
}

_FRONT_FOOT_LENGTHS = frozenset({Length.FULL, Length.YORKER})


def _footwork_for(shot: Shot, length: Length) -> Footwork:
    if shot is Shot.LEAVE:
        return Footwork.LEAVE
    if length in _FRONT_FOOT_LENGTHS:
        return Footwork.FRONT
    return Footwork.BACK


def _outcome_for(shot: Shot, contact: Contact, control: bool) -> Outcome:
    if shot is Shot.LEAVE:
        return Outcome.LEFT_ALONE
    if contact is Contact.MISS:
        return Outcome.BEATEN
    if contact is Contact.EDGE:
        return Outcome.EDGED
    if not control:
        return Outcome.UNCONTROLLED
    if shot is Shot.LOFT:
        return Outcome.CONTROLLED_AERIAL
    return Outcome.CONTROLLED_GROUND_SHOT


def generate_session(
    seed: int,
    blocks: tuple[BlockSpec, ...] | list[BlockSpec],
    zone_biases: tuple[ZoneBias, ...] | list[ZoneBias] = (),
) -> SyntheticSession:
    """Generate a deterministic synthetic session.

    Zone biases override the control rate for balls landing in their zone and
    (via ``weight``) make the machine "bowl at" that zone more often, so planted
    faults accumulate enough sample size for min-sample finding gates (US-G2).
    """
    rng = random.Random(seed)
    bias_by_zone = {(zb.line, zb.length): zb for zb in zone_biases}

    zones = list(_SHOT_BY_ZONE.keys())
    weights = [bias_by_zone[zone].weight if zone in bias_by_zone else 1.0 for zone in zones]

    balls: list[SyntheticBall] = []
    ball_no = 0
    for block_no, block in enumerate(blocks, start=1):
        speed = (
            block.machine_speed_kph
            if block.machine_speed_kph is not None
            else DEFAULT_MACHINE_SPEED_KPH
        )
        for _ in range(block.n_balls):
            ball_no += 1
            line, length = rng.choices(zones, weights=weights, k=1)[0]
            shot = rng.choice(_SHOT_BY_ZONE[(line, length)])
            footwork = _footwork_for(shot, length)

            bias = bias_by_zone.get((line, length))
            control_rate = bias.control_rate if bias is not None else _BASE_CONTROL_RATE
            if shot is Shot.LEAVE:
                contact, control = Contact.MISS, True  # a leave is a controlled decision
            else:
                control = rng.random() < control_rate
                if control:
                    contact = Contact.MIDDLE
                else:
                    contact = rng.choice((Contact.EDGE, Contact.MISS, Contact.MIDDLE))
            outcome = _outcome_for(shot, contact, control)

            jitter = (
                rng.uniform(-2.0, 2.0)
                if block.bowler_source is BowlerSource.MACHINE
                else (rng.uniform(-15.0, 10.0))
            )
            balls.append(
                SyntheticBall(
                    ball_no=ball_no,
                    block_no=block_no,
                    bowler_source=block.bowler_source,
                    intent=block.intent,
                    speed_kph=round(speed + jitter, 1),
                    line=line,
                    length=length,
                    shot=shot,
                    footwork=footwork,
                    contact=contact,
                    control=control,
                    outcome=outcome,
                )
            )

    return SyntheticSession(seed=seed, blocks=tuple(blocks), balls=tuple(balls))
