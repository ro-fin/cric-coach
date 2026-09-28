"""US-J4/US-G5 nightly baseline job: machine-only means, idempotent frozen
upserts, per-zone context slices."""

import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from cricai_coaching.progress_agent import baseline_to_mapping, build_snapshots
from cricai_data.db import create_all, make_session_factory
from cricai_data.enums import (
    BowlerSource,
    Contact,
    Footwork,
    Length,
    Line,
    MetricPhase,
    Outcome,
    SessionType,
    Shot,
)
from cricai_data.models import BallMetrics, BallTag, MetricBaseline, Player
from cricai_data.models import Session as SessionRow
from cricai_data.storage import FsObjectStore
from cricai_worker.context import WorkerContext
from cricai_worker.nightly_baselines import (
    WINDOW_SESSION,
    ZONE_ALL,
    compute_nightly_baselines,
)
from sqlalchemy import create_engine, delete, select
from sqlalchemy.pool import StaticPool

DAY_ONE = date(2026, 6, 1)
DAY_TWO = date(2026, 6, 2)


@pytest.fixture
def ctx(tmp_path: Path) -> WorkerContext:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    create_all(engine)
    return WorkerContext(
        session_factory=make_session_factory(engine),
        store=FsObjectStore(tmp_path / "store"),
    )


def _entry(value: Any, *, source: str | None = None) -> dict[str, Any]:
    """One metric-value payload; ``source`` omitted means machine-written."""
    entry: dict[str, Any] = {"value": value, "unit": "ratio", "confidence": 0.9}
    if source is not None:
        entry["source"] = source
    return entry


def _add_player(ctx: WorkerContext, **kwargs: Any) -> uuid.UUID:
    with ctx.session_factory() as db:
        player = Player(name="Arjun", birthdate=date(2014, 11, 20), **kwargs)
        db.add(player)
        db.commit()
        return player.id


def _add_session(
    ctx: WorkerContext,
    player_id: uuid.UUID,
    session_date: date,
    *,
    calibration_suspect: bool = False,
) -> uuid.UUID:
    with ctx.session_factory() as db:
        row = SessionRow(
            player_id=player_id,
            session_date=session_date,
            session_type=SessionType.BATTING,
            bowler_source=BowlerSource.MACHINE,
            calibration_suspect=calibration_suspect,
        )
        db.add(row)
        db.commit()
        return row.id


def _add_metrics(
    ctx: WorkerContext, session_id: uuid.UUID, ball_no: int, metrics: dict[str, Any]
) -> None:
    with ctx.session_factory() as db:
        db.add(
            BallMetrics(
                session_id=session_id,
                ball_no=ball_no,
                phase=MetricPhase.CONTACT,
                metrics=metrics,
            )
        )
        db.commit()


def _add_tag(
    ctx: WorkerContext, session_id: uuid.UUID, ball_no: int, *, line: Line, length: Length
) -> None:
    with ctx.session_factory() as db:
        db.add(
            BallTag(
                session_id=session_id,
                ball_no=ball_no,
                line=line,
                length=length,
                shot=Shot.DRIVE,
                footwork=Footwork.FRONT,
                contact=Contact.MIDDLE,
                outcome=Outcome.CONTROLLED_GROUND_SHOT,
                control=True,
                created_by="coach",
            )
        )
        db.commit()


def _baselines(ctx: WorkerContext, player_id: uuid.UUID) -> list[MetricBaseline]:
    with ctx.session_factory() as db:
        return list(
            db.scalars(
                select(MetricBaseline)
                .where(MetricBaseline.player_id == player_id)
                .order_by(
                    MetricBaseline.snapshot_date, MetricBaseline.zone_key, MetricBaseline.metric
                )
            )
        )


