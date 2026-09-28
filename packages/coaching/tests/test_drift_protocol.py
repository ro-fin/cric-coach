"""US-L4 drift protocol: auto-vs-manual agreement, threshold alerts, the
mislabeled-batch canary alarm (MV), and the stratified weekly sampling job."""

import datetime

import pytest
from cricai_coaching.drift import (
    CANARY_AGREEMENT_BOUND,
    CANARY_BLOCKS,
    CANARY_SEED,
    DEFAULT_AGREEMENT_THRESHOLDS,
    GROUND_TRUTH_FIELDS,
    MIN_COMPARED_PER_FIELD,
    WEEKLY_SAMPLE_SIZE,
    FieldAgreement,
    SampleCandidate,
    canary_alerts,
    canary_expectations,
    drift_alerts,
    field_agreement,
    paired_ball_count,
    sample_shortfall_alert,
    select_weekly_sample,
)

WEEK = datetime.date(2026, 7, 6)


def _balls(*rows: tuple[int, str, str]) -> dict[int, dict[str, object]]:
    """ball_no -> {line, length} shorthand for two-field comparisons."""
    return {ball_no: {"line": line, "length": length} for ball_no, line, length in rows}


class TestFieldAgreement:
    def test_agreement_counts_matches_and_mismatches(self) -> None:
        manual = _balls((1, "off", "good"), (2, "leg", "full"))
        auto = _balls((1, "off", "good"), (2, "off", "full"))
        agreements = field_agreement(manual, auto, fields=("line", "length"))
        assert agreements["line"] == FieldAgreement(field="line", matched=1, compared=2)
        assert agreements["length"] == FieldAgreement(field="length", matched=2, compared=2)

    def test_balls_missing_on_either_side_are_not_compared(self) -> None:
        manual = _balls((1, "off", "good"), (2, "leg", "full"))
        auto = _balls((2, "leg", "full"), (3, "off", "good"))
        agreements = field_agreement(manual, auto, fields=("line",))
        assert agreements["line"].compared == 1
        assert paired_ball_count(manual, auto) == 1

    def test_none_values_are_not_disagreements(self) -> None:
        manual = {1: {"line": "off", "length": None}}
        auto = {1: {"line": None, "length": "good"}}
        agreements = field_agreement(manual, auto, fields=("line", "length"))
        assert agreements["line"].compared == 0
        assert agreements["length"].compared == 0

    def test_absent_field_keys_are_not_compared(self) -> None:
        agreements = field_agreement({1: {"line": "off"}}, {1: {"length": "good"}})
        assert all(a.compared == 0 for a in agreements.values())
        assert set(agreements) == set(GROUND_TRUTH_FIELDS)

    def test_pct(self) -> None:
        assert FieldAgreement("line", 0, 0).pct is None
        assert FieldAgreement("line", 3, 4).pct == pytest.approx(0.75)


class TestDriftAlerts:
    def _agree(self, field: str, matched: int, compared: int) -> dict[str, FieldAgreement]:
        return {field: FieldAgreement(field=field, matched=matched, compared=compared)}

    def test_agreement_below_threshold_fires_developer_alert(self) -> None:
        alerts = drift_alerts(self._agree("line", 6, 10))
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert["audience"] == "developer"
        assert alert["code"] == "drift_agreement"
        assert alert["severity"] == "warning"
        assert alert["detail"] == {
            "field": "line",
            "agreement_pct": 0.6,
            "threshold": DEFAULT_AGREEMENT_THRESHOLDS["line"],
            "matched": 6,
            "compared": 10,
        }

    def test_agreement_at_threshold_stays_silent(self) -> None:
        assert drift_alerts(self._agree("line", 9, 10)) == []

    def test_thin_evidence_never_fires(self) -> None:
        assert drift_alerts(self._agree("line", 0, MIN_COMPARED_PER_FIELD - 1)) == []

    def test_zero_compared_stays_silent_even_without_min_gate(self) -> None:
        assert drift_alerts(self._agree("line", 0, 0), min_compared=0) == []

    def test_threshold_overrides_and_unknown_field_default(self) -> None:
        agreements = {
            **self._agree("line", 9, 10),
            **self._agree("custom_field", 7, 10),
        }
        alerts = drift_alerts(agreements, thresholds={"line": 0.95})
        assert [a["detail"]["field"] for a in alerts] == ["custom_field", "line"]
        assert alerts[1]["detail"]["threshold"] == 0.95
        assert alerts[0]["detail"]["threshold"] == 0.80  # unknown field default


