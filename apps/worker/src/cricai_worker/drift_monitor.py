"""Weekly drift & data-quality monitor job (US-L4): DB rows -> ``alerts`` rows.

Wires the pure ``cricai_coaching.drift``/``quality`` functions over the week
of sessions just ended (``session_date`` in ``[week_start, week_start + 7d)``):

- pools ``ground_truth_eligible`` manual tags from the PRIOR week's sessions
  (``[week_start - 7d, week_start)`` — the week whose sample the previous run
  posted, tagged during the week just ended) against auto-derived per-ball
  fields (default loader: ``bounce_estimates`` line/length — richer auto
  sources plug in via ``auto_fields``) and persists developer drift alerts.
  This closes the runbook loop: sample -> tag -> the NEXT run compares those
  exact balls;
- persists the sample-shortfall alert when the prior week's forever-manual
  sample fell below protocol;
- ALWAYS evaluates the canary: by default each run RE-DERIVES the frozen
  canary session through the production pipeline path
  (:func:`derive_canary_observed`: the events call-adapter's real
  segmentation over the frozen probe envelope -> the frozen synthetic
  per-ball trajectories -> the real US-F4 bounce estimator + zone classifier
  -> the same auto-fields loader the agreement leg uses) and compares the
  observations against the frozen expectations. ``drift_canary_missing`` now
  fires only on GENUINE failure — a derivation error (recorded on the alert
  as ``derivation_error``) or a pipeline that produced nothing comparable —
  never as a standing by-design alarm (silence is a failure, never a pass);
- scores every session's data quality and persists a parent honesty alert
  (``data_quality_low``) whenever the report must carry the banner;
- selects the week's ~10-ball manual sample, stratified across the week's
  sessions and auto length zones, and persists it as a developer
  ``ground_truth_sample`` alert for the runbook workflow.

Idempotent: every alert carries a deterministic ``detail.dedupe_key``; a
re-run of the same week inserts nothing new (crash-resume is a plain re-run).
"""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from cricai_coaching.drift import (
    CANARY_BLOCKS,
    CANARY_SEED,
    SampleCandidate,
    canary_alerts,
    drift_alerts,
    field_agreement,
    paired_ball_count,
    sample_shortfall_alert,
    select_weekly_sample,
)
from cricai_coaching.quality import SessionQualityInputs, score_session
from cricai_data.db import session_scope
from cricai_data.enums import (
    AlertAudience,
    BowlerSource,
    CalibrationKind,
    ClipStatus,
    SessionType,
    VideoStatus,
)
from cricai_data.models import (
    Alert,
    BallEvent,
    BallTag,
    BallTrack,
    BounceEstimate,
    Calibration,
    Clip,
    Player,
    PoseTrack,
    Video,
)
from cricai_data.models import Session as SessionRow
from cricai_data.synthetic import generate_session
from cricai_vision.extrinsics import PlaneCalibration, to_params
from cricai_vision.zones import ZoneConfig
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cricai_worker.context import WorkerContext
from cricai_worker.estimate_bounces import estimate_session_bounces
from cricai_worker.rederive import rederive_session
from cricai_worker.stage_adapters import (
    PROBE_MOTION_ENERGY_KEY,
    run_events_stage,
)
from cricai_worker.track_balls import track_key

#: Per-session auto-derived per-ball fields: (db, session_id) -> ball_no -> fields.
AutoFieldsLoader = Callable[[Session, uuid.UUID], dict[int, dict[str, object]]]

#: Per-session quality inputs: (db, session row) -> injected plain row data.
QualityInputsLoader = Callable[[Session, SessionRow], SessionQualityInputs]

#: Canary re-derivation seam: (ctx) -> observed per-ball fields; the default
#: is :func:`derive_canary_observed` — tests inject fakes/failures.
CanaryDeriver = Callable[[WorkerContext], Mapping[int, Mapping[str, object]]]

PARENT_QUALITY_CODE = "data_quality_low"
SAMPLE_CODE = "ground_truth_sample"


@dataclass(frozen=True)
class DriftMonitorSummary:
    """What one weekly run did — alert counts and the selected manual sample.

    ``paired_balls`` counts the COMPARED week (``compared_week_start``, the
    week before ``week_start``): the sample posted then, tagged since.
    """

    week_start: str
    compared_week_start: str
    sessions: int
    paired_balls: int
    alerts_written: int
    alerts_skipped: int
    sampled: tuple[tuple[str, int], ...]


