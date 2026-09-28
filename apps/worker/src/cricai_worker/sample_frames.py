"""Diversity-stratified frame sampling job (US-F1): ball events -> frame_samples.

For every valid :class:`~cricai_data.models.BallEvent` and each camera with
evidence footage (statuses in :data:`cricai_data.enums.EVIDENCE_STATUSES`), the
job picks ``frames_per_ball`` timestamps spread evenly across the ball window
and records one ``frame_samples`` row per frame with full provenance (session,
ball, camera, frame number, timestamp, sampler version).

Diversity strata (US-F1: "diverse: blocks, lighting, speeds") are recorded on
every row's ``stratum`` jsonb so dataset composition stays auditable:

- ``lighting`` — an explicit ``machine_settings["lighting"]`` tag wins; else
  derived from ``Session.started_at`` (hours 07-18 -> ``daylight``, otherwise
  ``artificial``); ``unknown`` when the session never started.
- ``speed_band`` — from ``Session.machine_settings["speed_kph"]``:
  < 80 ``slow``, < 100 ``medium``, else ``fast``; ``unknown`` when absent.
- ``intent`` — the :class:`~cricai_data.models.SessionBlock` whose
  ``[start_s, end_s)`` window covers the ball's start (balls inherit block
  context by timestamp, US-B3); ``unknown`` outside every block.

Frame image extraction goes through an injected :data:`FrameWriter` callable —
tests use a fake that records calls; the ffmpeg-based default
(:func:`ffmpeg_frame_writer`) is factored around the pure
:func:`build_frame_command` argv builder so tests cover command construction
without ever running ffmpeg.

Idempotent + crash-safe: rows upsert on the (session, camera, frame_no) unique
triple and each frame's store write + row commit land together (the
``extract_pose`` per-ball pattern), so a re-run skips frames whose row AND
stored image both exist, rewrites the rest, and never duplicates.

Error containment (the ``cut_clips`` split): sample times come from the ball
event window on the *reference* camera's timeline, so a target camera that
stopped recording earlier can be asked for frames past its end — picks beyond
the camera's known footage duration are dropped loudly (``beyond_footage`` in
the summary), never attempted, and a frame whose extraction fails
(:class:`FrameSamplingError` / :class:`~cricai_vision.ffmpeg.FfmpegError`) is
counted in ``failed`` and retried on the next run while the rest of the run
proceeds — one bad input never wedges the session. Only genuine infrastructure
unavailability (:class:`~cricai_vision.ffmpeg.FfmpegUnavailableError`) aborts
the run early.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from cricai_data.db import session_scope
from cricai_data.enums import EVIDENCE_STATUSES
from cricai_data.models import BallEvent, FrameSample, SessionBlock, Video
from cricai_data.models import Session as SessionRow
from cricai_data.storage import ObjectStore
from cricai_vision.ffmpeg import FfmpegError, FfmpegUnavailableError, run_ffmpeg
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_worker.context import WorkerContext
from cricai_worker.cut_clips import Runner, SourceResolver, store_source_resolver

_LOG = logging.getLogger(__name__)

#: Recorded on every row; bump when the sampling strategy changes (provenance).
SAMPLER_VERSION = "frame-sampler-1"

DEFAULT_FRAMES_PER_BALL = 3

#: Hour-of-day window classified as daylight (home-lab clock convention).
DAYLIGHT_HOURS = range(7, 19)

#: Machine speed bands (kph): below SLOW_MAX slow, below MEDIUM_MAX medium.
SLOW_MAX_KPH = 80.0
MEDIUM_MAX_KPH = 100.0

#: Writes one frame image: (source video row, ts_ms, destination object key).
FrameWriter = Callable[[Video, int, str], None]


class FrameSamplingError(RuntimeError):
    """Invalid sampling inputs or a frame that could not be produced."""


def frame_key(session_id: uuid.UUID, camera_id: str, frame_no: int) -> str:
    """Pinned object-store key of one sampled frame image (US-F1 layout)."""
    if frame_no < 0:
        raise FrameSamplingError(f"frame_no must be >= 0, got {frame_no}")
    return f"sessions/{session_id}/frames/{camera_id}/frame-{frame_no:06d}.jpg"


def lighting_stratum(started_at: datetime | None, machine_settings: dict[str, Any] | None) -> str:
    """Lighting tag: explicit metadata wins, else session start hour, else unknown."""
    tagged = (machine_settings or {}).get("lighting")
    if isinstance(tagged, str) and tagged:
        return tagged
    if started_at is None:
        return "unknown"
    return "daylight" if started_at.hour in DAYLIGHT_HOURS else "artificial"


def speed_band(machine_settings: dict[str, Any] | None) -> str:
    """Speed band from the session's machine settings; unknown when untagged."""
    speed = (machine_settings or {}).get("speed_kph")
    if isinstance(speed, bool) or not isinstance(speed, int | float):
        return "unknown"
    if speed < SLOW_MAX_KPH:
        return "slow"
    if speed < MEDIUM_MAX_KPH:
        return "medium"
    return "fast"


