"""Pre-session health checks (US-A4): pure, parameterised pass/fail probes.

The capture agent measures raw stats (frame deltas, brightness, fps, disk,
clock offsets); this module turns them into named ``CheckResult`` verdicts so
a dead lens or full disk is caught *before* a 500-ball session, not after.
Every threshold is an explicit parameter with a documented default — nothing
is hidden inside the check bodies.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Mean 8-bit brightness below which a feed is considered dark (covered lens).
DEFAULT_DARK_MEAN_BRIGHTNESS = 5.0
#: Allowed shortfall of measured fps versus target, in percent.
DEFAULT_FPS_TOLERANCE_PCT = 5.0
#: Usable exposure band for 8-bit frames: below = under-exposed, above = blown out.
DEFAULT_EXPOSURE_LO = 20.0
DEFAULT_EXPOSURE_HI = 235.0
#: Free disk must cover this multiple of the expected session size (US-A4 AC).
DEFAULT_DISK_HEADROOM = 1.5
#: Cameras may drift at most this many frame-durations apart.
DEFAULT_SYNC_MAX_FRAMES = 1.0

#: 1 megabit per second = 125_000 bytes per second (1_000_000 bits / 8).
BYTES_PER_MEGABIT = 125_000


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one named probe; ``detail`` explains the verdict."""

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class FrameStats:
    """Feed liveness sample: brightness of the last frame + frames since last probe."""

    mean_brightness: float
    frames_delta: int


@dataclass(frozen=True)
class CameraStats:
    """Everything the capture agent measured for one camera."""

    camera_id: str
    frame_stats: FrameStats
    measured_fps: float


@dataclass(frozen=True)
class HealthCheckInputs:
    """Full measurement set for one health-check run."""

    cameras: tuple[CameraStats, ...]
    target_fps: float
    free_disk_bytes: int
    expected_session_bytes: int
    max_sync_offset_ms: float


@dataclass(frozen=True)
class Thresholds:
    """All tunable limits in one place; defaults mirror the module constants."""

    dark_mean_brightness: float = DEFAULT_DARK_MEAN_BRIGHTNESS
    fps_tolerance_pct: float = DEFAULT_FPS_TOLERANCE_PCT
    exposure_lo: float = DEFAULT_EXPOSURE_LO
    exposure_hi: float = DEFAULT_EXPOSURE_HI
    disk_headroom: float = DEFAULT_DISK_HEADROOM
    sync_max_frames: float = DEFAULT_SYNC_MAX_FRAMES


@dataclass(frozen=True)
class HealthCheckReport:
    """Aggregate verdict: ``passed`` iff every individual check passed."""

    passed: bool
    results: tuple[CheckResult, ...]


def check_feed(
    frame_stats: FrameStats,
    *,
    dark_mean: float = DEFAULT_DARK_MEAN_BRIGHTNESS,
    name: str = "feed",
) -> CheckResult:
    """Feed is alive: frames are arriving and the image is not pitch black."""
    if frame_stats.frames_delta == 0:
        return CheckResult(name, False, "feed frozen: no new frames since last probe")
    if frame_stats.mean_brightness < dark_mean:
        return CheckResult(
            name,
            False,
            f"feed dark: mean brightness {frame_stats.mean_brightness:g} < {dark_mean:g} "
            "(lens covered?)",
        )
    return CheckResult(name, True, f"feed alive: {frame_stats.frames_delta} new frames")


def check_fps(
    measured_fps: float,
    target_fps: float,
    *,
    tolerance_pct: float = DEFAULT_FPS_TOLERANCE_PCT,
    name: str = "fps",
) -> CheckResult:
    """Measured fps must not fall more than ``tolerance_pct`` below target."""
    floor = target_fps * (1.0 - tolerance_pct / 100.0)
    if measured_fps < floor:
        return CheckResult(
            name,
            False,
            f"fps throttled: measured {measured_fps:g} < floor {floor:g} "
            f"(target {target_fps:g} - {tolerance_pct:g}%)",
        )
    return CheckResult(name, True, f"fps ok: measured {measured_fps:g} of target {target_fps:g}")