def bounce_estimate_fields(db: Session, session_id: uuid.UUID) -> dict[int, dict[str, object]]:
    """Default auto-fields loader: line/length zone classes from ``bounce_estimates``.

    The only per-ball fields the pipeline auto-derives today (US-F4); richer
    sources (auto shot/footwork classifiers) inject their own loader without
    touching this job. ``None`` zone classes stay ``None`` — an off-pitch
    vertex is not a disagreement.
    """
    fields: dict[int, dict[str, object]] = {}
    for row in db.scalars(select(BounceEstimate).where(BounceEstimate.session_id == session_id)):
        fields[row.ball_no] = {
            "line": row.line.value if row.line is not None else None,
            "length": row.length.value if row.length is not None else None,
        }
    return fields


# --- canary re-derivation (US-L4, Phase-6 debt retirement) --------------------
#
# The frozen canary session is materialized as a REAL session (deterministic
# ids, synthetic GUEST player — US-L3 guest scoping keeps it off kid-visible
# surfaces and out of the rollup sweep) so its re-derivation runs through the
# same ``(ctx, session_id)`` entrypoints production sessions use. Its frozen
# INPUTS are the probe motion-energy envelope (one bump per synthetic ball)
# and one synthetic trajectory per ball whose bounce vertex maps into the
# ball's (line, length) zone through the canary calibration — a record pinned
# under the reserved :data:`CANARY_CAMERA` id so no real-camera calibration
# lookup can ever resolve it. Everything downstream is real derivation code:
# the events adapter's segmentation, the US-F4 bounce estimator, the
# homography mapping, the zone classifier, and the ``bounce_estimate_fields``
# loader — a regression anywhere in that chain shows up as a canary
# disagreement, and a broken derivation as ``drift_canary_missing`` with the
# error recorded.

CANARY_ACTOR = "drift-canary"

#: Reserved synthetic camera id — deliberately OUTSIDE the ``C[1-8]`` id space
#: every real surface enforces (the camera registry and the calibration
#: create/attach/drift-check request patterns), and within the ``String(8)``
#: column bound. Containment is structural: no API request can name this
#: camera, the registry can never give it an active placement era, and every
#: era-scoped calibration lookup (API ``_latest_valid``, worker
#: ``_era_calibration``, ``bounce_mapping._current_era_calibration``) filters
#: on the real camera id it was asked about — so the canary's calibration row
#: can never shadow a real camera's record or be attached to a real session
#: (the explicit-id attach path 422s on the unregistered camera's era). The
#: canary's own pipeline is unaffected: it maps through the session-attached
#: calibration, which matches this camera id exactly (US-C3/US-L4).
CANARY_CAMERA = "CANARY"
CANARY_FPS = 30.0

#: Far outside any realistically monitored week, so the canary session never
#: enters weekly quality scoring, sampling or agreement windows.
CANARY_SESSION_DATE = date(2000, 1, 3)

#: Deterministic identities: every run converges on the same rows.
CANARY_PLAYER_ID = uuid.uuid5(uuid.NAMESPACE_URL, "cricai:drift-canary:player")
CANARY_SESSION_ID = uuid.uuid5(uuid.NAMESPACE_URL, f"cricai:drift-canary:session:{CANARY_SEED}")

#: Provenance stamped on the seeded synthetic canary tracks.
CANARY_TRACKER_VERSION = "canary-synth-1"

#: Envelope shape: 2 s of stillness between deliveries (>> the segmenter's
#: merge/min-gap windows), 1 s of motion per delivery (inside its min/max
#: event duration) — 30 clean events at :data:`CANARY_FPS`, forever.
_QUIET_FRAMES = 60
_ACTIVE_FRAMES = 30

#: Frozen pixel->pitch homography: ``pitch_x = px_x / 100``,
#: ``pitch_y = px_y / 200 - 2.7`` — invertible, so zone targets place vertex
#: pixels exactly (see :func:`_canary_pixel`).
_CANARY_CALIBRATION = PlaneCalibration(
    matrix=((0.01, 0.0, 0.0), (0.0, 0.005, -2.7), (0.0, 0.0, 1.0)),
    rms_px=0.0,
    rms_m=0.0,
    n_landmarks=6,
)


