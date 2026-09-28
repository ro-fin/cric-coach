"""Call-adapters wiring the CV media + bowling stages into the DAG (US-J1/US-L1).

The pipeline runner (:mod:`cricai_worker.pipeline`) invokes every stage as
``fn(ctx, session_id)`` but the CV entry points have richer real signatures.
These adapters derive the missing inputs from stored data — probe payloads,
uploaded videos, cut clips, calibrations and the production model registry —
and hand them to the real jobs:

- ``events`` (:func:`run_events_stage`) — motion energy + fps for
  :func:`cricai_worker.detect_events.run_detection`. A probe payload already
  carrying a per-frame ``motion_energy`` envelope wins (the enrichment seam
  the drift canary also feeds); otherwise the envelope is computed from the
  stored reference-camera video (mean absolute gray-frame difference,
  peak-normalized to the segmenter's roughly-normalized input contract).
  Audio onsets ride along from ``probe["audio_onsets_ms"]`` when a producer
  recorded them.
- ``clips`` (:func:`run_clips_stage`) — a caching store-backed source
  resolver for :func:`cricai_worker.cut_clips.cut_session_clips`. A run that
  stopped early (ffmpeg unavailable) FAILS the stage: an infrastructure gap
  is an honest failure the trace must show, never a quiet success.
- ``pose`` (:func:`run_pose_stage`) — provider + frames for
  :func:`cricai_worker.extract_pose.extract_session_pose`. Frames decode from
  the per-ball cut clips; the provider is the real MediaPipe adapter when the
  ``pose`` extra and a model asset (:data:`ENV_POSE_MODEL_ASSET`) are
  available, else the deterministic fake WITH the fallback reason recorded in
  the stage output — and ``model_name='fake-pose'`` lands on every persisted
  row, so provenance is honest at both levels.
- ``detect_track`` (:func:`run_detect_track_stage`) — detection provider for
  :func:`cricai_worker.track_balls.track_session_balls` resolved from the
  production model registry (:data:`DETECTOR_MODEL_NAME` at
  ``stage=production``; weights from the object store through the
  ultralytics adapter, frames decoded from the reference camera's evidence
  video). Every reason the production model could not be used is recorded in
  the stage output beside the fake fallback that replaces it.
- ``metrics`` (:func:`run_metrics_stage`) — fusion inputs for
  :func:`cricai_worker.fuse_contacts.fuse_session_contacts`. The bat modality
  uses the production detector when available and is otherwise honestly
  ABSENT (``detector=None`` -> ``bat_path`` null-with-reason): fake
  detections must never masquerade as contact evidence (US-F5).
- ``bowling_action`` (:func:`run_bowling_action_stage`) — pose provider +
  clip frames resolver for
  :func:`cricai_worker.bowling_action.analyze_session_bowling_action`
  (US-I2/I3), built exactly like the ``pose`` stage's: the real MediaPipe
  provider when configured, else the deterministic fake with the fallback
  reason recorded; frames decode from the C5 cut clips.
- ``bowling_flight`` (:func:`run_bowling_flight_stage`) —
  :func:`cricai_worker.bowling_flight.analyze_session_flight` over the stored
  tracks/bounces/targets (US-I4/I5).
- ``classify_variations`` (:func:`run_classify_variations_stage`) —
  :func:`cricai_worker.classify_variations.classify_session_variations` over
  the coach-labeled deliveries (US-I6; the honest deterministic classifier
  until a trained model clears the T3 gate).

The three bowling stages run for ``SessionType.BOWLING`` sessions only; for
every other session type each returns the honest gated no-op payload
:data:`SKIPPED_NOT_BOWLING` — visible in the trace, never silent.

Honest-degradation vocabulary (US-J1 "degraded but honest"): a missing INPUT
(no evidence footage, no usable fps, undecodable bytes, ffmpeg gone) raises
:class:`StageInputError`, which the runner records as an honest FAILED stage
row naming the gap; a missing MODEL degrades to the documented fallback with
the reason recorded in the stage output — never silent either way.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
from cricai_data.enums import EVIDENCE_STATUSES, ClipStatus, ModelStage, SessionType
from cricai_data.models import Clip, ModelRun, ModelVersion, Video
from cricai_data.models import Session as SessionRow
from cricai_data.storage import StorageError
from cricai_vision.detect import DetectionProvider, FakeDetectionProvider
from cricai_vision.mediapipe_pose import MediaPipePoseProvider
from cricai_vision.pose import FakePoseProvider, PoseProvider
from cricai_vision.yolo_detect import YoloDetectionProvider
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_worker.bowling_action import analyze_session_bowling_action
from cricai_worker.bowling_flight import analyze_session_flight
from cricai_worker.classify_variations import classify_session_variations
from cricai_worker.context import WorkerContext
from cricai_worker.cut_clips import cut_session_clips, store_source_resolver
from cricai_worker.detect_events import run_detection
from cricai_worker.extract_pose import FramesResolver, extract_session_pose
from cricai_worker.fuse_contacts import FuseInputs, fuse_session_contacts
from cricai_worker.track_balls import track_session_balls

_LOG = logging.getLogger(__name__)

#: Side-on reference camera: the motion-energy / detection source (US-D1/F3).
REFERENCE_CAMERA = "C1"

#: Registry identity of the trained ball detector (scripts/train_detector.py
#: convention); its ``stage=production`` version is the real-model wiring.
DETECTOR_MODEL_NAME = "ball-detector"

#: Path of a MediaPipe pose landmarker ``.task`` bundle; unset means the
#: deterministic fake provider (recorded loudly in the stage output).
ENV_POSE_MODEL_ASSET = "CRICAI_POSE_MODEL_ASSET"

#: Probe enrichment seam: a producer (or the drift canary) may store the
#: per-frame motion-energy envelope / audio onset times on ``Video.probe``.
PROBE_MOTION_ENERGY_KEY = "motion_energy"
PROBE_AUDIO_ONSETS_KEY = "audio_onsets_ms"


class StageInputError(RuntimeError):
    """A stage input gap the adapter cannot honestly work around.

    Raised so the runner records an honest FAILED stage row naming the gap
    (missing footage, unusable metadata, undecodable bytes) — downstream
    stages then SKIP with the reason instead of running on invented inputs.
    """


def _require_session(db: OrmSession, session_id: uuid.UUID) -> None:
    """Missing sessions raise ``LookupError`` (the runner's convention)."""
    if db.get(SessionRow, session_id) is None:
        raise LookupError(f"session {session_id} not found")


def _evidence_videos(db: OrmSession, session_id: uuid.UUID) -> dict[str, Video]:
    """Camera id -> newest evidence-status Video (private mirror of cut_clips)."""
    rows = db.scalars(
        select(Video)
        .where(Video.session_id == session_id, Video.status.in_(sorted(EVIDENCE_STATUSES)))
        .order_by(Video.created_at, Video.id)
    )
    return {video.camera_id: video for video in rows}  # newest row wins


def _best_fps(video: Video) -> float | None:
    """Best-known frame rate: the measured probe wins over the client claim."""
    probed = (video.probe or {}).get("fps")
    fps = probed if isinstance(probed, int | float) and not isinstance(probed, bool) else None
    if fps is None:
        fps = video.claimed_fps
    if fps is None or fps <= 0:
        return None
    return float(fps)


def _reference_video(session_id: uuid.UUID, videos: dict[str, Video]) -> Video:
    """The reference camera's evidence video, else the lowest evidence camera."""
    if not videos:
        raise StageInputError(
            f"no evidence footage for session {session_id}: nothing to detect events on"
        )
    if REFERENCE_CAMERA in videos:
        return videos[REFERENCE_CAMERA]
    return videos[min(videos)]


def _stored_motion_energy(video: Video) -> list[float] | None:
    """The probe-recorded envelope, ``None`` when absent; malformed fails loud."""
    raw = (video.probe or {}).get(PROBE_MOTION_ENERGY_KEY)
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise StageInputError(
            f"probe {PROBE_MOTION_ENERGY_KEY!r} for camera {video.camera_id} must be a list,"
            f" got {type(raw).__name__}"
        )
    envelope: list[float] = []
    for index, value in enumerate(raw):
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise StageInputError(
                f"probe {PROBE_MOTION_ENERGY_KEY!r} for camera {video.camera_id} has a"
                f" non-numeric sample at index {index}: {value!r}"
            )
        envelope.append(float(value))
    return envelope


def _stored_audio_onsets(video: Video) -> list[int] | None:
    """Probe-recorded audio onsets, ``None`` when absent; malformed fails loud."""
    raw = (video.probe or {}).get(PROBE_AUDIO_ONSETS_KEY)
    if raw is None:
        return None
    if not isinstance(raw, list) or any(
        isinstance(value, bool) or not isinstance(value, int) for value in raw
    ):
        raise StageInputError(
            f"probe {PROBE_AUDIO_ONSETS_KEY!r} for camera {video.camera_id} must be a list"
            " of integer milliseconds"
        )
    return [int(value) for value in raw]


def _computed_motion_energy(ctx: WorkerContext, camera_id: str, object_key: str) -> list[float]:
    """Per-frame motion energy of a stored video: mean |gray diff|, peak-normalized.

    The segmenter expects a roughly normalized envelope
    (:class:`cricai_vision.events.DetectorConfig` defaults), so the raw
    difference energy is divided by its peak; an all-still video stays
    all-zero (no events — honest, not an error).
    """
    with tempfile.TemporaryDirectory(prefix="cricai-events-") as tmp:
        source = Path(tmp) / "source.video"
        try:
            ctx.store.write_to_path(object_key, source)
        except StorageError as exc:
            raise StageInputError(
                f"source video for camera {camera_id} unavailable in the object store: {exc}"
            ) from exc
        capture = cv2.VideoCapture(str(source))
        try:
            if not capture.isOpened():
                raise StageInputError(f"source video for camera {camera_id} cannot be decoded")
            energy: list[float] = []
            previous = None
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                if previous is None:
                    energy.append(0.0)  # one value per frame; the first has no diff
                else:
                    energy.append(float(cv2.absdiff(gray, previous).mean()) / 255.0)
                previous = gray
        finally:
            capture.release()
    if not energy:
        raise StageInputError(f"source video for camera {camera_id} has no decodable frames")
    peak = max(energy)
    if peak <= 0.0:
        return energy  # perfectly still footage: an all-quiet envelope, honestly
    return [value / peak for value in energy]


def run_events_stage(ctx: WorkerContext, session_id: uuid.UUID) -> dict[str, Any]:
    """``events`` stage: derive motion energy + fps, run US-D1 detection.

    Raises :class:`LookupError` for a missing session (runner convention),
    :class:`StageInputError` for input gaps (no evidence footage, no usable
    fps, malformed probe seam data, undecodable bytes), and lets
    :class:`~cricai_worker.detect_events.DetectionBlockedError` propagate —
    a blocked replace is an honest FAILED row, never a silent skip.
    """
    with ctx.session_factory() as db:
        _require_session(db, session_id)
        video = _reference_video(session_id, _evidence_videos(db, session_id))
        camera_id = video.camera_id
        object_key = video.object_key
        fps = _best_fps(video)
        stored = _stored_motion_energy(video)
        onsets = _stored_audio_onsets(video)
    if fps is None:
        raise StageInputError(f"no usable fps for camera {camera_id} (neither probed nor claimed)")
    if stored is not None:
        motion_energy, energy_source = stored, "probe"
    else:
        motion_energy, energy_source = _computed_motion_energy(ctx, camera_id, object_key), "video"
    summary = run_detection(ctx, session_id, motion_energy, fps, audio_onsets_ms=onsets)
    return {
        "camera_id": camera_id,
        "fps": fps,
        "frames": len(motion_energy),
        "motion_energy_source": energy_source,
        "audio_onsets": None if onsets is None else len(onsets),
        "detector_version": summary.detector_version,
        "detected": summary.detected,
        "replaced": summary.replaced,
        "preserved": summary.preserved,
        "first_ball_no": summary.first_ball_no,
    }


def run_clips_stage(ctx: WorkerContext, session_id: uuid.UUID) -> dict[str, Any]:
    """``clips`` stage: cut per-ball clips with a store-backed source resolver.

    A run stopped early by ffmpeg unavailability raises
    :class:`StageInputError` so the stage FAILS honestly — its PENDING rows
    resume on the next run (US-D2 crash-resume) instead of hiding behind a
    green stage row.
    """
    with tempfile.TemporaryDirectory(prefix="cricai-clip-sources-") as tmp:
        summary = cut_session_clips(
            ctx, session_id, source_resolver=store_source_resolver(ctx.store, Path(tmp))
        )
    if summary.stopped_early:
        raise StageInputError(
            "ffmpeg unavailable: clip cutting stopped early; the remaining rows stay"
            " PENDING and resume on the next run"
        )
    return {
        "balls": summary.balls,
        "cameras": list(summary.cameras),
        "cut": summary.cut,
        "skipped": summary.skipped,
        "gaps": summary.gaps,
        "failed": summary.failed,
        "pending": summary.pending,
    }


def resolve_pose_provider() -> tuple[PoseProvider, str, str | None]:
    """(provider, ``model_name:model_version`` provenance, fallback reason).

    The real MediaPipe adapter needs the ``pose`` extra installed and a
    landmarker asset configured (:data:`ENV_POSE_MODEL_ASSET`); anything less
    degrades to the deterministic fake with the reason recorded — the fake's
    ``model_name`` also lands on every ``pose_tracks`` row, so the fallback is
    visible in the data itself, never silent.
    """
    asset = os.environ.get(ENV_POSE_MODEL_ASSET)
    if not asset:
        fake = FakePoseProvider()
        return (
            fake,
            f"{fake.model_name}:{fake.model_version}",
            f"no pose model asset configured (set {ENV_POSE_MODEL_ASSET} to a MediaPipe"
            " pose landmarker .task bundle)",
        )
    try:
        provider = MediaPipePoseProvider(model_asset_path=asset)
    except ImportError as exc:
        fake = FakePoseProvider()
        return fake, f"{fake.model_name}:{fake.model_version}", f"mediapipe unavailable: {exc}"
    return provider, f"{provider.model_name}:{provider.model_version}", None


def _decode_frames(ctx: WorkerContext, object_key: str) -> tuple[list[Any], float] | None:
    """All BGR frames of a stored video plus its container fps, or ``None``.

    ``None`` — unavailable bytes or undecodable content — is a loud per-ball
    footage gap for the caller's summary, never a fabricated frame (US-E1).
    """
    with tempfile.TemporaryDirectory(prefix="cricai-pose-frames-") as tmp:
        source = Path(tmp) / "clip.video"
        try:
            ctx.store.write_to_path(object_key, source)
        except StorageError as exc:
            _LOG.warning("clip %s unavailable in the object store: %s", object_key, exc)
            return None
        capture = cv2.VideoCapture(str(source))
        try:
            if not capture.isOpened():
                _LOG.warning("clip %s cannot be decoded", object_key)
                return None
            container_fps = float(capture.get(cv2.CAP_PROP_FPS))
            frames: list[Any] = []
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                frames.append(frame)
        finally:
            capture.release()
    if not frames:
        _LOG.warning("clip %s decoded to zero frames", object_key)
        return None
    return frames, container_fps


def clip_frames_resolver(ctx: WorkerContext) -> FramesResolver:
    """A :data:`~cricai_worker.extract_pose.FramesResolver` over the cut clips.

    Resolves one ``(session, ball, camera)`` to the decoded frames of its CUT
    clip; the frame rate prefers the camera video's probe/claim (the clip is a
    re-encode of that source) and falls back to the clip container's own fps.
    Any gap — no clip row, missing bytes, undecodable content, no usable rate
    — returns ``None`` so extract_pose reports the ball/camera loudly.
    """

    def resolve(
        session_id: uuid.UUID, ball_no: int, camera_id: str
    ) -> tuple[Sequence[Any], float] | None:
        with ctx.session_factory() as db:
            clip = db.scalar(
                select(Clip).where(
                    Clip.session_id == session_id,
                    Clip.ball_no == ball_no,
                    Clip.camera_id == camera_id,
                    Clip.status == ClipStatus.CUT,
                )
            )
            if clip is None or clip.object_key is None:
                return None
            object_key = clip.object_key
            video = _evidence_videos(db, session_id).get(camera_id)
            source_fps = None if video is None else _best_fps(video)
        decoded = _decode_frames(ctx, object_key)
        if decoded is None:
            return None
        frames, container_fps = decoded
        fps = source_fps if source_fps is not None else container_fps
        if fps <= 0:
            _LOG.warning("no usable frame rate for clip %s", object_key)
            return None
        return frames, fps

    return resolve


def run_pose_stage(ctx: WorkerContext, session_id: uuid.UUID) -> dict[str, Any]:
    """``pose`` stage: extract per-ball pose tracks from the cut clips (US-E1)."""
    provider, provenance, fallback = resolve_pose_provider()
    summary = extract_session_pose(
        ctx, session_id, provider=provider, frames_resolver=clip_frames_resolver(ctx)
    )
    return {
        "provider": provenance,
        "provider_fallback": fallback,
        "extracted": summary.extracted,
        "missing": [[ball_no, camera_id] for ball_no, camera_id in summary.missing],
        "low_availability": [
            [ball_no, camera_id] for ball_no, camera_id in summary.low_availability
        ],
    }


@dataclass(frozen=True)
class VideoFrameSource:
    """Decoded BGR frames of one stored video, one per frame-grid slot.

    Implements the :class:`cricai_vision.yolo_detect.FrameSource` protocol
    over a local file: seek to the window's first grid slot, read one frame
    per slot. A window past the footage end returns the frames that exist —
    the ultralytics adapter then fails that ball loudly (count mismatch)
    rather than detecting on fabricated frames.
    """

    source: Path

    def frames(self, *, start_ms: float, end_ms: float, fps: float) -> Sequence[Any]:
        frame_ms = 1000.0 / fps
        n_frames = int((end_ms - start_ms) / frame_ms) + 1
        first_frame = round(start_ms / frame_ms)
        capture = cv2.VideoCapture(str(self.source))
        try:
            if not capture.isOpened():
                raise StageInputError(f"detection source {self.source.name} cannot be decoded")
            capture.set(cv2.CAP_PROP_POS_FRAMES, max(first_frame, 0))
            frames: list[Any] = []
            for _ in range(n_frames):
                ok, frame = capture.read()
                if not ok:
                    break
                frames.append(frame)
        finally:
            capture.release()
        return frames


class _ProductionModelUnavailable(Exception):
    """The production detector cannot be used; the message is the honest reason."""


def _production_weights_key(ctx: WorkerContext, label: str, report_key: str) -> str:
    """The weights object key named by a run's eval-report artifact."""
    try:
        report = json.loads(ctx.store.get(report_key))
    except StorageError as exc:
        raise _ProductionModelUnavailable(
            f"{label}: eval report {report_key!r} missing from the object store"
        ) from exc
    except json.JSONDecodeError as exc:
        raise _ProductionModelUnavailable(
            f"{label}: eval report {report_key!r} is not valid JSON"
        ) from exc
    artifacts = report.get("artifacts") if isinstance(report, dict) else None
    weights_key = artifacts.get("weights_key") if isinstance(artifacts, dict) else None
    if not isinstance(weights_key, str):
        raise _ProductionModelUnavailable(f"{label}: eval report names no weights artifact")
    return weights_key


def _fetch(ctx: WorkerContext, key: str, dest: Path, *, what: str) -> Path:
    """Download one store object to ``dest`` or raise the honest reason."""
    try:
        ctx.store.write_to_path(key, dest)
    except StorageError as exc:
        raise _ProductionModelUnavailable(f"{what} {key!r} missing from the object store") from exc
    return dest


def _build_production_detector(
    ctx: WorkerContext, session_id: uuid.UUID, workdir: Path
) -> DetectionProvider:
    """Construct the registry's production detector or raise the honest reason."""
    with ctx.session_factory() as db:
        version = db.scalar(
            select(ModelVersion)
            .where(
                ModelVersion.model_name == DETECTOR_MODEL_NAME,
                ModelVersion.stage == ModelStage.PRODUCTION,
            )
            .order_by(ModelVersion.created_at.desc(), ModelVersion.id.desc())
            .limit(1)
        )
        if version is None:
            raise _ProductionModelUnavailable(
                f"no production {DETECTOR_MODEL_NAME!r} version in the model registry"
            )
        label = f"{version.model_name}:{version.version}"
        run = db.get(ModelRun, version.run_id)
        report_key = None if run is None else run.report_key
        videos = _evidence_videos(db, session_id)
        source_key = None if not videos else _reference_video(session_id, videos).object_key
    if report_key is None:
        raise _ProductionModelUnavailable(
            f"{label}: run has no eval-report artifact naming its weights"
        )
    if source_key is None:
        raise _ProductionModelUnavailable(
            f"{label}: no evidence footage to decode detection frames from"
        )
    weights_key = _production_weights_key(ctx, label, report_key)
    weights_path = _fetch(
        ctx, weights_key, workdir / Path(weights_key).name, what=f"{label}: weights"
    )
    source_path = _fetch(
        ctx,
        source_key,
        workdir / "detection-source.video",
        what=f"{label}: reference camera source video",
    )
    try:
        return YoloDetectionProvider(
            weights_path=weights_path, frame_source=VideoFrameSource(source_path)
        )
    except ImportError as exc:
        raise _ProductionModelUnavailable(f"{label}: {exc}") from exc


def production_detector(
    ctx: WorkerContext, session_id: uuid.UUID, workdir: Path
) -> tuple[DetectionProvider | None, str | None]:
    """The registry's production ball detector, or ``(None, reason)``.

    Resolution chain, every miss yielding its own honest reason: the
    ``stage=production`` :data:`DETECTOR_MODEL_NAME` version -> its run's
    eval-report artifact -> the report's ``artifacts.weights_key`` -> the
    weights bytes -> the reference camera's evidence video (the frame source)
    -> the ultralytics adapter. ``workdir`` must outlive the returned
    provider (it holds the downloaded weights + source video).
    """
    try:
        return _build_production_detector(ctx, session_id, workdir), None
    except _ProductionModelUnavailable as exc:
        return None, str(exc)


def run_detect_track_stage(ctx: WorkerContext, session_id: uuid.UUID) -> dict[str, Any]:
    """``detect_track`` stage: track every ball with the best available detector.

    The production registry model wins; anything less falls back to the
    deterministic :class:`~cricai_vision.detect.FakeDetectionProvider` with
    the reason recorded in the stage output (``detector_fallback``) — the
    documented US-F3 degradation, now loud instead of silent.
    """
    with tempfile.TemporaryDirectory(prefix="cricai-detect-") as tmp:
        provider, fallback = production_detector(ctx, session_id, Path(tmp))
        detector: DetectionProvider = provider if provider is not None else FakeDetectionProvider()
        summary = track_session_balls(ctx, session_id, provider=detector)
    return {
        "detector": detector.version,
        "detector_fallback": fallback,
        "tracked": summary.tracked,
        "skipped_cameras": [list(pair) for pair in summary.skipped_cameras],
        "pixel_only_cameras": [list(pair) for pair in summary.pixel_only_cameras],
        "low_coverage": [list(pair) for pair in summary.low_coverage],
        "failed": [list(item) for item in summary.failed],
    }


#: Honest gated no-op payload of the bowling stages on non-bowling sessions:
#: the trace row says WHY nothing was written, never a silent green (US-J1).
SKIPPED_NOT_BOWLING: dict[str, Any] = {"skipped": "not a bowling session"}


def _is_bowling_session(ctx: WorkerContext, session_id: uuid.UUID) -> bool:
    """Session-type gate for the bowling stages; missing sessions raise
    ``LookupError`` (the runner's convention)."""
    with ctx.session_factory() as db:
        session = db.get(SessionRow, session_id)
        if session is None:
            raise LookupError(f"session {session_id} not found")
        return session.session_type is SessionType.BOWLING


def run_bowling_action_stage(ctx: WorkerContext, session_id: uuid.UUID) -> dict[str, Any]:
    """``bowling_action`` stage: release + checkpoints from the C5 clips (US-I2/I3).

    Builds the production inputs exactly like :func:`run_pose_stage`: the pose
    provider from :func:`resolve_pose_provider` (real MediaPipe when
    configured, else the deterministic fake with the reason recorded) and the
    frames from the per-ball cut clips via :func:`clip_frames_resolver` —
    :data:`cricai_worker.bowling_action.DEFAULT_CAMERA` (C5) is the release
    view. Non-bowling sessions return :data:`SKIPPED_NOT_BOWLING` (bowling
    metrics belong only to ``mode == "bowling"`` records, contract #1).
    """
    if not _is_bowling_session(ctx, session_id):
        return dict(SKIPPED_NOT_BOWLING)
    provider, provenance, fallback = resolve_pose_provider()
    summary = analyze_session_bowling_action(
        ctx, session_id, provider=provider, frames_resolver=clip_frames_resolver(ctx)
    )
    return {
        "provider": provenance,
        "provider_fallback": fallback,
        "analyzed": summary.analyzed_count,
        "undetected": [
            [ball.ball_no, ball.detection_reason]
            for ball in summary.analyzed
            if ball.detection_reason is not None
        ],
        "skipped": [[ball_no, reason] for ball_no, reason in summary.skipped],
    }


def run_bowling_flight_stage(ctx: WorkerContext, session_id: uuid.UUID) -> dict[str, Any]:
    """``bowling_flight`` stage: turn/apex/dip/target metrics (US-I4/I5).

    Runs :func:`~cricai_worker.bowling_flight.analyze_session_flight` over the
    stored tracks, bounces (manual marks beat auto estimates, US-F4) and
    declared targets. The payload counts what was measured vs honestly null —
    nulls are data, not failure. Non-bowling sessions return
    :data:`SKIPPED_NOT_BOWLING`.
    """
    if not _is_bowling_session(ctx, session_id):
        return dict(SKIPPED_NOT_BOWLING)
    summary = analyze_session_flight(ctx, session_id)
    return {
        "balls": len(summary.balls),
        "turn_measured": sum(1 for ball in summary.balls if ball.turn_cm is not None),
        "apex_measured": sum(1 for ball in summary.balls if ball.apex_m is not None),
        "dip_measured": sum(1 for ball in summary.balls if ball.dip_flag is not None),
        "targets_scored": sum(1 for ball in summary.balls if ball.target_hit is not None),
    }


def run_classify_variations_stage(ctx: WorkerContext, session_id: uuid.UUID) -> dict[str, Any]:
    """``classify_variations`` stage: predict variations for labeled balls (US-I6).

    Runs :func:`~cricai_worker.classify_variations.classify_session_variations`
    with its honest default classifier (the deterministic fake until a trained
    model clears the ≥80% T3 agreement gate; report surfaces re-apply the gate
    before anything kid-facing). Non-bowling sessions return
    :data:`SKIPPED_NOT_BOWLING`.
    """
    if not _is_bowling_session(ctx, session_id):
        return dict(SKIPPED_NOT_BOWLING)
    summary = classify_session_variations(ctx, session_id)
    return {
        "classified": summary.classified,
        "unclear": summary.unclear,
        "cleared": summary.cleared,
        "skipped": [[ball_no, reason] for ball_no, reason in summary.skipped],
        "agreement_3way": summary.agreement_3way,
        "gate_met": summary.gate_met,
        "classifier_version": summary.classifier_version,
    }


def run_metrics_stage(ctx: WorkerContext, session_id: uuid.UUID) -> dict[str, Any]:
    """``metrics`` stage: fuse contact metrics from the available modalities.

    The bat modality uses the production detector when the registry has one;
    otherwise it is honestly absent (``detector=None`` -> ``bat_path``
    null-with-reason, US-F5) — a fake detector's synthetic bat boxes must
    never become contact evidence. No audio-onset producer exists yet, so the
    audio modality is absent by default (degradation is data, not failure).
    """
    with tempfile.TemporaryDirectory(prefix="cricai-fuse-") as tmp:
        provider, fallback = production_detector(ctx, session_id, Path(tmp))
        summary = fuse_session_contacts(ctx, session_id, inputs=FuseInputs(detector=provider))
    return {
        "detector": None if provider is None else provider.version,
        "detector_fallback": fallback,
        "fused_count": summary.fused_count,
        "fused": [
            {
                "ball_no": ball.ball_no,
                "contact_quality": ball.contact_quality,
                "confidence": ball.confidence,
                "modalities": list(ball.modalities),
                "bat_path": ball.bat_path,
            }
            for ball in summary.fused
        ],
        "skipped": [[ball_no, reason] for ball_no, reason in summary.skipped],
    }