class TestValueSelection:
    def test_machine_numeric_and_boolean_values_average(self, ctx: WorkerContext) -> None:
        player_id = _add_player(ctx)
        session_id = _add_session(ctx, player_id, DAY_ONE)
        _add_metrics(ctx, session_id, 1, {"control_pct": _entry(0.8), "in_control": _entry(True)})
        _add_metrics(ctx, session_id, 2, {"control_pct": _entry(0.6), "in_control": _entry(False)})

        summary = compute_nightly_baselines(ctx, player_id)
        assert summary.created == 2
        assert summary.updated == 0
        assert summary.values == 4

        rows = _baselines(ctx, player_id)
        by_metric = {row.metric: row for row in rows}
        assert by_metric["control_pct"].value == pytest.approx(0.7)
        assert by_metric["control_pct"].n == 2
        assert by_metric["control_pct"].zone_key == ZONE_ALL
        assert by_metric["control_pct"].window == WINDOW_SESSION
        assert by_metric["in_control"].value == pytest.approx(0.5)  # (1.0 + 0.0) / 2
        assert by_metric["control_pct"].payload == {
            "session_id": str(session_id),
            "session_ids": [str(session_id)],
        }

    def test_manual_and_non_numeric_and_non_dict_entries_are_skipped(
        self, ctx: WorkerContext
    ) -> None:
        player_id = _add_player(ctx)
        session_id = _add_session(ctx, player_id, DAY_ONE)
        _add_metrics(
            ctx,
            session_id,
            1,
            {
                "control_pct": _entry(0.9),  # machine numeric: kept
                "coach_score": _entry(4, source="manual"),  # manual: skipped
                "shot_class": _entry("drive"),  # class label: skipped
                "landing_xy": _entry([1, 2]),  # list value: skipped
                "null_metric": _entry(None),  # nullable-with-reason: skipped
                "corrupt": "not-a-dict",  # non-dict entry: skipped
            },
        )
        summary = compute_nightly_baselines(ctx, player_id)
        assert summary.values == 1
        rows = _baselines(ctx, player_id)
        assert [row.metric for row in rows] == ["control_pct"]
        assert rows[0].value == pytest.approx(0.9)


class TestIdempotenceAndFreezing:
    def test_rerun_updates_in_place_never_duplicates(self, ctx: WorkerContext) -> None:
        player_id = _add_player(ctx)
        session_id = _add_session(ctx, player_id, DAY_ONE)
        _add_metrics(ctx, session_id, 1, {"control_pct": _entry(0.5)})

        first = compute_nightly_baselines(ctx, player_id)
        assert (first.created, first.updated) == (1, 0)

        # A later ball lands; the re-run upserts the same slice, not a duplicate.
        _add_metrics(ctx, session_id, 2, {"control_pct": _entry(0.9)})
        second = compute_nightly_baselines(ctx, player_id)
        assert (second.created, second.updated) == (0, 1)

        rows = _baselines(ctx, player_id)
        assert len(rows) == 1
        assert rows[0].value == pytest.approx(0.7)  # recomputed mean of both balls
        assert rows[0].n == 2

    def test_as_of_bounds_the_sessions_considered(self, ctx: WorkerContext) -> None:
        player_id = _add_player(ctx)
        early = _add_session(ctx, player_id, DAY_ONE)
        late = _add_session(ctx, player_id, DAY_TWO)
        _add_metrics(ctx, early, 1, {"control_pct": _entry(0.5)})
        _add_metrics(ctx, late, 1, {"control_pct": _entry(0.9)})

        bounded = compute_nightly_baselines(ctx, player_id, as_of=DAY_ONE)
        assert bounded.created == 1
        assert [row.snapshot_date for row in _baselines(ctx, player_id)] == [DAY_ONE]

    def test_no_as_of_spans_every_session_date(self, ctx: WorkerContext) -> None:
        player_id = _add_player(ctx)
        for day, value in ((DAY_ONE, 0.5), (DAY_TWO, 0.9)):
            session_id = _add_session(ctx, player_id, day)
            _add_metrics(ctx, session_id, 1, {"control_pct": _entry(value)})

        summary = compute_nightly_baselines(ctx, player_id)
        assert summary.created == 2
        assert [row.snapshot_date for row in _baselines(ctx, player_id)] == [DAY_ONE, DAY_TWO]

    def test_two_sessions_one_date_merge_into_one_slice(self, ctx: WorkerContext) -> None:
        player_id = _add_player(ctx)
        morning = _add_session(ctx, player_id, DAY_ONE)
        evening = _add_session(ctx, player_id, DAY_ONE)
        _add_metrics(ctx, morning, 1, {"control_pct": _entry(0.4)})
        _add_metrics(ctx, evening, 1, {"control_pct": _entry(0.8)})

        compute_nightly_baselines(ctx, player_id)
        rows = _baselines(ctx, player_id)
        assert len(rows) == 1
        assert rows[0].value == pytest.approx(0.6)
        assert rows[0].n == 2
        assert rows[0].payload == {
            "session_id": None,  # two contributors: no single session to name
            "session_ids": sorted([str(morning), str(evening)]),
        }