def _canary_pixel(pitch_x: float, pitch_y: float) -> tuple[float, float]:
    """The pixel that :data:`_CANARY_CALIBRATION` maps to (pitch_x, pitch_y)."""
    return (pitch_x * 100.0, (pitch_y + 2.7) * 200.0)


def _band_center(lo: float, hi: float) -> float:
    """A point safely inside a zone band (half-open bands: edges excluded)."""
    if math.isinf(lo):
        return hi - 0.5
    if math.isinf(hi):
        return lo + 0.5
    return (lo + hi) / 2.0


def canary_zone_targets(config: ZoneConfig | None = None) -> dict[int, tuple[float, float]]:
    """ball_no -> (pitch_x, pitch_y) classifying into the frozen ball's zones.

    Derived from the zone config's own bands, so tuning the (configuration,
    not code) band values re-targets the canary instead of false-alarming;
    a regression in the classifier itself still trips it.
    """
    cfg = config if config is not None else ZoneConfig()
    session = generate_session(CANARY_SEED, CANARY_BLOCKS)
    targets: dict[int, tuple[float, float]] = {}
    for ball in session.balls:
        x_lo, x_hi = cfg.length_bands_m[ball.length]
        y_lo, y_hi = cfg.line_channels_m[ball.line]
        targets[ball.ball_no] = (_band_center(x_lo, x_hi), _band_center(y_lo, y_hi))
    return targets


def _canary_motion_energy(n_balls: int) -> list[float]:
    """The frozen per-frame envelope: one peaked 1 s bump per synthetic ball."""
    envelope: list[float] = []
    half = _ACTIVE_FRAMES // 2
    for _ in range(n_balls):
        envelope.extend([0.0] * _QUIET_FRAMES)
        envelope.extend(1.0 - 0.02 * abs(index - half) for index in range(_ACTIVE_FRAMES))
    envelope.extend([0.0] * _QUIET_FRAMES)
    return envelope


def ensure_canary_session(ctx: WorkerContext) -> uuid.UUID:
    """Idempotently materialize the frozen canary session and its inputs.

    Deterministic ids converge every run on the same player/session rows; the
    probe payload (fps + motion-energy envelope), the session-attached
    calibration row and the player's guest containment are (re)pinned to the
    frozen values each run so config drift in the DB — an operator
    invalidation sweep, an edited row, a stray upload — can never silently
    re-target the canary (US-L4). Two runs racing the first materialization
    (the scheduled sweep vs a manual smoke-test on a fresh DB) collide on the
    deterministic primary keys: the loser catches the ``IntegrityError`` and
    converges on the winner's rows with a plain idempotent re-run instead of
    surfacing a false ``drift_canary_missing`` page.
    """
    try:
        return _materialize_canary(ctx)
    except IntegrityError:
        # Lost the insert race on the deterministic ids: the rows exist now,
        # so a fresh pass takes the re-pin paths and converges.
        return _materialize_canary(ctx)


