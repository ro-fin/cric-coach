"""US-E1 batter-selection tests: crease-zone prior, continuity, never fabricate."""

import pytest
from cricai_vision.person_select import (
    CONTINUITY_KEEP,
    MIN_CONFIDENCE,
    Box,
    PersonCandidate,
    Selection,
    score_candidate,
    select_batter,
)

CREASE_ZONE: Box = (0.3, 0.2, 0.7, 0.95)

#: Batter at the crease: mostly inside the zone, decent size, clearly visible.
BATTER = PersonCandidate(bbox=(0.4, 0.45, 0.6, 0.9), mean_visibility=0.95, track_id=7)
#: Coach behind the net on the right edge, outside the zone.
COACH = PersonCandidate(bbox=(0.78, 0.35, 0.9, 0.75), mean_visibility=0.9, track_id=8)
#: Parent sitting in a far corner: tiny and outside the zone.
PARENT = PersonCandidate(bbox=(0.02, 0.05, 0.08, 0.18), mean_visibility=0.85, track_id=9)


def test_batter_wins_multi_person_layout() -> None:
    selection = select_batter([COACH, BATTER, PARENT], crease_zone=CREASE_ZONE)
    assert selection.candidate is BATTER
    assert selection.confidence is not None
    assert selection.confidence >= MIN_CONFIDENCE
    assert selection.confidence <= 1.0


def test_batter_wins_regardless_of_candidate_order() -> None:
    first = select_batter([BATTER, COACH, PARENT], crease_zone=CREASE_ZONE)
    second = select_batter([PARENT, COACH, BATTER], crease_zone=CREASE_ZONE)
    assert first.candidate is BATTER
    assert second.candidate is BATTER
    assert first.confidence == second.confidence


def test_track_id_continuity_keeps_batter_through_brief_zone_exit() -> None:
    # Batter stepped out of the crease zone (occlusion recovery): zero zone overlap,
    # but the track id matches the previous selection, so the track is kept.
    wandering = PersonCandidate(bbox=(0.72, 0.3, 0.92, 0.9), mean_visibility=0.6, track_id=7)
    selection = select_batter([wandering], crease_zone=CREASE_ZONE, previous=BATTER)
    assert selection.candidate is wandering
    assert selection.confidence is not None
    # Without the previous track the same candidate is implausible (no zone overlap).
    assert select_batter([wandering], crease_zone=CREASE_ZONE) == Selection(None, None)


def test_bbox_overlap_continuity_without_track_ids() -> None:
    previous = PersonCandidate(bbox=(0.72, 0.3, 0.92, 0.9), mean_visibility=0.9)
    current = PersonCandidate(bbox=(0.73, 0.3, 0.93, 0.9), mean_visibility=0.7)
    assert score_candidate(current, crease_zone=CREASE_ZONE, previous=previous) > score_candidate(
        current, crease_zone=CREASE_ZONE
    )
    selection = select_batter([current], crease_zone=CREASE_ZONE, previous=previous)
    assert selection.candidate is current


def test_continuity_below_keep_threshold_is_not_enough_outside_zone() -> None:
    previous = PersonCandidate(bbox=(0.72, 0.3, 0.92, 0.9), mean_visibility=0.9)
    barely_overlapping = PersonCandidate(bbox=(0.9, 0.85, 0.95, 0.92), mean_visibility=0.9)
    assert CONTINUITY_KEEP > 0.0
    selection = select_batter([barely_overlapping], crease_zone=CREASE_ZONE, previous=previous)
    assert selection == Selection(None, None)


def test_empty_candidates_yield_no_selection() -> None:
    assert select_batter([], crease_zone=CREASE_ZONE) == Selection(None, None)


def test_no_plausible_batter_yields_no_selection() -> None:
    selection = select_batter([COACH, PARENT], crease_zone=CREASE_ZONE)
    assert selection == Selection(None, None)


def test_zone_sliver_with_low_visibility_is_below_min_confidence() -> None:
    # Touches the zone (plausible location) but so small and dim the score
    # cannot clear MIN_CONFIDENCE: no selection rather than a fabricated batter.
    sliver = PersonCandidate(bbox=(0.68, 0.9, 0.7, 0.94), mean_visibility=0.3)
    assert 0.0 < score_candidate(sliver, crease_zone=CREASE_ZONE) < MIN_CONFIDENCE
    assert select_batter([sliver], crease_zone=CREASE_ZONE) == Selection(None, None)


def test_degenerate_bboxes_never_divide_by_zero() -> None:
    point = PersonCandidate(bbox=(0.5, 0.5, 0.5, 0.5), mean_visibility=1.0)
    assert score_candidate(point, crease_zone=CREASE_ZONE) == 0.0
    previous = PersonCandidate(bbox=(0.5, 0.5, 0.5, 0.5), mean_visibility=1.0)
    assert score_candidate(point, crease_zone=CREASE_ZONE, previous=previous) == 0.0
    assert select_batter([point], crease_zone=CREASE_ZONE) == Selection(None, None)


def test_visibility_is_clipped_to_unit_range() -> None:
    over = PersonCandidate(bbox=BATTER.bbox, mean_visibility=5.0)
    unit = PersonCandidate(bbox=BATTER.bbox, mean_visibility=1.0)
    assert score_candidate(over, crease_zone=CREASE_ZONE) == score_candidate(
        unit, crease_zone=CREASE_ZONE
    )
    negative = PersonCandidate(bbox=BATTER.bbox, mean_visibility=-1.0)
    assert score_candidate(negative, crease_zone=CREASE_ZONE) == 0.0


@pytest.mark.parametrize("zone", [(0.7, 0.2, 0.3, 0.95), (0.3, 0.95, 0.7, 0.2)])
def test_invalid_crease_zone_raises(zone: Box) -> None:
    with pytest.raises(ValueError, match="crease_zone"):
        select_batter([BATTER], crease_zone=zone)
    with pytest.raises(ValueError, match="crease_zone"):
        score_candidate(BATTER, crease_zone=zone)
