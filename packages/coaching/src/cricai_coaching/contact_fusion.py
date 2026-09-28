"""US-F5 contact-quality & bat-path fusion over three independent modalities.

Fuses (1) ball-track deviation at the bat plane (the pinned US-F3 track payload:
post-contact segment presence + direction-change magnitude), (2) the audio onset
class from ``cricai_vision.audio_onset`` (its :class:`RefinedContact` result
shape is consumed as-is — no DSP is reimplemented here), and (3) bat-detection
overlap near the ball at the contact frame, into one ``contact_quality`` class
over :class:`cricai_data.enums.Contact` (middle | edge | miss) plus a
``bat_path`` class, each with calibrated-intent confidence.

Fusion math (documented in ``docs/coaching_metrics.md`` US-F5 section):

- each modality produces a :class:`ModalitySignal` — a score distribution over
  the three Contact classes (sums to 1) and a modality confidence in [0, 1];
- base weights (``FusionConfig``: track 0.5, audio 0.3, bat 0.2) are
  RENORMALIZED over the modalities actually present, so any single modality
  (or any pair) still yields a class;
- ``combined[c] = sum_i (w_i / present_w) * scores_i[c]``; the class is the
  argmax with ties resolved toward EDGE first (edge recall matters most,
  US-F5), then MIDDLE, then MISS;
- ``confidence = combined[top] * coverage * weighted_modality_confidence``
  where ``coverage = present_w / total_w`` — missing modalities always lower
  confidence, never silently inflate it;
- ALL THREE missing -> null-with-reason under the pinned metric contract
  (``{value, unit, confidence, reason-if-null, proxy?}``), never a guess.

Also ships the US-F5 confidence-calibration harness: :func:`reliability_diagram`
(bins of observed accuracy vs stated confidence + a ``calibration_gap``
summary, the confidence-weighted expected calibration error) and
:func:`confusion_gate`, the release-over-release regression gate that FAILS
whenever the edge->middled misclassification rate rises (US-F5 REG AC).

Outputs are NOT proxies (``proxy=False``): unlike the US-E3 wrist stand-ins,
these come from real ball-track/audio/bat-detection inputs, and their
provenance ``source`` names the modalities used, e.g.
``contact-fusion-1.0.0(track+audio)``.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cricai_data.enums import Contact, LabelClass
from cricai_vision.audio_onset import OnsetKind, RefinedContact
from cricai_vision.detect import Detection

from cricai_coaching.contact_metrics import MetricValue

#: Version recorded in every fused metric's ``source`` provenance.
FUSION_VERSION = "contact-fusion-1.0.0"

#: Modality names as they appear in provenance strings and summaries.
MODALITY_TRACK = "track"
MODALITY_AUDIO = "audio"
MODALITY_BAT = "bat"

#: ball_metrics contact-phase keys owned by the US-F5 fusion job. Both writers
#: of the contact phase honor these: the fusion job merge-writes exactly these
#: keys, and the metrics PUT preserves them across whole-phase-set replaces so
#: an E3 re-run cannot silently delete fusion outputs.
FUSION_METRIC_KEYS: tuple[str, str] = ("contact_quality", "bat_path")

#: Argmax tie-break priority: edge first (edge recall matters most, US-F5).
TIE_BREAK_ORDER: tuple[Contact, ...] = (Contact.EDGE, Contact.MIDDLE, Contact.MISS)

#: Track-modality score tables (rows of docs/coaching_metrics.md US-F5).
TRACK_SCORES_MIDDLED: dict[Contact, float] = {
    Contact.MIDDLE: 0.75,
    Contact.EDGE: 0.20,
    Contact.MISS: 0.05,
}
TRACK_SCORES_EDGE: dict[Contact, float] = {
    Contact.MIDDLE: 0.20,
    Contact.EDGE: 0.70,
    Contact.MISS: 0.10,
}
TRACK_SCORES_FAINT: dict[Contact, float] = {
    Contact.MIDDLE: 0.10,
    Contact.EDGE: 0.50,
    Contact.MISS: 0.40,
}
TRACK_SCORES_NO_DEVIATION: dict[Contact, float] = {
    Contact.MIDDLE: 0.45,
    Contact.EDGE: 0.35,
    Contact.MISS: 0.20,
}
TRACK_SCORES_MISS: dict[Contact, float] = {
    Contact.MIDDLE: 0.05,
    Contact.EDGE: 0.15,
    Contact.MISS: 0.80,
}

#: Audio-modality score tables per onset class (audio alone cannot cleanly
#: split edge from middle — both crack; honesty documented in the US-F5 docs).
AUDIO_SCORES: dict[OnsetKind, dict[Contact, float]] = {
    OnsetKind.BAT_CRACK: {Contact.MIDDLE: 0.60, Contact.EDGE: 0.35, Contact.MISS: 0.05},
    OnsetKind.THUD: {Contact.MIDDLE: 0.10, Contact.EDGE: 0.20, Contact.MISS: 0.70},
    OnsetKind.UNKNOWN: {Contact.MIDDLE: 1 / 3, Contact.EDGE: 1 / 3, Contact.MISS: 1 / 3},
}

#: Bat-modality score tables by bat-to-ball box gap at the contact frame.
BAT_SCORES_OVERLAP: dict[Contact, float] = {
    Contact.MIDDLE: 0.55,
    Contact.EDGE: 0.35,
    Contact.MISS: 0.10,
}
BAT_SCORES_NEAR: dict[Contact, float] = {
    Contact.MIDDLE: 0.30,
    Contact.EDGE: 0.45,
    Contact.MISS: 0.25,
}
BAT_SCORES_FAR: dict[Contact, float] = {
    Contact.MIDDLE: 0.05,
    Contact.EDGE: 0.15,
    Contact.MISS: 0.80,
}

#: Bat-path classes emitted by :func:`bat_path_from_detections` (US-F5).
BAT_PATH_CLASSES: tuple[str, ...] = ("straight", "across", "inside_out")


class FusionError(ValueError):
    """Raised for malformed fusion inputs (payloads, matrices, configuration)."""


@dataclass(frozen=True)
class FusionConfig:
    """Every fusion weight and threshold lives here, not in code (coach-tunable)."""

    track_weight: float = 0.5
    audio_weight: float = 0.3
    bat_weight: float = 0.2
    deviation_points: int = 3  # track points sampled each side of the bat plane
    edge_min_deviation_deg: float = 8.0  # deviation at/above -> at least an edge
    middled_min_deviation_deg: float = 35.0  # deviation at/above -> clean-hit range
    no_post_contact_confidence: float = 0.7  # miss inferred from segment ABSENCE
    flagged_track_penalty: float = 0.5  # identity_risk/long_gap tracks count less
    contact_frame_tolerance_ms: float = 25.0  # "at the contact frame" half-window
    bat_near_gap: float = 0.05  # normalized box gap counted as "near" the ball
    swing_window_ms: float = 300.0  # bat boxes sampled this long before contact
    min_bat_frames: int = 2  # distinct bat frames needed for a path class
    bat_path_min_travel: float = 0.01  # normalized travel below this -> null
    bat_path_straight_min_deg: float = 20.0
    bat_path_straight_max_deg: float = 60.0
    toward_ball_sign: float = -1.0  # image-x sign toward the ball line (C1 rig)

    def __post_init__(self) -> None:
        if min(self.track_weight, self.audio_weight, self.bat_weight) <= 0:
            raise FusionError("modality weights must all be > 0")
        if self.deviation_points < 2:
            raise FusionError(f"deviation_points must be >= 2, got {self.deviation_points}")
        if not 0 < self.edge_min_deviation_deg < self.middled_min_deviation_deg:
            raise FusionError("deviation thresholds must satisfy 0 < edge_min < middled_min")
        if not 0.0 <= self.no_post_contact_confidence <= 1.0:
            raise FusionError("no_post_contact_confidence must be in [0, 1]")
        if not 0.0 <= self.flagged_track_penalty <= 1.0:
            raise FusionError("flagged_track_penalty must be in [0, 1]")
        if self.contact_frame_tolerance_ms < 0 or self.bat_near_gap < 0:
            raise FusionError("contact_frame_tolerance_ms and bat_near_gap must be >= 0")
        if self.swing_window_ms <= 0 or self.bat_path_min_travel <= 0:
            raise FusionError("swing_window_ms and bat_path_min_travel must be > 0")
        if self.min_bat_frames < 2:
            raise FusionError(f"min_bat_frames must be >= 2, got {self.min_bat_frames}")
        if self.bat_path_straight_min_deg >= self.bat_path_straight_max_deg:
            raise FusionError("bat-path rule table must have straight_min < straight_max")
        if self.toward_ball_sign not in (-1.0, 1.0):
            raise FusionError(f"toward_ball_sign must be -1.0 or 1.0, got {self.toward_ball_sign}")


#: Shared frozen default (avoids constructing in argument defaults).
DEFAULT_FUSION_CONFIG = FusionConfig()


@dataclass(frozen=True)
class ModalitySignal:
    """One modality's vote: a score distribution over Contact + its confidence."""

    scores: Mapping[Contact, float]
    confidence: float
    note: str  # human-readable provenance detail for summaries/debugging

    def __post_init__(self) -> None:
        if set(self.scores) != set(Contact):
            raise FusionError("scores must cover exactly the Contact classes")
        if any(score < 0 for score in self.scores.values()):
            raise FusionError("scores must be non-negative")
        total = sum(self.scores.values())
        if abs(total - 1.0) > 1e-6:
            raise FusionError(f"scores must sum to 1, got {total}")
        if not 0.0 <= self.confidence <= 1.0:
            raise FusionError(f"confidence must be in [0, 1], got {self.confidence}")