def _materialize_canary(ctx: WorkerContext) -> uuid.UUID:
    """One idempotent materialization pass (see :func:`ensure_canary_session`)."""
    balls = len(generate_session(CANARY_SEED, CANARY_BLOCKS).balls)
    envelope = _canary_motion_energy(balls)
    with session_scope(ctx.session_factory) as db:
        player = db.get(Player, CANARY_PLAYER_ID)
        if player is None:
            player = Player(id=CANARY_PLAYER_ID, name="Drift Canary", birthdate=date(2000, 1, 1))
            db.add(player)
        # US-L3 guest containment, re-pinned every run: guests are hidden from
        # the kid-visible player/session surfaces and skipped by the weekly
        # rollup sweep (``rollup_all_players``), so the synthetic canary can
        # never accrue reports or appear on longitudinal surfaces.
        player.is_guest = True
        session = db.get(SessionRow, CANARY_SESSION_ID)
        if session is None:
            session = SessionRow(
                id=CANARY_SESSION_ID,
                player_id=CANARY_PLAYER_ID,
                session_date=CANARY_SESSION_DATE,
                session_type=SessionType.BATTING,
                bowler_source=BowlerSource.MACHINE,
                expected_cameras=[CANARY_CAMERA],
            )
            db.add(session)
            db.flush()
        calibration = (
            db.get(Calibration, session.calibration_id)
            if session.calibration_id is not None
            else None
        )
        if calibration is None:
            calibration = Calibration(
                camera_id=CANARY_CAMERA, kind=CalibrationKind.EXTRINSIC, params={}
            )
            db.add(calibration)
            db.flush()
            session.calibration_id = calibration.id
        # Re-pin the frozen mapping every run: invalidation or edits of this
        # row (it is reachable by explicit id) must never re-target the canary
        # through a different homography.
        calibration.camera_id = CANARY_CAMERA
        calibration.era_no = 1
        calibration.kind = CalibrationKind.EXTRINSIC
        calibration.params = to_params(_CANARY_CALIBRATION)
        calibration.valid = True
        # The canary's evidence is exactly one synthetic video: a foreign row
        # (a stray upload, or a legacy real-camera canary row) could hijack
        # the reference-camera choice, so non-canary rows are pruned.
        db.execute(
            delete(Video).where(
                Video.session_id == CANARY_SESSION_ID, Video.camera_id != CANARY_CAMERA
            )
        )
        video = db.scalar(
            select(Video).where(
                Video.session_id == CANARY_SESSION_ID, Video.camera_id == CANARY_CAMERA
            )
        )
        probe = {
            "fps": CANARY_FPS,
            "width": 1920,
            "height": 1080,
            "resolution": "1920x1080",
            "codec": "synthetic",
            "duration_s": len(envelope) / CANARY_FPS,
            PROBE_MOTION_ENERGY_KEY: envelope,
        }
        if video is None:
            db.add(
                Video(
                    session_id=CANARY_SESSION_ID,
                    camera_id=CANARY_CAMERA,
                    object_key=f"canary/{CANARY_SESSION_ID}/{CANARY_CAMERA}.synthetic",
                    filename="drift-canary.synthetic",
                    checksum_sha256="c" * 64,
                    size_bytes=0,
                    status=VideoStatus.PROBED,
                    probe=probe,
                )
            )
        else:  # re-pin the frozen input every run
            video.probe = probe
            video.status = VideoStatus.PROBED
    return CANARY_SESSION_ID


def _seed_canary_tracks(ctx: WorkerContext, session_id: uuid.UUID) -> int:
    """Write the frozen synthetic trajectory of every re-detected canary ball.

    Each payload is a 7-point flight whose vertical-velocity sign change sits
    exactly at the ball's zone-target pixel, between a ``pre_bounce`` and a
    ``post_bounce`` segment — the US-F3 payload contract the real estimator
    consumes. Balls the segmenter numbered outside the frozen expectation set
    get no track (their absence is a canary disagreement, not a crash).
    """
    targets = canary_zone_targets()
    seeded = 0
    with session_scope(ctx.session_factory) as db:
        events = db.scalars(
            select(BallEvent)
            .where(BallEvent.session_id == session_id, BallEvent.valid.is_(True))
            .order_by(BallEvent.ball_no)
        )
        for event in events:
            target = targets.get(event.ball_no)
            if target is None:
                continue
            px_x, px_y = _canary_pixel(*target)
            bounce_ms = (event.start_ms + event.end_ms) / 2.0
            base_frame = round(bounce_ms * CANARY_FPS / 1000.0)
            points: list[dict[str, Any]] = [
                {
                    "frame_no": base_frame + offset,
                    "ts_ms": bounce_ms + 30.0 * offset,
                    "px_x": px_x + 4.0 * offset,
                    "px_y": px_y - 40.0 * abs(offset),
                    "score": 0.9,
                    "bridged": False,
                }
                for offset in range(-3, 4)
            ]
            segments: list[dict[str, Any]] = [
                {
                    "kind": "pre_bounce",
                    "start_ms": float(event.start_ms),
                    "end_ms": bounce_ms - 15.0,
                    "confidence": 0.95,
                },
                {
                    "kind": "post_bounce",
                    "start_ms": bounce_ms + 15.0,
                    "end_ms": float(event.end_ms),
                    "confidence": 0.95,
                },
            ]
            flags: dict[str, Any] = {"identity_risk": False, "long_gap": False}
            payload = {"version": 1, "points": points, "segments": segments, "flags": flags}
            key = track_key(session_id, event.ball_no, CANARY_CAMERA)
            ctx.store.put(key, json.dumps(payload).encode("utf-8"))
            row = db.scalar(
                select(BallTrack).where(
                    BallTrack.session_id == session_id,
                    BallTrack.ball_no == event.ball_no,
                    BallTrack.camera_id == CANARY_CAMERA,
                )
            )
            if row is None:
                row = BallTrack(
                    session_id=session_id, ball_no=event.ball_no, camera_id=CANARY_CAMERA
                )
                db.add(row)
            row.tracker_version = CANARY_TRACKER_VERSION
            row.points_key = key
            row.coverage = 1.0
            row.segments = segments
            row.flags = flags
            row.confidence = 0.95
            seeded += 1
    return seeded