class TestEligibilityReconvergence:
    """A re-run converges to the state a fresh run would produce (US-G5)."""

    def test_session_flagged_suspect_after_baselining_loses_its_slices(
        self, ctx: WorkerContext
    ) -> None:
        player_id = _add_player(ctx)
        day_one = _add_session(ctx, player_id, DAY_ONE)
        day_two = _add_session(ctx, player_id, DAY_TWO)
        _add_metrics(ctx, day_one, 1, {"control_pct": _entry(0.5)})
        _add_metrics(ctx, day_two, 1, {"control_pct": _entry(0.9)})
        compute_nightly_baselines(ctx, player_id)
        assert [row.snapshot_date for row in _baselines(ctx, player_id)] == [DAY_ONE, DAY_TWO]

        # The session is flagged AFTER its slice was frozen (US-C4 drift check).
        with ctx.session_factory() as db:
            session = db.get(SessionRow, day_one)
            assert session is not None
            session.calibration_suspect = True
            db.commit()

        summary = compute_nightly_baselines(ctx, player_id)
        assert summary.deleted == 1
        assert [row.snapshot_date for row in _baselines(ctx, player_id)] == [DAY_TWO]

        again = compute_nightly_baselines(ctx, player_id)  # idempotent re-run
        assert again.deleted == 0
        assert [row.snapshot_date for row in _baselines(ctx, player_id)] == [DAY_TWO]

    def test_metric_whose_machine_values_vanished_loses_its_slice(self, ctx: WorkerContext) -> None:
        player_id = _add_player(ctx)
        session_id = _add_session(ctx, player_id, DAY_ONE)
        _add_metrics(ctx, session_id, 1, {"control_pct": _entry(0.5), "bat_speed": _entry(20.0)})
        compute_nightly_baselines(ctx, player_id)
        assert [row.metric for row in _baselines(ctx, player_id)] == ["bat_speed", "control_pct"]

        # A re-derive re-attributes bat_speed to a human: no longer baseline-eligible.
        with ctx.session_factory() as db:
            metrics_row = db.scalar(select(BallMetrics).where(BallMetrics.session_id == session_id))
            assert metrics_row is not None
            metrics_row.metrics = {
                "control_pct": _entry(0.5),
                "bat_speed": _entry(20.0, source="manual"),
            }
            db.commit()

        summary = compute_nightly_baselines(ctx, player_id)
        assert summary.deleted == 1
        assert [row.metric for row in _baselines(ctx, player_id)] == ["control_pct"]

    def test_as_of_bounded_rerun_keeps_out_of_scope_slices(self, ctx: WorkerContext) -> None:
        """A bounded run must not delete slices dated after its own horizon."""
        player_id = _add_player(ctx)
        early = _add_session(ctx, player_id, DAY_ONE)
        late = _add_session(ctx, player_id, DAY_TWO)
        _add_metrics(ctx, early, 1, {"control_pct": _entry(0.5)})
        _add_metrics(ctx, late, 1, {"control_pct": _entry(0.9)})
        compute_nightly_baselines(ctx, player_id)

        bounded = compute_nightly_baselines(ctx, player_id, as_of=DAY_ONE)
        assert bounded.deleted == 0
        assert [row.snapshot_date for row in _baselines(ctx, player_id)] == [DAY_ONE, DAY_TWO]


class TestSessionLinkage:
    """Contract #7 seam: points carry ``session_id`` (fixed as finding rx4/3+39)."""

    def test_single_session_slice_carries_the_scalar_session_id(self, ctx: WorkerContext) -> None:
        player_id = _add_player(ctx)
        session_id = _add_session(ctx, player_id, DAY_ONE)
        _add_metrics(ctx, session_id, 1, {"control_pct": _entry(0.8)})
        compute_nightly_baselines(ctx, player_id)
        (row,) = _baselines(ctx, player_id)
        assert row.payload == {
            "session_id": str(session_id),
            "session_ids": [str(session_id)],
        }

    def test_progress_points_link_back_to_the_written_sessions(self, ctx: WorkerContext) -> None:
        """Writer -> stored row -> reader: the loop the fixtures used to fake."""
        player_id = _add_player(ctx)
        session_ids = []
        for offset, value in enumerate((0.5, 0.6, 0.7)):
            session_id = _add_session(ctx, player_id, DAY_ONE + timedelta(days=offset))
            _add_metrics(ctx, session_id, 1, {"control_pct": _entry(value)})
            session_ids.append(str(session_id))
        compute_nightly_baselines(ctx, player_id)
        rows = [baseline_to_mapping(row) for row in _baselines(ctx, player_id)]
        snapshot = build_snapshots(rows)[0]
        assert [point["session_id"] for point in snapshot["points"]] == session_ids


