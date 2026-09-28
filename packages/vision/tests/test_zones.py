"""US-C5: config-driven zone classification, boundary rules, LH mirroring."""

import pytest
from cricai_data.enums import Handedness, Length, Line
from cricai_vision.zones import (
    DEFAULT_LENGTH_BANDS_M,
    DEFAULT_LINE_CHANNELS_M,
    ZoneConfig,
    ZoneConfigError,
    classify,
    classify_length,
    classify_line,
)


@pytest.fixture
def config() -> ZoneConfig:
    return ZoneConfig()


def test_length_bands_cover_all_lengths(config: ZoneConfig) -> None:
    assert set(config.length_bands_m) == set(Length)
    assert set(config.line_channels_m) == set(Line)


@pytest.mark.parametrize(
    ("x", "expected"),
    [
        (0.0, Length.YORKER),
        (1.99, Length.YORKER),
        (2.0, Length.FULL),  # edge belongs to the farther band (documented rule)
        (4.999, Length.FULL),
        (5.0, Length.GOOD),
        (8.0, Length.SHORT),
        (19.0, Length.SHORT),
    ],
)
def test_length_boundaries(config: ZoneConfig, x: float, expected: Length) -> None:
    assert classify_length(config, x) is expected


@pytest.mark.parametrize(
    ("y", "expected"),
    [
        (-1.0, Line.LEG),
        (-0.1143, Line.MIDDLE),  # edge → more off-side channel
        (0.0, Line.MIDDLE),
        (0.1143, Line.OFF),
        (0.39, Line.OFF),
        (0.40, Line.OUTSIDE_OFF),
        (1.2, Line.OUTSIDE_OFF),
    ],
)
def test_line_boundaries_rh(config: ZoneConfig, y: float, expected: Line) -> None:
    assert classify_line(config, y, Handedness.RIGHT) is expected


def test_lh_batter_mirrors_line_channels(config: ZoneConfig) -> None:
    """US-C5: left-hand batter mode mirrors line channels correctly."""
    for y in (-1.2, -0.3, -0.05, 0.05, 0.3, 1.2):
        rh = classify_line(config, y, Handedness.RIGHT)
        lh = classify_line(config, -y, Handedness.LEFT)
        assert rh is lh


def test_classify_returns_both(config: ZoneConfig) -> None:
    line, length = classify(config, 3.0, 0.5, Handedness.RIGHT)
    assert (line, length) == (Line.OUTSIDE_OFF, Length.FULL)


def test_invalid_inputs_rejected(config: ZoneConfig) -> None:
    with pytest.raises(ZoneConfigError, match="off the pitch"):
        classify_length(config, -0.1)  # beyond the 0.05 m clamp epsilon
    with pytest.raises(ZoneConfigError, match="invalid bounce"):
        classify_length(config, float("nan"))
    with pytest.raises(ZoneConfigError, match="invalid lateral"):
        classify_line(config, float("inf"), Handedness.RIGHT)


# ---------------------------------------------------------------------------
# Clamp + off-pitch validation (US-C5, REVIEW FIX)
# ---------------------------------------------------------------------------


def test_yorker_click_within_epsilon_clamps_to_yorker(config: ZoneConfig) -> None:
    """A legitimate yorker click mapping to x=-0.01 m (within US-C2's accepted
    calibration error) classifies as YORKER instead of being rejected."""
    assert classify_length(config, -0.01) is Length.YORKER
    assert classify_length(config, -0.05) is Length.YORKER  # epsilon edge included


def test_beyond_epsilon_off_striker_end_is_rejected(config: ZoneConfig) -> None:
    with pytest.raises(ZoneConfigError, match="off the pitch"):
        classify_length(config, -0.06)


def test_past_bowler_stumps_within_epsilon_clamps_to_short(config: ZoneConfig) -> None:
    assert classify_length(config, 20.13) is Length.SHORT  # clamps to 20.12
    assert classify_length(config, 20.17) is Length.SHORT  # epsilon edge included


def test_beyond_epsilon_past_bowler_end_is_rejected(config: ZoneConfig) -> None:
    with pytest.raises(ZoneConfigError, match="off the pitch"):
        classify_length(config, 20.20)
    with pytest.raises(ZoneConfigError, match="off the pitch"):
        classify_length(config, 25.0)  # previously (bug) accepted as SHORT


def test_implausibly_wide_offsets_rejected(config: ZoneConfig) -> None:
    for y in (3.01, -3.01):
        with pytest.raises(ZoneConfigError, match="implausibly wide"):
            classify_line(config, y, Handedness.RIGHT)
        with pytest.raises(ZoneConfigError, match="implausibly wide"):
            classify_line(config, y, Handedness.LEFT)
    with pytest.raises(ZoneConfigError, match="implausibly wide"):
        classify_line(config, 5.0, Handedness.RIGHT)  # previously (bug) OUTSIDE_OFF


def test_wide_but_plausible_offsets_still_classify(config: ZoneConfig) -> None:
    assert classify_line(config, 2.9, Handedness.RIGHT) is Line.OUTSIDE_OFF
    assert classify_line(config, 3.0, Handedness.RIGHT) is Line.OUTSIDE_OFF  # limit inclusive
    assert classify_line(config, -2.9, Handedness.LEFT) is Line.OUTSIDE_OFF  # LH mirror intact