def derive_canary_observed(ctx: WorkerContext) -> dict[int, dict[str, object]]:
    """Re-derive the frozen canary session through the production pipeline path.

    Weekly sequence: materialize/pin the frozen inputs -> clear the previous
    week's machine-derived rows (the audited re-derive cascade, so the events
    replace is legal) -> the events call-adapter's REAL segmentation over the
    frozen envelope -> the frozen per-ball trajectories -> the REAL US-F4
    bounce estimator + zone classification -> the same
    :func:`bounce_estimate_fields` loader the agreement leg uses. The result
    is deterministic end to end, so pooled agreement below the bound is a
    genuine pipeline regression (US-L4 MV: a broken batch must trip the alarm).
    """
    session_id = ensure_canary_session(ctx)
    rederive_session(ctx, session_id, actor=CANARY_ACTOR)
    run_events_stage(ctx, session_id)
    _seed_canary_tracks(ctx, session_id)
    estimate_session_bounces(ctx, session_id)
    with ctx.session_factory() as db:
        return bounce_estimate_fields(db, session_id)


def _manual_truth(db: Session, session_id: uuid.UUID) -> dict[int, dict[str, object]]:
    """``ground_truth_eligible`` manual tags as plain comparison dicts (US-B4)."""
    truth: dict[int, dict[str, object]] = {}
    rows = db.scalars(
        select(BallTag).where(
            BallTag.session_id == session_id, BallTag.ground_truth_eligible.is_(True)
        )
    )
    for tag in rows:
        truth[tag.ball_no] = {
            "line": tag.line.value,
            "length": tag.length.value,
            "shot": tag.shot.value,
            "footwork": tag.footwork.value,
            "contact": tag.contact.value,
            "outcome": tag.outcome.value,
            "control": tag.control,
        }
    return truth


def _valid_ball_nos(db: Session, session_id: uuid.UUID) -> tuple[int, ...]:
    """Balls that exist per US-D4: rejected events are not balls."""
    return tuple(
        sorted(
            db.scalars(
                select(BallEvent.ball_no).where(
                    BallEvent.session_id == session_id, BallEvent.valid.is_(True)
                )
            )
        )
    )


def _luma_samples(db: Session, session_id: uuid.UUID) -> dict[str, list[float]]:
    """Exposure proxy samples from video probes, when a producer recorded them."""
    luma: dict[str, list[float]] = {}
    for video in db.scalars(select(Video).where(Video.session_id == session_id)):
        samples = (video.probe or {}).get("mean_luma_samples")
        if isinstance(samples, list):
            luma.setdefault(video.camera_id, []).extend(
                float(value) for value in samples if isinstance(value, int | float)
            )
    return luma


def _calibration_age_days(db: Session, session: SessionRow) -> float | None:
    """Calibration age at session time, or ``None`` when none is attached."""
    if session.calibration_id is None:
        return None
    record = db.get(Calibration, session.calibration_id)
    if record is None:
        return None
    return max(0.0, float((session.session_date - record.created_at.date()).days))