class TestZoneSlices:
    """US-G5 context normalization: tagged balls also feed line/length slices."""

    def test_tagged_balls_write_per_zone_slices_alongside_all(self, ctx: WorkerContext) -> None:
        player_id = _add_player(ctx)
        session_id = _add_session(ctx, player_id, DAY_ONE)
        _add_metrics(ctx, session_id, 1, {"control_pct": _entry(0.8)})
        _add_metrics(ctx, session_id, 2, {"control_pct": _entry(0.4)})
        _add_tag(ctx, session_id, 1, line=Line.OFF, length=Length.GOOD)
        _add_tag(ctx, session_id, 2, line=Line.LEG, length=Length.SHORT)

        summary = compute_nightly_baselines(ctx, player_id)
        assert summary.created == 3  # all + off/good + leg/short
        assert summary.values == 2  # underlying values counted once

        rows = _baselines(ctx, player_id)
        by_zone = {row.zone_key: row for row in rows}
        assert set(by_zone) == {ZONE_ALL, "off/good", "leg/short"}
        assert by_zone[ZONE_ALL].value == pytest.approx(0.6)
        assert by_zone[ZONE_ALL].n == 2
        assert by_zone["off/good"].value == pytest.approx(0.8)
        assert by_zone["off/good"].n == 1
        assert by_zone["leg/short"].value == pytest.approx(0.4)
        assert by_zone["leg/short"].n == 1
        assert all(row.window == WINDOW_SESSION for row in rows)

    def test_untagged_balls_feed_only_the_all_slice(self, ctx: WorkerContext) -> None:
        player_id = _add_player(ctx)
        session_id = _add_session(ctx, player_id, DAY_ONE)
        _add_metrics(ctx, session_id, 1, {"control_pct": _entry(0.8)})
        _add_tag(ctx, session_id, 1, line=Line.OFF, length=Length.GOOD)
        _add_metrics(ctx, session_id, 2, {"control_pct": _entry(0.4)})  # no tag

        compute_nightly_baselines(ctx, player_id)
        by_zone = {row.zone_key: row for row in _baselines(ctx, player_id)}
        assert by_zone[ZONE_ALL].n == 2
        assert by_zone["off/good"].n == 1
        assert by_zone["off/good"].value == pytest.approx(0.8)

    def test_zone_slice_carries_session_linkage_payload(self, ctx: WorkerContext) -> None:
        player_id = _add_player(ctx)
        session_id = _add_session(ctx, player_id, DAY_ONE)
        _add_metrics(ctx, session_id, 1, {"control_pct": _entry(0.8)})
        _add_tag(ctx, session_id, 1, line=Line.MIDDLE, length=Length.FULL)

        compute_nightly_baselines(ctx, player_id)
        by_zone = {row.zone_key: row for row in _baselines(ctx, player_id)}
        assert by_zone["middle/full"].payload == {
            "session_id": str(session_id),
            "session_ids": [str(session_id)],
        }

    def test_retagged_zone_loses_its_stale_slice(self, ctx: WorkerContext) -> None:
        """Zone re-labelling converges like every other eligibility change."""
        player_id = _add_player(ctx)
        session_id = _add_session(ctx, player_id, DAY_ONE)
        _add_metrics(ctx, session_id, 1, {"control_pct": _entry(0.8)})
        _add_tag(ctx, session_id, 1, line=Line.OFF, length=Length.GOOD)
        compute_nightly_baselines(ctx, player_id)
        assert {row.zone_key for row in _baselines(ctx, player_id)} == {ZONE_ALL, "off/good"}

        with ctx.session_factory() as db:  # the tag is re-labelled to another zone
            db.execute(delete(BallTag).where(BallTag.session_id == session_id))
            db.commit()
        _add_tag(ctx, session_id, 1, line=Line.LEG, length=Length.SHORT)

        summary = compute_nightly_baselines(ctx, player_id)
        assert summary.deleted == 1
        assert {row.zone_key for row in _baselines(ctx, player_id)} == {ZONE_ALL, "leg/short"}


class TestGuardrails:
    def test_unknown_player_raises(self, ctx: WorkerContext) -> None:
        with pytest.raises(ValueError, match="player not found"):
            compute_nightly_baselines(ctx, uuid.uuid4())

    def test_calibration_suspect_sessions_are_excluded(self, ctx: WorkerContext) -> None:
        player_id = _add_player(ctx)
        good = _add_session(ctx, player_id, DAY_ONE)
        suspect = _add_session(ctx, player_id, DAY_TWO, calibration_suspect=True)
        _add_metrics(ctx, good, 1, {"control_pct": _entry(0.5)})
        _add_metrics(ctx, suspect, 1, {"control_pct": _entry(0.9)})

        summary = compute_nightly_baselines(ctx, player_id)
        assert summary.created == 1
        assert [row.snapshot_date for row in _baselines(ctx, player_id)] == [DAY_ONE]

    def test_session_without_machine_metrics_writes_nothing(self, ctx: WorkerContext) -> None:
        player_id = _add_player(ctx)
        session_id = _add_session(ctx, player_id, DAY_ONE)
        _add_metrics(ctx, session_id, 1, {"coach_score": _entry(4, source="manual")})

        summary = compute_nightly_baselines(ctx, player_id)
        assert summary.created == 0
        assert summary.values == 0
        assert _baselines(ctx, player_id) == []