@dataclass(frozen=True)
class FusionResult:
    """Fused contact-quality metric plus diagnostics (modalities used, scores)."""

    contact_quality: MetricValue
    used: tuple[str, ...]
    combined: Mapping[Contact, float]


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


# --- track modality ---------------------------------------------------------


def _payload_parts(payload: Mapping[str, Any]) -> tuple[list[Any], list[Any], dict[str, Any]]:
    """The pinned payload's points/segments/flags, or FusionError when malformed."""
    points = payload.get("points")
    segments = payload.get("segments")
    flags = payload.get("flags")
    if not (isinstance(points, list) and isinstance(segments, list) and isinstance(flags, dict)):
        raise FusionError("track payload must carry points (list), segments (list), flags (object)")
    return points, segments, flags


def _segment_span(segment: Mapping[str, Any]) -> tuple[float, float]:
    """The segment's [start_ms, end_ms] time span; FusionError when malformed."""
    try:
        return float(segment["start_ms"]), float(segment["end_ms"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FusionError(f"track segment missing numeric start_ms/end_ms: {segment!r}") from exc


def _post_contact_segment(segments: list[Any]) -> tuple[tuple[float, float], float] | None:
    """([start_ms, end_ms], confidence) of the first post_contact segment, or None."""
    for segment in segments:
        if isinstance(segment, dict) and segment.get("kind") == "post_contact":
            span = _segment_span(segment)
            try:
                return span, float(segment["confidence"])
            except (KeyError, TypeError, ValueError) as exc:
                raise FusionError(f"malformed post_contact segment: {segment!r}") from exc
    return None


def _incoming_segment_bounds(
    segments: list[Any], bat_plane_ms: float
) -> tuple[float, float] | None:
    """[start_ms, end_ms] of the flight phase feeding the bat plane, or None.

    The latest non-post_contact segment starting at/before the plane. Segment
    boundaries mark real direction changes (the bounce above all), so the
    incoming-velocity window must never reach past this segment's start into an
    earlier phase: with bridged fills excluded, an unbounded window around an
    occluded contact would backfill from pre-bounce descent points and read the
    bounce itself as bat deviation (a faint edge scored as a clean hit). None
    when the payload carries no such segment — nothing to bound by.
    """
    best: tuple[float, float] | None = None
    for segment in segments:
        if not isinstance(segment, dict) or segment.get("kind") == "post_contact":
            continue
        span = _segment_span(segment)
        if span[0] <= bat_plane_ms and (best is None or span[0] > best[0]):
            best = span
    return best


def _points_xy(points: list[Any]) -> list[tuple[float, float, float]]:
    """(ts_ms, px_x, px_y) of the REAL points sorted by time; FusionError on malformed.

    Bridged points are the tracker's constant-velocity chord fills (US-F3,
    explicitly fabricated): around an occluded contact both velocity windows
    would lie exactly on the chord and read ~0 deg deviation for a middled
    ball, so they never count as measurements here.
    """
    out: list[tuple[float, float, float]] = []
    for point in points:
        if not isinstance(point, dict):
            raise FusionError(f"track payload point is not an object: {point!r}")
        if point.get("bridged"):
            continue
        try:
            out.append((float(point["ts_ms"]), float(point["px_x"]), float(point["px_y"])))
        except (KeyError, TypeError, ValueError) as exc:
            raise FusionError(f"track point missing numeric ts_ms/px_x/px_y: {point!r}") from exc
    return sorted(out)


def _within(ts_ms: float, bounds: tuple[float, float] | None) -> bool:
    """True when the timestamp lies inside the bounds (unbounded when None)."""
    return bounds is None or bounds[0] <= ts_ms <= bounds[1]


def _direction_change_deg(
    points: list[tuple[float, float, float]],
    bat_plane_ms: float,
    k: int,
    *,
    pre_bounds: tuple[float, float] | None,
    post_bounds: tuple[float, float],
) -> float | None:
    """|angle| between the incoming and outgoing velocity around the bat plane.

    Uses up to ``k`` points each side, each side clamped to its own flight
    segment (``pre_bounds``/``post_bounds``) so a window never straddles a
    segment boundary and measures a bounce as bat deviation. None when either
    side has fewer than two usable points or a degenerate (zero-length)
    direction vector.
    """
    pre = [p for p in points if p[0] <= bat_plane_ms and _within(p[0], pre_bounds)][-k:]
    post = [p for p in points if p[0] > bat_plane_ms and _within(p[0], post_bounds)][:k]
    if len(pre) < 2 or len(post) < 2:
        return None
    v_pre = (pre[-1][1] - pre[0][1], pre[-1][2] - pre[0][2])
    v_post = (post[-1][1] - post[0][1], post[-1][2] - post[0][2])
    if math.hypot(*v_pre) < 1e-9 or math.hypot(*v_post) < 1e-9:
        return None
    cross = v_pre[0] * v_post[1] - v_pre[1] * v_post[0]
    dot = v_pre[0] * v_post[0] + v_pre[1] * v_post[1]
    return abs(math.degrees(math.atan2(cross, dot)))


def track_signal(
    payload: Mapping[str, Any],
    *,
    contact_ms: float | None,
    config: FusionConfig = DEFAULT_FUSION_CONFIG,
) -> ModalitySignal:
    """Track-modality vote from the pinned US-F3 payload.

    No post-contact segment -> miss-leaning (the flight never changed); with a
    post-contact segment the direction-change magnitude at the bat plane
    (``contact_ms`` when known, else the segment start) selects the clean-hit /
    deflection / faint score table. Each velocity window uses only real
    (non-bridged) points inside its own flight segment — the incoming window is
    clamped to the latest non-post_contact segment starting at/before the bat
    plane, the outgoing window to the post_contact segment — degrading to the
    presence-only table when either side cannot measure.
    ``identity_risk``/``long_gap`` flags scale confidence down by
    ``flagged_track_penalty``. Raises :class:`FusionError` on malformed
    payloads — callers degrade to a missing modality.
    """
    points, segments, flags = _payload_parts(payload)
    post = _post_contact_segment(segments)
    flagged = bool(flags.get("identity_risk")) or bool(flags.get("long_gap"))
    penalty = config.flagged_track_penalty if flagged else 1.0
    if post is None:
        return ModalitySignal(
            scores=TRACK_SCORES_MISS,
            confidence=round(config.no_post_contact_confidence * penalty, 4),
            note="no post-contact segment (ball flight unchanged)",
        )
    post_span, seg_confidence = post
    bat_plane_ms = contact_ms if contact_ms is not None else post_span[0]
    deviation = _direction_change_deg(
        _points_xy(points),
        bat_plane_ms,
        config.deviation_points,
        pre_bounds=_incoming_segment_bounds(segments, bat_plane_ms),
        post_bounds=post_span,
    )
    if deviation is None:
        scores, note = (
            TRACK_SCORES_NO_DEVIATION,
            "post-contact segment present; deviation not computable",
        )
    elif deviation >= config.middled_min_deviation_deg:
        scores, note = TRACK_SCORES_MIDDLED, f"deviation {deviation:.1f} deg (clean-hit range)"
    elif deviation >= config.edge_min_deviation_deg:
        scores, note = TRACK_SCORES_EDGE, f"deviation {deviation:.1f} deg (deflection range)"
    else:
        scores, note = TRACK_SCORES_FAINT, f"deviation {deviation:.1f} deg (below edge range)"
    return ModalitySignal(
        scores=scores, confidence=round(_clamp01(seg_confidence) * penalty, 4), note=note
    )


# --- audio modality ---------------------------------------------------------


def audio_signal(refined: RefinedContact) -> ModalitySignal:
    """Audio-modality vote from US-D3's :class:`RefinedContact` (public API)."""
    return ModalitySignal(
        scores=AUDIO_SCORES[refined.kind],
        confidence=round(_clamp01(refined.confidence), 4),
        note=f"audio onset {refined.kind.value} at {refined.contact_ms:.0f} ms",
    )


# --- bat-detection modality --------------------------------------------------


def _box_gap(a: Detection, b: Detection) -> float:
    """Normalized gap between two boxes; 0.0 when they overlap."""
    dx = max(0.0, abs(a.cx - b.cx) - (a.w + b.w) / 2)
    dy = max(0.0, abs(a.cy - b.cy) - (a.h + b.h) / 2)
    return math.hypot(dx, dy)


def _real_point_near(track_points: Sequence[Any], ts_ms: float, tolerance_ms: float) -> bool:
    """True when a real (non-bridged) tracked point lies within the tolerance window.

    Tolerant of malformed points — junk never corroborates, it just doesn't count.
    """
    for point in track_points:
        if not isinstance(point, Mapping) or point.get("bridged"):
            continue
        point_ts = point.get("ts_ms")
        if isinstance(point_ts, int | float) and abs(point_ts - ts_ms) <= tolerance_ms:
            return True
    return False


def bat_signal(
    detections: Sequence[Detection],
    *,
    contact_ts_ms: float,
    track_points: Sequence[Any] | None = None,
    config: FusionConfig = DEFAULT_FUSION_CONFIG,
) -> ModalitySignal | None:
    """Bat-modality vote from bat/ball boxes at the contact frame, or None.

    Considers detections within ``contact_frame_tolerance_ms`` of the contact
    instant; needs at least one ball AND one bat box there, else the modality
    is missing (None). The closest bat-ball pair's gap selects the overlap /
    near / far score table; confidence is the weaker of the pair's scores.

    ``track_points`` (the pinned US-F3 payload points, when the caller has
    them) gate identity: without a real (non-bridged) tracked-ball point
    within the same tolerance of the contact instant, every ball box there is
    an unverified impostor — e.g. a static decoy in the net while the bat
    occludes the real ball — so the modality is missing rather than a
    confident far/miss vote against the wrong ball.
    """
    if track_points is not None and not _real_point_near(
        track_points, contact_ts_ms, config.contact_frame_tolerance_ms
    ):
        return None
    at_contact = [
        d for d in detections if abs(d.ts_ms - contact_ts_ms) <= config.contact_frame_tolerance_ms
    ]
    balls = [d for d in at_contact if d.label is LabelClass.BALL]
    bats = [d for d in at_contact if d.label is LabelClass.BAT]
    if not balls or not bats:
        return None
    ball, bat, gap = min(
        ((b, t, _box_gap(b, t)) for b in balls for t in bats), key=lambda pair: pair[2]
    )
    if gap == 0.0:
        scores, note = BAT_SCORES_OVERLAP, "bat box overlaps ball at contact"
    elif gap <= config.bat_near_gap:
        scores, note = BAT_SCORES_NEAR, f"bat within {gap:.3f} of ball at contact"
    else:
        scores, note = BAT_SCORES_FAR, f"bat {gap:.3f} from ball at contact"
    return ModalitySignal(scores=scores, confidence=round(min(ball.score, bat.score), 4), note=note)


def bat_path_from_detections(
    detections: Sequence[Detection],
    *,
    contact_ts_ms: float,
    config: FusionConfig = DEFAULT_FUSION_CONFIG,
) -> MetricValue:
    """Bat-path class from real bat boxes over the swing window (proxy=False).

    Takes the best bat box per frame inside ``[contact - swing_window_ms,
    contact]`` and classifies the first->last displacement angle
    ``theta = atan2(dx_toward_ball, dy_down)`` with the rule table
    straight [straight_min, straight_max] / across (> straight_max) /
    inside_out (< straight_min). Null-with-reason on too few frames or too
    little travel — a class is never fabricated from noise.
    """
    window_start = contact_ts_ms - config.swing_window_ms
    best_per_frame: dict[int, Detection] = {}
    for detection in detections:
        if detection.label is not LabelClass.BAT:
            continue
        if not window_start <= detection.ts_ms <= contact_ts_ms:
            continue
        current = best_per_frame.get(detection.frame_no)
        if current is None or detection.score > current.score:
            best_per_frame[detection.frame_no] = detection
    if len(best_per_frame) < config.min_bat_frames:
        return MetricValue(
            None,
            "class",
            0.0,
            reason=(
                f"only {len(best_per_frame)} bat detection frames in the swing window"
                f" (need >= {config.min_bat_frames})"
            ),
        )
    ordered = [best_per_frame[frame_no] for frame_no in sorted(best_per_frame)]
    first, last = ordered[0], ordered[-1]
    dx_toward = (last.cx - first.cx) * config.toward_ball_sign
    dy_down = last.cy - first.cy
    travel = math.hypot(dx_toward, dy_down)
    if travel < config.bat_path_min_travel:
        return MetricValue(
            None,
            "class",
            0.0,
            reason=(
                f"bat travel {travel:.4f} below {config.bat_path_min_travel:.4f}"
                " in the swing window (cannot classify swing direction)"
            ),
        )
    theta = math.degrees(math.atan2(dx_toward, dy_down))
    if theta > config.bat_path_straight_max_deg:
        label = "across"
    elif theta >= config.bat_path_straight_min_deg:
        label = "straight"
    else:
        label = "inside_out"
    confidence = round(statistics.fmean(d.score for d in ordered), 4)
    return MetricValue(label, "class", confidence, source=f"{FUSION_VERSION}(bat:{len(ordered)}f)")


# --- fusion -------------------------------------------------------------------


def _argmax(combined: Mapping[Contact, float]) -> Contact:
    """Deterministic argmax; ties resolve by :data:`TIE_BREAK_ORDER`."""
    return max(TIE_BREAK_ORDER, key=lambda contact: combined[contact])


def fuse_contact(
    *,
    track: ModalitySignal | None,
    audio: RefinedContact | None,
    bat: ModalitySignal | None,
    config: FusionConfig = DEFAULT_FUSION_CONFIG,
) -> FusionResult:
    """Fuse the available modalities into one contact_quality metric.

    Any subset (all 7 present/absent combinations) degrades gracefully: base
    weights renormalize over the present modalities and the missing coverage
    scales confidence down. All three missing -> null-with-reason (never a
    guess). Provenance names the used modalities, e.g.
    ``contact-fusion-1.0.0(track+audio)``.
    """
    signals: list[tuple[str, float, ModalitySignal]] = []
    if track is not None:
        signals.append((MODALITY_TRACK, config.track_weight, track))
    if audio is not None:
        signals.append((MODALITY_AUDIO, config.audio_weight, audio_signal(audio)))
    if bat is not None:
        signals.append((MODALITY_BAT, config.bat_weight, bat))
    used = tuple(name for name, _, _ in signals)
    if not signals:
        return FusionResult(
            contact_quality=MetricValue(
                None,
                "class",
                0.0,
                reason="all fusion modalities missing (no track, no audio, no bat detections)",
                source=f"{FUSION_VERSION}()",
            ),
            used=(),
            combined=dict.fromkeys(Contact, 0.0),
        )
    total_weight = config.track_weight + config.audio_weight + config.bat_weight
    present_weight = sum(weight for _, weight, _ in signals)
    combined = {
        contact: sum(
            (weight / present_weight) * signal.scores[contact] for _, weight, signal in signals
        )
        for contact in Contact
    }
    top = _argmax(combined)
    coverage = present_weight / total_weight
    weighted_confidence = sum(
        (weight / present_weight) * signal.confidence for _, weight, signal in signals
    )
    confidence = round(combined[top] * coverage * weighted_confidence, 4)
    return FusionResult(
        contact_quality=MetricValue(
            top.value, "class", confidence, source=f"{FUSION_VERSION}({'+'.join(used)})"
        ),
        used=used,
        combined=combined,
    )


# --- confidence-calibration harness (US-F5) -----------------------------------


@dataclass(frozen=True)
class ReliabilityBin:
    """One confidence bin: stated confidence vs observed accuracy."""

    lo: float
    hi: float
    count: int
    mean_confidence: float | None  # None for empty bins
    observed_accuracy: float | None  # None for empty bins


@dataclass(frozen=True)
class ReliabilityReport:
    """Reliability diagram + the calibration_gap summary (weighted ECE)."""

    bins: tuple[ReliabilityBin, ...]
    total: int
    calibration_gap: float  # sum over bins of (count/total) * |confidence - accuracy|


def reliability_diagram(
    pairs: Sequence[tuple[float, bool]], *, bins: int = 10
) -> ReliabilityReport:
    """Bin (confidence, correct) pairs and report observed accuracy per bin.

    ``calibration_gap`` is the count-weighted mean |mean_confidence -
    observed_accuracy| over non-empty bins (expected calibration error). An
    empty input yields empty bins and gap 0.0 — no data is no evidence of
    miscalibration, and the ``total`` of 0 keeps that visible.
    """
    if bins < 1:
        raise FusionError(f"bins must be >= 1, got {bins}")
    grouped: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for confidence, correct in pairs:
        if not 0.0 <= confidence <= 1.0:
            raise FusionError(f"confidence must be in [0, 1], got {confidence}")
        grouped[min(int(confidence * bins), bins - 1)].append((confidence, correct))
    total = sum(len(group) for group in grouped)
    out: list[ReliabilityBin] = []
    gap = 0.0
    for index, group in enumerate(grouped):
        lo, hi = index / bins, (index + 1) / bins
        if not group:
            out.append(ReliabilityBin(lo, hi, 0, None, None))
            continue
        mean_confidence = statistics.fmean(confidence for confidence, _ in group)
        accuracy = sum(1 for _, correct in group if correct) / len(group)
        gap += (len(group) / total) * abs(mean_confidence - accuracy)
        out.append(
            ReliabilityBin(lo, hi, len(group), round(mean_confidence, 4), round(accuracy, 4))
        )
    return ReliabilityReport(bins=tuple(out), total=total, calibration_gap=round(gap, 4))


#: true class -> predicted class -> count.
ConfusionMatrix = Mapping[Contact, Mapping[Contact, int]]


@dataclass(frozen=True)
class GateResult:
    """Outcome of the edge->middled regression gate (US-F5 REG AC)."""

    passed: bool
    old_rate: float
    new_rate: float
    detail: str


def _edge_to_middled_rate(matrix: ConfusionMatrix, name: str) -> float:
    """edge->middled misclassification rate; 0.0 when the matrix has no edges."""
    for true_class, row in matrix.items():
        for predicted, count in row.items():
            if count < 0:
                raise FusionError(
                    f"{name} matrix count for {true_class.value}->{predicted.value} is negative"
                )
    edge_row = matrix.get(Contact.EDGE, {})
    total = sum(edge_row.values())
    if total == 0:
        return 0.0
    return edge_row.get(Contact.MIDDLE, 0) / total


def confusion_gate(
    old_matrix: ConfusionMatrix, new_matrix: ConfusionMatrix, *, tolerance: float = 0.0
) -> GateResult:
    """FAIL when the edge->middled misclassification rate rises vs the old release.

    Rates are ``count(edge classified middled) / count(true edges)``; a release
    whose benchmark has no true edges scores 0.0 (the gate cannot measure what
    the benchmark does not contain — benchmark composition is the US-F5
    edge-drill MV's job).
    """
    if tolerance < 0:
        raise FusionError(f"tolerance must be >= 0, got {tolerance}")
    old_rate = _edge_to_middled_rate(old_matrix, "old")
    new_rate = _edge_to_middled_rate(new_matrix, "new")
    passed = new_rate <= old_rate + tolerance
    verdict = "held" if passed else "rose"
    return GateResult(
        passed=passed,
        old_rate=round(old_rate, 4),
        new_rate=round(new_rate, 4),
        detail=(
            f"edge->middled misclassification rate {verdict}:"
            f" {old_rate:.4f} -> {new_rate:.4f} (tolerance {tolerance:.4f})"
        ),
    )