def load_quality_inputs(db: Session, session: SessionRow) -> SessionQualityInputs:
    """Default quality-inputs loader over the session's stored artifacts (US-L4)."""
    session_id = session.id
    offsets: dict[int, dict[str, float]] = {}
    clip_rows = db.scalars(
        select(Clip).where(Clip.session_id == session_id, Clip.status == ClipStatus.CUT)
    )
    for clip in clip_rows:
        offsets.setdefault(clip.ball_no, {})[clip.camera_id] = float(clip.start_ms)
    pose: dict[int, dict[str, float]] = {}
    for pose_row in db.scalars(select(PoseTrack).where(PoseTrack.session_id == session_id)):
        pose.setdefault(pose_row.ball_no, {})[pose_row.camera_id] = pose_row.availability
    track: dict[int, dict[str, float]] = {}
    for track_row in db.scalars(select(BallTrack).where(BallTrack.session_id == session_id)):
        track.setdefault(track_row.ball_no, {})[track_row.camera_id] = track_row.coverage
    return SessionQualityInputs(
        ball_nos=_valid_ball_nos(db, session_id),
        event_offsets_ms=offsets,
        luma_samples=_luma_samples(db, session_id),
        pose_availability=pose,
        track_coverage=track,
        calibration_age_days=_calibration_age_days(db, session),
        calibration_suspect=session.calibration_suspect,
    )


def _sample_candidates(db: Session, session: SessionRow) -> list[SampleCandidate]:
    """Untagged valid balls, stratified by session and auto length zone."""
    tagged = set(db.scalars(select(BallTag.ball_no).where(BallTag.session_id == session.id)))
    lengths: dict[int, str] = {}
    for row in db.scalars(select(BounceEstimate).where(BounceEstimate.session_id == session.id)):
        lengths[row.ball_no] = row.length.value if row.length is not None else "?"
    return [
        SampleCandidate(
            session_id=str(session.id),
            ball_no=ball_no,
            stratum=f"{session.id}:{lengths.get(ball_no, '?')}",
        )
        for ball_no in _valid_ball_nos(db, session.id)
        if ball_no not in tagged
    ]


def _persist_alert(
    db: Session,
    payload: Mapping[str, Any],
    *,
    dedupe_key: str,
    session_id: uuid.UUID | None = None,
) -> bool:
    """Insert one alert row unless its ``dedupe_key`` already exists (idempotency)."""
    code = str(payload["code"])
    for existing in db.scalars(select(Alert).where(Alert.code == code)):
        if existing.detail.get("dedupe_key") == dedupe_key:
            return False
    detail = dict(payload["detail"])
    detail["dedupe_key"] = dedupe_key
    db.add(
        Alert(
            audience=AlertAudience(str(payload["audience"])),
            code=code,
            severity=str(payload["severity"]),
            detail=detail,
            session_id=session_id,
        )
    )
    return True


def _quality_alert_payload(session: SessionRow, score_dict: dict[str, Any]) -> dict[str, Any]:
    return {
        "audience": AlertAudience.PARENT.value,
        "code": PARENT_QUALITY_CODE,
        "severity": "warning",
        "detail": {"session_id": str(session.id), **score_dict},
    }


def _sample_alert_payload(week_key: str, sample: tuple[SampleCandidate, ...]) -> dict[str, Any]:
    return {
        "audience": AlertAudience.DEVELOPER.value,
        "code": SAMPLE_CODE,
        "severity": "info",
        "detail": {
            "week_start": week_key,
            "balls": [
                {"session_id": c.session_id, "ball_no": c.ball_no, "stratum": c.stratum}
                for c in sample
            ],
        },
    }


def _resolve_canary_observed(
    ctx: WorkerContext,
    canary_observed: Mapping[int, Mapping[str, object]] | None,
    canary_deriver: CanaryDeriver,
) -> tuple[Mapping[int, Mapping[str, object]], str | None]:
    """(observations, derivation error): explicit observations win, else derive.

    A failed derivation returns ``({}, error)`` — nothing comparable, so the
    canary leg fires ``drift_canary_missing`` carrying the error; the rest of
    the weekly monitor still runs (a broken canary must never silence the
    quality/agreement legs).
    """
    if canary_observed is not None:
        return canary_observed, None
    try:
        return canary_deriver(ctx), None
    except Exception as exc:  # any broken derivation IS the alarm, never a crash
        return {}, f"{type(exc).__name__}: {exc}"


