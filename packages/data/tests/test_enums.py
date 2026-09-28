"""Canonical vocabulary contract tests (US-B4/US-G1)."""

from cricai_data.enums import (
    DECISION_SHOTS,
    SHOT_TO_DECISION,
    AlertAudience,
    BlockIntent,
    BowlerSource,
    BowlingVariation,
    BraceState,
    CameraRole,
    Contact,
    DeliveryIntensity,
    EvidenceVerdict,
    FindingSeverity,
    Footwork,
    Handedness,
    Length,
    Line,
    MilestoneKind,
    Outcome,
    Provenance,
    ReportKind,
    ReportStatus,
    ReviewMode,
    SafetyCode,
    SessionType,
    Shot,
    StageStatus,
    decision_class,
)


def test_line_channels_match_backlog() -> None:
    assert {line.value for line in Line} == {"outside_off", "off", "middle", "leg"}


def test_length_bands_match_backlog() -> None:
    assert {length.value for length in Length} == {"yorker", "full", "good", "short"}


def test_footwork_matches_backlog() -> None:
    assert {f.value for f in Footwork} == {"front", "back", "leave"}


def test_contact_matches_backlog() -> None:
    assert {c.value for c in Contact} == {"middle", "edge", "miss"}


def test_decision_shots_are_exactly_eight() -> None:
    assert len(DECISION_SHOTS) == 8


def test_every_granular_shot_collapses_to_a_decision_class() -> None:
    for shot in Shot:
        assert decision_class(shot) in DECISION_SHOTS


def test_decision_shots_map_to_themselves() -> None:
    for shot in DECISION_SHOTS:
        assert decision_class(shot) is shot


def test_shot_to_decision_only_maps_non_decision_shots() -> None:
    assert set(SHOT_TO_DECISION).isdisjoint(DECISION_SHOTS)


def test_bowler_sources_match_backlog() -> None:
    assert {b.value for b in BowlerSource} == {"machine", "human", "coach"}


def test_block_intents_match_quality_split() -> None:
    assert {i.value for i in BlockIntent} == {
        "technical",
        "decision",
        "match_scenario",
        "spin_specific",
        "fun",
    }


def test_session_types() -> None:
    assert {s.value for s in SessionType} == {"batting", "bowling", "mixed"}


def test_handedness_supports_lh_guests() -> None:
    assert {h.value for h in Handedness} == {"right", "left"}


def test_provenance_values_match_g1() -> None:
    assert {p.value for p in Provenance} == {"manual", "auto", "proxy"}


def test_outcome_includes_g1_example_value() -> None:
    assert Outcome("controlled_ground_shot") is Outcome.CONTROLLED_GROUND_SHOT


def test_delivery_intensities_match_h1_ledger() -> None:
    assert {i.value for i in DeliveryIntensity} == {"spin", "pace_intent", "throwdown"}


def test_report_kinds_cover_g3_and_g5_cadences() -> None:
    assert {k.value for k in ReportKind} == {"daily", "weekly", "monthly"}


def test_report_statuses_include_the_safety_blocked_state() -> None:
    assert {s.value for s in ReportStatus} == {"draft", "published", "blocked"}


def test_stage_statuses_match_the_dag_lifecycle() -> None:
    assert {s.value for s in StageStatus} == {
        "pending",
        "running",
        "succeeded",
        "failed",
        "skipped",
    }


def test_alert_audiences_route_parents_and_developers() -> None:
    assert {a.value for a in AlertAudience} == {"parent", "developer"}


def test_evidence_verdicts_match_g6() -> None:
    assert {v.value for v in EvidenceVerdict} == {"confirms", "not_supported"}


def test_safety_codes_match_the_workload_policy() -> None:
    assert {c.value for c in SafetyCode} == {
        "workload_ceiling",
        "day_pattern_violation",
        "pain_flag",
    }


def test_finding_severities_match_the_ranking_ladder() -> None:
    assert {s.value for s in FindingSeverity} == {"info", "minor", "major"}


def test_camera_roles_match_i1() -> None:
    assert {r.value for r in CameraRole} == {
        "batting_side",
        "bowling_side",
        "wrist",
        "front_on",
        "other",
    }


def test_bowling_variations_match_the_legspin_vocabulary() -> None:
    assert {v.value for v in BowlingVariation} == {
        "leg_break",
        "top_spinner",
        "googly",
        "slider",
        "flipper",
        "unknown",
    }


def test_milestone_kinds_match_g5() -> None:
    assert {k.value for k in MilestoneKind} == {"personal_best", "volume", "streak"}


def test_brace_states_match_the_release_checkpoint() -> None:
    assert {b.value for b in BraceState} == {"braced", "bent", "collapsed"}


def test_review_modes_match_the_j5_gate() -> None:
    assert {m.value for m in ReviewMode} == {"auto_publish", "coach_gate"}