def check_exposure(
    mean_brightness: float,
    *,
    lo: float = DEFAULT_EXPOSURE_LO,
    hi: float = DEFAULT_EXPOSURE_HI,
    name: str = "exposure",
) -> CheckResult:
    """Mean brightness must sit inside the usable ``[lo, hi]`` band."""
    if mean_brightness < lo:
        return CheckResult(
            name, False, f"under-exposed: mean brightness {mean_brightness:g} < {lo:g}"
        )
    if mean_brightness > hi:
        return CheckResult(
            name, False, f"over-exposed: mean brightness {mean_brightness:g} > {hi:g}"
        )
    return CheckResult(name, True, f"exposure ok: mean brightness {mean_brightness:g}")


def check_disk(
    free_bytes: int,
    expected_session_bytes: int,
    *,
    headroom: float = DEFAULT_DISK_HEADROOM,
    name: str = "disk",
) -> CheckResult:
    """Free disk must cover ``headroom`` times the expected session footprint."""
    required = expected_session_bytes * headroom
    if free_bytes < required:
        return CheckResult(
            name,
            False,
            f"disk full: free {free_bytes} B < required {required:g} B "
            f"({headroom:g}x expected {expected_session_bytes} B)",
        )
    return CheckResult(name, True, f"disk ok: free {free_bytes} B >= required {required:g} B")


def check_sync(
    max_offset_ms: float,
    fps: float,
    *,
    max_frames: float = DEFAULT_SYNC_MAX_FRAMES,
    name: str = "sync",
) -> CheckResult:
    """Worst inter-camera clock offset must stay within ``max_frames`` frame-durations."""
    if fps <= 0:
        raise ValueError("fps must be positive")
    budget_ms = max_frames * 1000.0 / fps
    if max_offset_ms > budget_ms:
        return CheckResult(
            name,
            False,
            f"clock desync: max offset {max_offset_ms:g} ms > budget {budget_ms:g} ms "
            f"({max_frames:g} frame(s) at {fps:g} fps)",
        )
    return CheckResult(
        name, True, f"sync ok: max offset {max_offset_ms:g} ms within {budget_ms:g} ms"
    )


def estimate_session_bytes(
    balls: int,
    cameras: int,
    bitrate_mbps: float,
    seconds_per_ball: float,
) -> int:
    """Expected recording footprint of a session, in bytes.

    Each ball yields ``seconds_per_ball`` seconds of footage on each of the
    ``cameras`` cameras, encoded at ``bitrate_mbps`` megabits per second:

        bytes = balls * cameras * seconds_per_ball * bitrate_mbps * 1_000_000 / 8
              = balls * cameras * seconds_per_ball * bitrate_mbps * 125_000
    """
    if balls < 0 or cameras < 0 or bitrate_mbps < 0 or seconds_per_ball < 0:
        raise ValueError("estimate_session_bytes arguments must be non-negative")
    return int(balls * cameras * seconds_per_ball * bitrate_mbps * BYTES_PER_MEGABIT)


def run_health_check(
    inputs: HealthCheckInputs,
    thresholds: Thresholds | None = None,
) -> HealthCheckReport:
    """Run every probe over the measured inputs and aggregate a pass/fail report.

    Per-camera checks are named ``feed:<camera_id>``, ``fps:<camera_id>`` and
    ``exposure:<camera_id>``; fleet-wide checks are named ``disk`` and ``sync``.
    """
    limits = thresholds if thresholds is not None else Thresholds()
    results: list[CheckResult] = []
    for camera in inputs.cameras:
        results.append(
            check_feed(
                camera.frame_stats,
                dark_mean=limits.dark_mean_brightness,
                name=f"feed:{camera.camera_id}",
            )
        )
        results.append(
            check_fps(
                camera.measured_fps,
                inputs.target_fps,
                tolerance_pct=limits.fps_tolerance_pct,
                name=f"fps:{camera.camera_id}",
            )
        )
        results.append(
            check_exposure(
                camera.frame_stats.mean_brightness,
                lo=limits.exposure_lo,
                hi=limits.exposure_hi,
                name=f"exposure:{camera.camera_id}",
            )
        )
    results.append(
        check_disk(
            inputs.free_disk_bytes,
            inputs.expected_session_bytes,
            headroom=limits.disk_headroom,
        )
    )
    results.append(
        check_sync(
            inputs.max_sync_offset_ms,
            inputs.target_fps,
            max_frames=limits.sync_max_frames,
        )
    )
    return HealthCheckReport(
        passed=all(result.passed for result in results),
        results=tuple(results),
    )
