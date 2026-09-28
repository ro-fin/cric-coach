"""US-E4 V1 ball-flight & decision schema unification over manual sources.

V1 populates ``speed_kph, line, length, bounce_xy, footwork, shot`` (plus the 8-way
``decision_class``) from manual sources -- US-B4 tags, US-C5 bounce marks, machine
settings -- and Epic F later swaps in auto values under the SAME keys. Every present
value carries ``source`` provenance so the schema is identical either way (US-E4 AC);
missing values are null-with-reason, never silent zeros.

:func:`decision_quality` is the pure aggregation behind the US-E4 decision-quality
view: leaves vs chases per length zone for balls outside off stump.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from cricai_data.enums import Length, Line, Shot, decision_class
from cricai_data.models import BallTag, BounceMark

from cricai_coaching.contact_metrics import MetricValue

#: V1 provenance: every populated flight value comes from a human/manual source.
MANUAL_SOURCE = "manual"

#: Every key :func:`assemble_flight_metrics` emits, in stable output order.
FLIGHT_METRIC_KEYS: tuple[str, ...] = (
    "speed_kph",
    "line",
    "length",
    "bounce_xy",
    "footwork",
    "shot",
    "decision_class",
)


def _zone_class(
    name: str,
    tag_value: StrEnum | None,
    bounce_value: StrEnum | None,
    bounce: BounceMark | None,
) -> MetricValue:
    """line/length: prefer the tag's class, else the bounce mark's stored class."""
    if tag_value is not None:
        return MetricValue(tag_value.value, "class", 1.0, source=MANUAL_SOURCE)
    if bounce_value is not None:
        return MetricValue(bounce_value.value, "class", 1.0, source=MANUAL_SOURCE)
    if bounce is not None:
        return MetricValue(None, "class", 0.0, reason=f"bounce mark has no derived {name} class")
    return MetricValue(None, "class", 0.0, reason=f"no tag or bounce mark to provide {name}")


def _tag_class(value: StrEnum | None, description: str) -> MetricValue:
    if value is None:
        return MetricValue(None, "class", 0.0, reason=f"no tag to provide {description}")
    return MetricValue(value.value, "class", 1.0, source=MANUAL_SOURCE)


def assemble_flight_metrics(
    tag: BallTag | None,
    bounce: BounceMark | None,
    machine_speed_kph: float | None,
) -> dict[str, MetricValue]:
    """Unify one ball's manual flight/decision context under the metric contract.

    - ``speed_kph``: the bowling-machine set speed (confidence 1.0, source manual);
      null-with-reason when no machine speed applies.
    - ``line``/``length``: the tag's classes when tagged, else the bounce mark's
      stored zone classes (both manual provenance).
    - ``bounce_xy``: the bounce mark's pitch-plane coordinates in meters.
    - ``footwork``/``shot``: from the tag; ``shot`` keeps the granular label and
      ``decision_class`` exposes its 8-way class via ``cricai_data.enums``.
    """
    if machine_speed_kph is None:
        speed = MetricValue(
            None, "kph", 0.0, reason="no machine speed setting recorded for this ball"
        )
    else:
        speed = MetricValue(float(machine_speed_kph), "kph", 1.0, source=MANUAL_SOURCE)
    if bounce is None:
        bounce_xy = MetricValue(None, "m", 0.0, reason="no bounce mark for this ball")
    else:
        bounce_xy = MetricValue([bounce.pitch_x, bounce.pitch_y], "m", 1.0, source=MANUAL_SOURCE)
    return {
        "speed_kph": speed,
        "line": _zone_class(
            "line",
            tag.line if tag is not None else None,
            bounce.line if bounce is not None else None,
            bounce,
        ),
        "length": _zone_class(
            "length",
            tag.length if tag is not None else None,
            bounce.length if bounce is not None else None,
            bounce,
        ),
        "bounce_xy": bounce_xy,
        "footwork": _tag_class(tag.footwork if tag is not None else None, "footwork"),
        "shot": _tag_class(tag.shot if tag is not None else None, "shot"),
        "decision_class": _tag_class(
            decision_class(tag.shot) if tag is not None else None, "decision class"
        ),
    }


@dataclass(frozen=True)
class BallContext:
    """One ball's joined decision context (tag- and/or bounce-derived), DB-free."""

    ball_no: int
    line: Line | None
    length: Length | None
    shot: Shot | None


@dataclass(frozen=True)
class ZoneDecision:
    """Leave/chase counts for one length zone of the outside-off channel."""

    length: Length
    leaves: int
    chases: int

    @property
    def balls(self) -> int:
        """The reported denominator for this zone (US-E3/E4)."""
        return self.leaves + self.chases


@dataclass(frozen=True)
class DecisionQuality:
    """US-E4 decision-quality view: leaves vs chases for balls outside off stump."""

    zones: tuple[ZoneDecision, ...]
    outside_off_balls: int
    leaves: int
    chases: int
    unclassified: int  # outside-off balls missing shot or length context


def decision_quality(balls: Iterable[BallContext]) -> DecisionQuality:
    """Pure aggregation: per-length-zone leave/chase counts for balls outside off.

    A ball counts as a leave when its 8-way decision class is ``leave``; anything
    played at (defend/drive/cut/pull/sweep/flick/loft) is a chase of a ball that
    could be left. Balls whose line is unknown or not outside off are ignored;
    outside-off balls missing shot or length context are reported ``unclassified``
    instead of silently dropped (US-E3: the denominator is reported).
    """
    per_zone: dict[Length, list[int]] = {}
    leaves = chases = unclassified = total = 0
    for ball in balls:
        if ball.line is not Line.OUTSIDE_OFF:
            continue
        total += 1
        if ball.shot is None or ball.length is None:
            unclassified += 1
            continue
        counts = per_zone.setdefault(ball.length, [0, 0])
        if decision_class(ball.shot) is Shot.LEAVE:
            counts[0] += 1
            leaves += 1
        else:
            counts[1] += 1
            chases += 1
    zones = tuple(
        ZoneDecision(length=length, leaves=per_zone[length][0], chases=per_zone[length][1])
        for length in Length
        if length in per_zone
    )
    return DecisionQuality(
        zones=zones,
        outside_off_balls=total,
        leaves=leaves,
        chases=chases,
        unclassified=unclassified,
    )