def test_classify_applies_clamp_and_wide_checks(config: ZoneConfig) -> None:
    assert classify(config, -0.01, 0.0, Handedness.RIGHT) == (Line.MIDDLE, Length.YORKER)
    with pytest.raises(ZoneConfigError, match="off the pitch"):
        classify(config, 20.20, 0.0, Handedness.RIGHT)
    with pytest.raises(ZoneConfigError, match="implausibly wide"):
        classify(config, 3.0, 3.01, Handedness.RIGHT)


def test_clamp_fields_round_trip_through_dict() -> None:
    config = ZoneConfig(clamp_epsilon_m=0.03, pitch_length_m=18.0, wide_limit_m=2.5)
    raw = config.to_dict()
    assert raw["clamp_epsilon_m"] == 0.03
    assert raw["pitch_length_m"] == 18.0
    assert raw["wide_limit_m"] == 2.5
    assert ZoneConfig.from_dict(raw) == config
    # Junior pitch: the clamp window follows the configured pitch length.
    assert classify_length(ZoneConfig.from_dict(raw), 18.02) is Length.SHORT
    with pytest.raises(ZoneConfigError, match="off the pitch"):
        classify_length(ZoneConfig.from_dict(raw), 18.04)


def test_from_dict_defaults_clamp_fields_when_absent() -> None:
    """Stored configs predating the clamp fields keep loading with defaults."""
    raw = ZoneConfig().to_dict()
    del raw["clamp_epsilon_m"]
    del raw["pitch_length_m"]
    del raw["wide_limit_m"]
    config = ZoneConfig.from_dict(raw)
    assert config == ZoneConfig()
    assert config.clamp_epsilon_m == 0.05
    assert config.pitch_length_m == 20.12
    assert config.wide_limit_m == 3.0


def test_from_dict_rejects_malformed_clamp_fields() -> None:
    raw = ZoneConfig().to_dict()
    raw["clamp_epsilon_m"] = "wide"
    with pytest.raises(ZoneConfigError, match="malformed"):
        ZoneConfig.from_dict(raw)


def test_clamp_field_validation() -> None:
    with pytest.raises(ZoneConfigError, match="clamp_epsilon_m"):
        ZoneConfig(clamp_epsilon_m=-0.01)
    with pytest.raises(ZoneConfigError, match="clamp_epsilon_m"):
        ZoneConfig(clamp_epsilon_m=float("nan"))
    with pytest.raises(ZoneConfigError, match="pitch_length_m"):
        ZoneConfig(pitch_length_m=0.0)
    with pytest.raises(ZoneConfigError, match="wide_limit_m"):
        ZoneConfig(wide_limit_m=float("inf"))
    # Zero epsilon is allowed: no slack, strict [0, pitch_length_m].
    strict = ZoneConfig(clamp_epsilon_m=0.0)
    with pytest.raises(ZoneConfigError, match="off the pitch"):
        classify_length(strict, -0.001)


def test_config_round_trip_and_reclassification() -> None:
    config = ZoneConfig()
    raw = config.to_dict()
    restored = ZoneConfig.from_dict(raw)
    assert restored == config

    # Changing boundaries re-derives classes without re-clicking (US-C5)
    tweaked_raw = restored.to_dict()
    tweaked_raw["length_bands_m"]["yorker"] = [0.0, 2.5]
    tweaked_raw["length_bands_m"]["full"] = [2.5, 5.0]
    tweaked = ZoneConfig.from_dict(tweaked_raw)
    assert classify_length(tweaked, 2.2) is Length.YORKER
    assert classify_length(ZoneConfig(), 2.2) is Length.FULL


def test_config_validation_rejects_gaps_overlaps_and_missing() -> None:
    bands = dict(DEFAULT_LENGTH_BANDS_M)
    bands[Length.FULL] = (2.5, 5.0)  # gap 2.0-2.5
    with pytest.raises(ZoneConfigError, match="contiguous"):
        ZoneConfig(length_bands_m=bands)

    bands = dict(DEFAULT_LENGTH_BANDS_M)
    del bands[Length.GOOD]
    with pytest.raises(ZoneConfigError, match="exactly"):
        ZoneConfig(length_bands_m=bands)

    bands = dict(DEFAULT_LENGTH_BANDS_M)
    bands[Length.GOOD] = (8.0, 5.0)  # empty band
    with pytest.raises(ZoneConfigError, match="empty"):
        ZoneConfig(length_bands_m=bands)

    channels = dict(DEFAULT_LINE_CHANNELS_M)
    channels[Line.OUTSIDE_OFF] = (0.40, 5.0)  # must extend to infinity
    with pytest.raises(ZoneConfigError, match="infinity"):
        ZoneConfig(line_channels_m=channels)


def test_from_dict_rejects_malformed() -> None:
    with pytest.raises(ZoneConfigError, match="malformed"):
        ZoneConfig.from_dict({"length_bands_m": {"yorker": [0.0]}})
    with pytest.raises(ZoneConfigError, match="malformed"):
        ZoneConfig.from_dict({})
    with pytest.raises(ZoneConfigError, match="malformed"):
        ZoneConfig.from_dict({"length_bands_m": {"nope": [0, 1]}, "line_channels_m": {}})