def _block_intent(blocks: Sequence[SessionBlock], start_ms: int) -> str:
    """Intent of the block covering the ball's start (open end_s covers onward)."""
    start_s = start_ms / 1000.0
    for block in blocks:
        if start_s >= block.start_s and (block.end_s is None or start_s < block.end_s):
            return block.intent.value
    return "unknown"


def _video_fps(video: Video) -> float | None:
    """Best-known frame rate: the measured probe wins over the client claim."""
    probed = (video.probe or {}).get("fps")
    fps = probed if isinstance(probed, int | float) else video.claimed_fps
    if fps is None or fps <= 0:
        return None
    return float(fps)


def _video_duration_ms(video: Video) -> int | None:
    """Best-known media duration (private mirror of cut_clips): probe wins."""
    probed = (video.probe or {}).get("duration_s")
    duration_s = probed if isinstance(probed, int | float) else video.claimed_duration_s
    if duration_s is None:
        return None
    return int(duration_s * 1000)


def _sample_times(start_ms: int, end_ms: int, count: int) -> list[int]:
    """``count`` timestamps spread evenly across [start_ms, end_ms]."""
    if count == 1:
        return [round((start_ms + end_ms) / 2)]
    step = (end_ms - start_ms) / (count - 1)
    return [round(start_ms + step * index) for index in range(count)]


@dataclass(frozen=True)
class FrameSamplingSummary:
    """What one run did: rows ensured, gaps, and the audited strata mix."""

    session_id: str
    sampled: int
    skipped: int
    failed: int  # extraction failed this run; retried on the next run
    beyond_footage: int  # picks past the camera's known footage end: never extractable
    missing_cameras: tuple[str, ...]  # no evidence footage at all
    unusable_cameras: tuple[str, ...]  # footage but no usable frame rate
    strata: tuple[tuple[str, int], ...]  # canonical stratum JSON -> rows ensured


@dataclass(frozen=True)
class _Pick:
    """One frame chosen for a ball, with the stratum it will be recorded under."""

    ball_no: int
    frame_no: int
    ts_ms: int
    stratum: dict[str, str]


@dataclass
class _Run:
    """Per-run state threaded through the camera/ball loops."""

    db: OrmSession
    store: ObjectStore
    session_id: uuid.UUID
    writer: FrameWriter
    session: SessionRow
    events: Sequence[BallEvent]
    blocks: Sequence[SessionBlock]
    frames_per_ball: int
    existing: dict[tuple[str, int], FrameSample] = field(default_factory=dict)
    sampled: int = 0
    skipped: int = 0
    failed: int = 0
    beyond_footage: int = 0
    strata: dict[str, int] = field(default_factory=dict)


def sample_session_frames(
    ctx: WorkerContext,
    session_id: uuid.UUID,
    *,
    frame_writer: FrameWriter,
    frames_per_ball: int = DEFAULT_FRAMES_PER_BALL,
    cameras: Sequence[str] | None = None,
) -> FrameSamplingSummary:
    """Sample labeling frames for every valid ball event x evidence camera.

    ``cameras`` restricts the camera set (default: every camera with evidence
    footage). Cameras without footage or without a usable frame rate are
    reported loudly in the summary — frames are never fabricated.
    """
    if frames_per_ball < 1:
        raise FrameSamplingError(f"frames_per_ball must be >= 1, got {frames_per_ball}")
    missing: list[str] = []
    unusable: list[str] = []
    with session_scope(ctx.session_factory) as db:
        session = db.get(SessionRow, session_id)
        if session is None:
            raise ValueError(f"session not found: {session_id}")
        videos = _evidence_videos(db, session_id)
        run = _build_run(db, ctx.store, session, frame_writer, frames_per_ball)
        for camera_id in list(cameras) if cameras is not None else sorted(videos):
            video = videos.get(camera_id)
            if video is None:
                missing.append(camera_id)
                continue
            fps = _video_fps(video)
            if fps is None:
                unusable.append(camera_id)
                continue
            _sample_camera(run, video, fps)
    return FrameSamplingSummary(
        session_id=str(session_id),
        sampled=run.sampled,
        skipped=run.skipped,
        failed=run.failed,
        beyond_footage=run.beyond_footage,
        missing_cameras=tuple(missing),
        unusable_cameras=tuple(unusable),
        strata=tuple(sorted(run.strata.items())),
    )


def _build_run(
    db: OrmSession,
    store: ObjectStore,
    session: SessionRow,
    writer: FrameWriter,
    frames_per_ball: int,
) -> _Run:
    session_id = session.id
    return _Run(
        db=db,
        store=store,
        session_id=session_id,
        writer=writer,
        session=session,
        frames_per_ball=frames_per_ball,
        events=list(
            db.scalars(
                select(BallEvent)
                .where(BallEvent.session_id == session_id, BallEvent.valid.is_(True))
                .order_by(BallEvent.ball_no)
            )
        ),
        blocks=list(
            db.scalars(
                select(SessionBlock)
                .where(SessionBlock.session_id == session_id)
                .order_by(SessionBlock.block_no)
            )
        ),
        existing={
            (row.camera_id, row.frame_no): row
            for row in db.scalars(select(FrameSample).where(FrameSample.session_id == session_id))
        },
    )


