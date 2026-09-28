"""Pose foundation tests (US-E1): dataclasses, payload round-trip, deterministic fake."""

import pytest
from cricai_vision.pose import (
    LANDMARK_NAMES,
    N_LANDMARKS,
    FakePoseProvider,
    Landmark,
    PoseError,
    PoseFrame,
    PoseTrack,
    track_from_payload,
)


def _landmark(x: float = 0.0, y: float = 0.0, visibility: float = 1.0) -> Landmark:
    return Landmark(image_xy=(x, y), world_xyz=(x / 1000, y / 1000, 0.0), visibility=visibility)


def _frame(frame_no: int = 0, visibility: float = 1.0) -> PoseFrame:
    return PoseFrame(
        frame_no=frame_no,
        landmarks=tuple(_landmark(float(i), float(i), visibility) for i in range(N_LANDMARKS)),
    )


def _track(frames: tuple[PoseFrame, ...], fps: float = 120.0) -> PoseTrack:
    return PoseTrack(
        model_name="fake-pose",
        model_version="1",
        fps=fps,
        frames=frames,
        subject_confidence=0.9,
    )


def test_frame_requires_all_landmarks() -> None:
    with pytest.raises(PoseError, match="needs 33 landmarks"):
        PoseFrame(frame_no=0, landmarks=(_landmark(),))


def test_landmark_lookup_by_name_and_unknown_name() -> None:
    frame = _frame()
    nose = frame.landmark("nose")
    assert nose.image_xy == (float(LANDMARK_NAMES["nose"]), float(LANDMARK_NAMES["nose"]))
    with pytest.raises(PoseError, match="unknown landmark name"):
        frame.landmark("tail")


def test_track_validation() -> None:
    with pytest.raises(PoseError, match="fps must be positive"):
        _track((_frame(),), fps=0.0)
    with pytest.raises(PoseError, match="subject_confidence"):
        PoseTrack(
            model_name="m",
            model_version="1",
            fps=120.0,
            frames=(_frame(),),
            subject_confidence=1.5,
        )


def test_availability_counts_median_visible_frames() -> None:
    good = _frame(0, visibility=0.9)
    bad = _frame(1, visibility=0.1)
    track = _track((good, bad, _frame(2, visibility=0.9)))
    assert track.availability() == pytest.approx(2 / 3)
    assert track.frame_count == 3


def test_availability_empty_track_is_zero() -> None:
    assert _track(()).availability() == 0.0


def test_payload_round_trip() -> None:
    original = _track((_frame(0), _frame(1)))
    restored = track_from_payload(original.to_payload())
    assert restored == original


def test_payload_version_and_malformed_rejection() -> None:
    with pytest.raises(PoseError, match="unsupported pose payload version"):
        track_from_payload({"version": 2})
    payload = _track((_frame(),)).to_payload()
    del payload["frames"][0]["landmarks"][0]["visibility"]
    with pytest.raises(PoseError, match="malformed pose payload"):
        track_from_payload(payload)


def test_payload_non_numeric_fields_raise_pose_error() -> None:
    """A corrupted payload raises the documented PoseError, never a bare ValueError."""
    payload = _track((_frame(),)).to_payload()
    payload["frames"][0]["landmarks"][0]["visibility"] = "high"
    with pytest.raises(PoseError, match="malformed pose payload"):
        track_from_payload(payload)
    payload = _track((_frame(),)).to_payload()
    payload["fps"] = "thirty"
    with pytest.raises(PoseError, match="malformed pose payload"):
        track_from_payload(payload)
    payload = _track((_frame(),)).to_payload()
    payload["frames"][0]["frame_no"] = "first"
    with pytest.raises(PoseError, match="malformed pose payload"):
        track_from_payload(payload)


def test_fake_provider_is_deterministic_and_seeded() -> None:
    frames_in = [object()] * 8
    track_a = FakePoseProvider(seed=3).extract(frames_in, fps=120.0)
    track_b = FakePoseProvider(seed=3).extract(frames_in, fps=120.0)
    track_c = FakePoseProvider(seed=4).extract(frames_in, fps=120.0)
    assert track_a == track_b
    assert track_a != track_c
    assert track_a.frame_count == 8
    assert track_a.subject_confidence == pytest.approx(0.99)
    # The head sways while the frame count and topology stay fixed.
    noses = {frame.landmark("nose").image_xy for frame in track_a.frames}
    assert len(noses) > 1


def test_fake_provider_rejects_bad_fps() -> None:
    with pytest.raises(PoseError, match="fps must be positive"):
        FakePoseProvider().extract([object()], fps=-1.0)