def run_weekly_drift_monitor(  # noqa: PLR0913  (public seam: injectable loaders/derivers)
    ctx: WorkerContext,
    week_start: date,
    *,
    auto_fields: AutoFieldsLoader = bounce_estimate_fields,
    canary_observed: Mapping[int, Mapping[str, object]] | None = None,
    canary_deriver: CanaryDeriver = derive_canary_observed,
    quality_inputs: QualityInputsLoader = load_quality_inputs,
) -> DriftMonitorSummary:
    """Run the weekly drift + honesty monitor over one week of sessions (US-L4).

    ``week_start`` names the week just ended (any anchor date the scheduler
    uses); it keys the dedupe keys, so re-running the same week is a no-op.
    Quality scoring and sample selection cover ``week_start``'s window; the
    agreement/shortfall legs compare the PRIOR week's ground-truth-eligible
    tags — the sample posted by the previous run, tagged during this one — so
    the tag-then-compare loop actually closes. The canary leg runs every time:
    explicit ``canary_observed`` wins, otherwise ``canary_deriver`` re-derives
    the frozen canary session through the pipeline path
    (:func:`derive_canary_observed`); a derivation failure becomes the
    ``drift_canary_missing`` alarm with the error recorded — never a crash of
    the whole monitor and never a silent pass. Alerts route per the pinned
    audiences: drift/canary/sample to developers, low-quality honesty banners
    to parents.
    """
    week_end = week_start + timedelta(days=7)
    compared_week_start = week_start - timedelta(days=7)
    week_key = week_start.isoformat()
    compared_key = compared_week_start.isoformat()
    written = 0
    skipped = 0
    observed, canary_error = _resolve_canary_observed(ctx, canary_observed, canary_deriver)
    with session_scope(ctx.session_factory) as db:
        sessions = list(
            db.scalars(
                select(SessionRow)
                .where(SessionRow.session_date >= week_start, SessionRow.session_date < week_end)
                .order_by(SessionRow.session_date, SessionRow.id)
            )
        )
        compared_sessions = list(
            db.scalars(
                select(SessionRow)
                .where(
                    SessionRow.session_date >= compared_week_start,
                    SessionRow.session_date < week_start,
                )
                .order_by(SessionRow.session_date, SessionRow.id)
            )
        )
        manual_pool: dict[tuple[str, int], dict[str, object]] = {}
        auto_pool: dict[tuple[str, int], dict[str, object]] = {}
        for compared_session in compared_sessions:
            key = str(compared_session.id)
            for ball_no, fields in _manual_truth(db, compared_session.id).items():
                manual_pool[(key, ball_no)] = fields
            for ball_no, fields in auto_fields(db, compared_session.id).items():
                auto_pool[(key, ball_no)] = fields
        candidates: list[SampleCandidate] = []
        for session in sessions:
            candidates.extend(_sample_candidates(db, session))
            score = score_session(quality_inputs(db, session))
            if score.banner is not None:
                persisted = _persist_alert(
                    db,
                    _quality_alert_payload(session, score.as_dict()),
                    dedupe_key=f"{PARENT_QUALITY_CODE}:{session.id}",
                    session_id=session.id,
                )
                written, skipped = (written + persisted, skipped + (not persisted))
        paired = paired_ball_count(manual_pool, auto_pool)
        alerts = drift_alerts(field_agreement(manual_pool, auto_pool))
        shortfall = sample_shortfall_alert(paired)
        if shortfall is not None:
            alerts.append(shortfall)
        for alert in alerts:  # agreement + shortfall are about the compared week
            alert["detail"]["week_start"] = week_key
            alert["detail"]["compared_week_start"] = compared_key
        # The canary leg runs EVERY week: no comparable observations — a
        # failed or empty re-derivation — is a critical failure, not a pass.
        canary = canary_alerts(observed)
        if canary_error is not None:
            for alert in canary:
                alert["detail"]["derivation_error"] = canary_error
        alerts.extend(canary)
        for alert in alerts:
            field = alert["detail"].get("field")
            suffix = f":{field}" if field is not None else ""
            persisted = _persist_alert(db, alert, dedupe_key=f"{alert['code']}:{week_key}{suffix}")
            written, skipped = (written + persisted, skipped + (not persisted))
        sample = select_weekly_sample(candidates, week_start)
        if sample:
            persisted = _persist_alert(
                db,
                _sample_alert_payload(week_key, sample),
                dedupe_key=f"{SAMPLE_CODE}:{week_key}",
            )
            written, skipped = (written + persisted, skipped + (not persisted))
    return DriftMonitorSummary(
        week_start=week_key,
        compared_week_start=compared_key,
        sessions=len(sessions),
        paired_balls=paired,
        alerts_written=written,
        alerts_skipped=skipped,
        sampled=tuple((c.session_id, c.ball_no) for c in sample),
    )