def _evidence_videos(db: OrmSession, session_id: uuid.UUID) -> dict[str, Video]:
    """Camera id -> newest evidence-status Video (private mirror of cut_clips)."""
    rows = db.scalars(
        select(Video)
        .where(Video.session_id == session_id, Video.status.in_(sorted(EVIDENCE_STATUSES)))
        .order_by(Video.created_at, Video.id)
    )
    return {video.camera_id: video for video in rows}  # newest row wins


def _sample_camera(run: _Run, video: Video, fps: float) -> None:
    duration_ms = _video_duration_ms(video)
    for event in run.events:
        stratum = {
            "lighting": lighting_stratum(run.session.started_at, run.session.machine_settings),
            "speed_band": speed_band(run.session.machine_settings),
            "intent": _block_intent(run.blocks, event.start_ms),
        }
        stratum_key = json.dumps(stratum, sort_keys=True)
        picks: dict[int, int] = {}  # frame_no -> ts_ms; dedupes low-fps collisions
        for ts_ms in _sample_times(event.start_ms, event.end_ms, run.frames_per_ball):
            picks.setdefault(round(ts_ms * fps / 1000.0), ts_ms)
        for frame_no, ts_ms in picks.items():
            if duration_ms is not None and ts_ms >= duration_ms:
                # The event window is reference-camera time; this camera's footage
                # ends earlier. Deterministically unextractable: drop loudly.
                _LOG.warning(
                    "pick %sms for ball %s is past camera %s footage end (%sms)",
                    ts_ms,
                    event.ball_no,
                    video.camera_id,
                    duration_ms,
                )
                run.beyond_footage += 1
                continue
            pick = _Pick(ball_no=event.ball_no, frame_no=frame_no, ts_ms=ts_ms, stratum=stratum)
            try:
                _ensure_frame(run, video, pick)
            except FfmpegUnavailableError:
                raise  # infrastructure, not footage: abort and retry the whole run
            except (FrameSamplingError, FfmpegError) as exc:
                _LOG.warning(
                    "frame extraction failed for ball %s camera %s at %sms: %s",
                    event.ball_no,
                    video.camera_id,
                    ts_ms,
                    exc,
                )
                run.failed += 1
                continue
            run.strata[stratum_key] = run.strata.get(stratum_key, 0) + 1


def _ensure_frame(run: _Run, video: Video, pick: _Pick) -> None:
    """Upsert one frame: skip when row + stored image both exist, else (re)write."""
    row = run.existing.get((video.camera_id, pick.frame_no))
    if row is not None and run.store.exists(row.object_key):
        run.skipped += 1
        return
    key = frame_key(run.session_id, video.camera_id, pick.frame_no)
    run.writer(video, pick.ts_ms, key)
    if row is None:
        row = FrameSample(
            session_id=run.session_id, camera_id=video.camera_id, frame_no=pick.frame_no
        )
        run.db.add(row)
        run.existing[(video.camera_id, pick.frame_no)] = row
    row.ball_no = pick.ball_no
    row.ts_ms = pick.ts_ms
    row.object_key = key
    row.stratum = pick.stratum
    row.sampler_version = SAMPLER_VERSION
    run.db.commit()  # per-frame durability: image + row land together
    run.sampled += 1


# --- ffmpeg default writer ------------------------------------------------------


def build_frame_command(
    source: Path, dest: Path, ts_ms: int, *, ffmpeg: str = "ffmpeg"
) -> list[str]:
    """The exact ffmpeg argv extracting one still frame at ``ts_ms`` as JPEG."""
    if ts_ms < 0:
        raise FrameSamplingError(f"ts_ms must be >= 0, got {ts_ms}")
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-ss",
        f"{ts_ms / 1000:.3f}",
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(dest),
    ]


def ffmpeg_frame_writer(
    store: ObjectStore,
    workdir: Path,
    *,
    source_resolver: SourceResolver | None = None,
    runner: Runner = run_ffmpeg,
) -> FrameWriter:
    """Production :data:`FrameWriter`: resolve source, run ffmpeg, store the JPEG.

    ``runner`` executes one built argv (tests inject a fake; the default is the
    real :func:`cricai_vision.ffmpeg.run_ffmpeg`). Sources are resolved via the
    shared caching :func:`~cricai_worker.cut_clips.store_source_resolver` so a
    multi-ball session downloads each camera file once.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    resolve = (
        source_resolver
        if source_resolver is not None
        else store_source_resolver(store, workdir / "sources")
    )

    def write(video: Video, ts_ms: int, key: str) -> None:
        source = resolve(video)
        dest = workdir / f"{uuid.uuid4().hex}.jpg"
        runner(build_frame_command(source, dest, ts_ms))
        if not dest.is_file():
            raise FrameSamplingError(f"ffmpeg reported success but produced no frame for {key}")
        store.put(key, dest.read_bytes())
        dest.unlink()

    return write