class TestSampleShortfall:
    def test_full_sample_is_silent(self) -> None:
        assert sample_shortfall_alert(WEEKLY_SAMPLE_SIZE) is None

    def test_short_sample_alerts_developer(self) -> None:
        alert = sample_shortfall_alert(3)
        assert alert is not None
        assert alert["code"] == "drift_sample_short"
        assert alert["audience"] == "developer"
        assert alert["detail"] == {"expected": WEEKLY_SAMPLE_SIZE, "paired": 3}


class TestCanary:
    def test_expectations_are_deterministic_and_complete(self) -> None:
        first = canary_expectations()
        assert first == canary_expectations()
        assert len(first) == sum(block.n_balls for block in CANARY_BLOCKS)
        assert set(first[1]) == set(GROUND_TRUTH_FIELDS)

    def test_healthy_pipeline_passes(self) -> None:
        assert canary_alerts(canary_expectations()) == []

    def test_mislabeled_batch_fires_critical_alarm(self) -> None:
        """MV acceptance: a deliberately mislabeled batch trips the canary."""
        observed = canary_expectations()
        for ball_no in (1, 2, 3):
            observed[ball_no] = {**observed[ball_no], "line": "mislabeled", "shot": "mislabeled"}
        alerts = canary_alerts(observed)
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert["code"] == "drift_canary"
        assert alert["severity"] == "critical"
        assert alert["audience"] == "developer"
        assert alert["detail"]["agreement_pct"] < CANARY_AGREEMENT_BOUND
        assert alert["detail"]["bound"] == CANARY_AGREEMENT_BOUND
        assert alert["detail"]["per_field"]["line"] < 1.0
        assert alert["detail"]["per_field"]["length"] == 1.0

    def test_tiny_disagreement_within_bound_passes(self) -> None:
        observed = canary_expectations()
        observed[1] = {**observed[1], "line": "mislabeled"}  # 1 of 210 field pairs
        assert canary_alerts(observed) == []

    def test_silent_pipeline_fires_missing_alarm(self) -> None:
        alerts = canary_alerts({})
        assert len(alerts) == 1
        assert alerts[0]["code"] == "drift_canary_missing"
        assert alerts[0]["severity"] == "critical"
        assert alerts[0]["detail"]["expected_balls"] == len(canary_expectations())

    def test_canary_constants_are_frozen(self) -> None:
        # Changing these invalidates every recorded expectation — the values
        # are part of the drift protocol, not tuning knobs.
        assert CANARY_SEED == 424242
        assert [block.n_balls for block in CANARY_BLOCKS] == [15, 15]


def _candidates(count: int, stratum: str, session_id: str = "s1") -> list[SampleCandidate]:
    return [
        SampleCandidate(session_id=session_id, ball_no=n + 1, stratum=stratum) for n in range(count)
    ]


class TestWeeklySample:
    def test_selects_the_protocol_size(self) -> None:
        sample = select_weekly_sample(_candidates(30, "a"), WEEK)
        assert len(sample) == WEEKLY_SAMPLE_SIZE
        assert len(set(sample)) == WEEKLY_SAMPLE_SIZE

    def test_deterministic_and_input_order_independent(self) -> None:
        candidates = _candidates(15, "a") + _candidates(15, "b", "s2")
        sample = select_weekly_sample(candidates, WEEK)
        assert sample == select_weekly_sample(list(reversed(candidates)), WEEK)

    def test_different_weeks_pick_different_balls(self) -> None:
        candidates = _candidates(40, "a")
        other_week = WEEK + datetime.timedelta(days=7)
        assert select_weekly_sample(candidates, WEEK) != select_weekly_sample(
            candidates, other_week
        )

    def test_round_robin_across_strata(self) -> None:
        candidates = _candidates(20, "a") + _candidates(20, "b", "s2")
        sample = select_weekly_sample(candidates, WEEK)
        by_stratum = {s: sum(1 for c in sample if c.stratum == s) for s in ("a", "b")}
        assert by_stratum == {"a": 5, "b": 5}

    def test_exhausted_stratum_yields_to_others(self) -> None:
        candidates = _candidates(1, "a") + _candidates(20, "b", "s2")
        sample = select_weekly_sample(candidates, WEEK, size=4)
        by_stratum = {s: sum(1 for c in sample if c.stratum == s) for s in ("a", "b")}
        assert by_stratum == {"a": 1, "b": 3}

    def test_fewer_candidates_than_size_returns_all(self) -> None:
        candidates = _candidates(3, "a")
        assert set(select_weekly_sample(candidates, WEEK)) == set(candidates)

    def test_no_candidates(self) -> None:
        assert select_weekly_sample([], WEEK) == ()

    def test_mid_round_stop_at_exact_size(self) -> None:
        candidates = _candidates(2, "a") + _candidates(2, "b", "s2")
        assert len(select_weekly_sample(candidates, WEEK, size=3)) == 3
