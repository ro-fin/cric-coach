"""Per-ball multi-camera clip generation job (US-D2).

For every valid :class:`~cricai_data.models.BallEvent` in a session and every
camera with evidence footage, cut a pre/post-rolled clip and store it under
the pinned key layout ``sessions/{session_id}/balls/{ball_no}/{camera_id}.mp4``
(:func:`cricai_data.storage.clip_key` — the retention layer protects these
prefixes for ground-truth balls, US-B6).

Evidence rule:
    The set of Video statuses that prove usable footage exists is canonical
    in :data:`cricai_data.enums.EVIDENCE_STATUSES`, shared with the API's
    capture-state seam (UPLOADED, PROBED, METADATA_CONFLICT all have playable
    bytes in the store; FAILED uploads had their bytes deleted and never
    count).

Loud gaps (US-D2 AC):
    An expected camera without evidence footage produces a
    :attr:`~cricai_data.enums.ClipStatus.GAP` row per ball with the reason in
    ``error`` — never a silent absence. A camera that has evidence footage
    but is missing from ``Session.expected_cameras`` is still clipped.

Crash-resume + idempotency (US-D2 AC):
    The job is status-driven and commits after every row, so a re-run
    - skips rows already CUT whose stored window still matches the event's
      current effective window and whose object still exists in the store,
    - re-cuts CUT rows whose object went missing OR whose event timings were
      corrected since the cut (US-D4: a correction re-cuts, never skips),
    - keeps a CUT clip when the camera's footage evidence is later lost:
      the duration is then unknown so the recomputed window is unclamped,
      and a stored window consistent with a past duration clamp (same
      start, end <= the unclamped end) still counts as matching — losing
      duration knowledge alone never demotes a valid clip (see _mark_gaps),
    - reprocesses PENDING / FAILED / GAP rows (a late upload heals a gap),
    - never duplicates rows (upsert on the (session, ball, camera) triple).

Error split (mirrors :mod:`cricai_vision.ffmpeg`):
    :class:`FfmpegError` — including :class:`FfmpegTimeoutError`, a
    pathological source that hangs ffmpeg past the per-clip timeout — means
    this one clip cannot be cut: the row is marked FAILED and the run
    continues with the next ball/camera. Only genuine unavailability
    (:class:`FfmpegUnavailableError`: the binary is missing/not runnable)
    aborts the run early; the current row is then left PENDING and untouched
    balls/cameras keep their PENDING/absent rows, so a later run resumes
    exactly where this one stopped — never a livelock on one bad input.
    Every failure branch clears ``object_key``: after a window-drift re-cut
    failure the row would otherwise keep pointing at live pre-correction
    footage while claiming the corrected bounds (the clips API serves both
    fields verbatim).

Player-safe errors:
    ``Clip.error`` is served verbatim to every read role including PLAYER,
    so only short classified messages are persisted (e.g. ``"ffmpeg failed
    (timeout)"``). Raw exception text — which can embed server filesystem
    paths — goes to the worker log only.
"""

from __future__ import annotations

import logging
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from cricai_data.enums import EVIDENCE_STATUSES, ClipStatus
from cricai_data.models import BallEvent, Clip, Session, Video
from cricai_data.storage import ObjectStore, StorageError, clip_key
from cricai_vision.ffmpeg import (
    ClipWindow,
    FfmpegError,
    FfmpegTimeoutError,
    FfmpegUnavailableError,
    build_clip_command,
    clip_window,
    run_ffmpeg,
)
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_worker.context import WorkerContext

_LOG = logging.getLogger(__name__)

#: ``Clip.error`` column width (String(255)).
_ERROR_MAX_LEN = 255

#: A resolver turns a Video row into a local file path ffmpeg can read.
SourceResolver = Callable[[Video], Path]

#: A runner executes one built ffmpeg argv and must produce the dest file;
#: it raises FfmpegError / FfmpegUnavailableError per the ffmpeg contract.
Runner = Callable[[list[str]], None]


@dataclass(frozen=True)
class ClipRunSummary:
    """What one ``cut_session_clips`` run did (counts are rows touched)."""

    session_id: str
    balls: int
    cameras: tuple[str, ...]
    cut: int
    skipped: int
    gaps: int
    failed: int
    pending: int
    stopped_early: bool


def store_source_resolver(store: ObjectStore, workdir: Path) -> SourceResolver:
    """Default resolver: stream the camera file out of the object store.

    Results are cached per object key so a multi-ball session downloads each
    camera source exactly once per run.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    cache: dict[str, Path] = {}

    def resolve(video: Video) -> Path:
        key = video.object_key
        if key not in cache:
            dest = workdir / f"source-{video.camera_id}-{video.id.hex}.mp4"
            store.write_to_path(key, dest)
            cache[key] = dest
        return cache[key]

    return resolve


@dataclass
class _Run:
    """Shared per-run state threaded through the camera/ball loops."""

    db: OrmSession
    store: ObjectStore
    session_id: uuid.UUID
    source_resolver: SourceResolver
    runner: Runner
    workdir: Path
    existing: dict[tuple[int, str], Clip]  # (ball_no, camera_id) -> row
    counts: dict[str, int] = field(
        default_factory=lambda: {"cut": 0, "skipped": 0, "gaps": 0, "failed": 0, "pending": 0}
    )
    stopped_early: bool = False


def cut_session_clips(
    ctx: WorkerContext,
    session_id: uuid.UUID,
    *,
    source_resolver: SourceResolver,
    runner: Runner = run_ffmpeg,
) -> ClipRunSummary:
    """Cut every (valid ball event x evidence camera) clip for one session."""
    with ctx.session_factory() as db, tempfile.TemporaryDirectory(prefix="cricai-clips-") as tmp:
        session = db.get(Session, session_id)
        if session is None:
            raise ValueError(f"session not found: {session_id}")
        events = list(
            db.scalars(
                select(BallEvent)
                .where(BallEvent.session_id == session_id, BallEvent.valid.is_(True))
                .order_by(BallEvent.ball_no)
            )
        )
        videos = _evidence_videos(db, session_id)
        camera_ids = sorted(set(session.expected_cameras) | set(videos))
        run = _Run(
            db=db,
            store=ctx.store,
            session_id=session_id,
            source_resolver=source_resolver,
            runner=runner,
            workdir=Path(tmp),
            existing={
                (clip.ball_no, clip.camera_id): clip
                for clip in db.scalars(select(Clip).where(Clip.session_id == session_id))
            },
        )
        for camera_id in camera_ids:
            _process_camera(run, camera_id, events, videos.get(camera_id))
            if run.stopped_early:
                break
        db.commit()
        return ClipRunSummary(
            session_id=str(session_id),
            balls=len(events),
            cameras=tuple(camera_ids),
            stopped_early=run.stopped_early,
            **run.counts,
        )


def _evidence_videos(db: OrmSession, session_id: uuid.UUID) -> dict[str, Video]:
    """Camera id -> its newest evidence-status Video (the file worth cutting)."""
    rows = db.scalars(
        select(Video)
        .where(Video.session_id == session_id, Video.status.in_(sorted(EVIDENCE_STATUSES)))
        .order_by(Video.created_at, Video.id)
    )
    return {video.camera_id: video for video in rows}  # newest row wins


def _windows(
    events: Sequence[BallEvent], *, video_duration_ms: int | None
) -> dict[int, ClipWindow | str]:
    """Pre/post-rolled cut window per ball — end-clamped to the camera's media
    duration when known — or the error text when the event timestamps cannot
    form one (recorded FAILED, never silently skipped)."""
    out: dict[int, ClipWindow | str] = {}
    for event in events:
        try:
            out[event.ball_no] = clip_window(
                event.start_ms, event.end_ms, video_duration_ms=video_duration_ms
            )
        except FfmpegError as exc:
            out[event.ball_no] = f"invalid event window: {exc}"
    return out


def _video_duration_ms(video: Video) -> int | None:
    """Best-known media duration: the measured probe wins over the client claim."""
    probed = (video.probe or {}).get("duration_s")
    duration_s = probed if isinstance(probed, int | float) else video.claimed_duration_s
    if duration_s is None:
        return None
    return int(duration_s * 1000)


def _process_camera(
    run: _Run, camera_id: str, events: Sequence[BallEvent], video: Video | None
) -> None:
    if video is None:
        reason = _no_footage_reason(run.db, run.session_id, camera_id)
        _mark_gaps(run, camera_id, events, reason, duration_ms=None)
        return
    duration_ms = _video_duration_ms(video)
    try:
        source = run.source_resolver(video)
    except (StorageError, OSError) as exc:
        # Raw text can embed server paths; players see Clip.error (US-L1).
        _LOG.warning(
            "source video unavailable for camera %s (video %s): %s", camera_id, video.id, exc
        )
        reason = f"source video unavailable for camera {camera_id}"
        _mark_gaps(run, camera_id, events, reason, duration_ms=duration_ms)
        return
    _cut_camera(run, camera_id, events, source, _windows(events, video_duration_ms=duration_ms))


def _no_footage_reason(db: OrmSession, session_id: uuid.UUID, camera_id: str) -> str:
    statuses = sorted(
        {
            str(row)
            for row in db.scalars(
                select(Video.status).where(
                    Video.session_id == session_id, Video.camera_id == camera_id
                )
            )
        }
    )
    if not statuses:
        return f"no video uploaded for camera {camera_id}"
    return f"no evidence footage for camera {camera_id} (video statuses: {', '.join(statuses)})"


def _mark_gaps(
    run: _Run,
    camera_id: str,
    events: Sequence[BallEvent],
    reason: str,
    *,
    duration_ms: int | None,
) -> None:
    """Record the missing camera loudly for every ball (US-D2 AC), keeping any
    clip that was already cut for the event's current window and still has its
    object in the store.

    Kept-clip rule when ``duration_ms`` is None (no evidence video to read the
    media duration from): the recomputed windows are unclamped, so a stored
    window also counts as current when its start matches and its end lies in
    (start, unclamped end] — i.e. it is consistent with the current event under
    some past duration clamp. Losing duration knowledge alone must never demote
    a validly cut clip to GAP and sever its object_key (the footage reference
    would be orphaned for good if the source never returns); only a genuine
    event-timing drift (start moved, or end past the unclamped bound) demotes.
    """
    windows = _windows(events, video_duration_ms=duration_ms)
    for event in events:
        clip = _upsert_row(run, event, camera_id)
        if _already_cut(run, clip, windows[event.ball_no], accept_past_clamp=duration_ms is None):
            run.counts["skipped"] += 1
            continue
        _apply_window(clip, event, windows[event.ball_no])
        clip.status = ClipStatus.GAP
        clip.object_key = None
        clip.error = _truncate(reason)
        run.counts["gaps"] += 1
        run.db.commit()  # crash-resume granularity: every row lands durably


def _cut_camera(
    run: _Run,
    camera_id: str,
    events: Sequence[BallEvent],
    source: Path,
    windows: dict[int, ClipWindow | str],
) -> None:
    for event in events:
        clip = _upsert_row(run, event, camera_id)
        window = windows[event.ball_no]
        if _already_cut(run, clip, window):
            run.counts["skipped"] += 1
            continue
        _apply_window(clip, event, window)
        if isinstance(window, str):
            clip.status = ClipStatus.FAILED
            clip.error = _truncate(window)
            run.counts["failed"] += 1
        else:
            _cut_one(run, clip, source, window)
        run.db.commit()  # crash-resume granularity: every row lands durably
        if run.stopped_early:
            return


def _upsert_row(run: _Run, event: BallEvent, camera_id: str) -> Clip:
    """Fetch-or-create on the (session, ball, camera) unique triple."""
    key = (event.ball_no, camera_id)
    clip = run.existing.get(key)
    if clip is None:
        clip = Clip(
            session_id=run.session_id,
            ball_no=event.ball_no,
            camera_id=camera_id,
            start_ms=event.start_ms,
            end_ms=event.end_ms,
            status=ClipStatus.PENDING,
        )
        run.db.add(clip)
        run.existing[key] = clip
    return clip


def _already_cut(
    run: _Run, clip: Clip, window: ClipWindow | str, *, accept_past_clamp: bool = False
) -> bool:
    """Skip only when the row says CUT, its stored window still matches the
    event's current effective window, *and* the object really exists; a window
    drift (US-D4 timing correction) or a missing object means the clip must be
    re-cut — overwrite the object and update the row (US-D2 resume AC).

    ``accept_past_clamp`` (gap paths with unknown duration only) additionally
    accepts a stored window that ends short of the recomputed unclamped end —
    see the kept-clip rule on :func:`_mark_gaps`."""
    return (
        clip.status == ClipStatus.CUT
        and clip.object_key is not None
        and isinstance(window, ClipWindow)
        and _window_current(clip, window, accept_past_clamp=accept_past_clamp)
        and run.store.exists(clip.object_key)
    )


def _window_current(clip: Clip, window: ClipWindow, *, accept_past_clamp: bool) -> bool:
    """Does the stored window still describe the event's current window?"""
    if (clip.start_ms, clip.end_ms) == (window.start_ms, window.end_ms):
        return True
    return (
        accept_past_clamp
        and clip.start_ms == window.start_ms
        and clip.start_ms < clip.end_ms <= window.end_ms
    )


def _apply_window(clip: Clip, event: BallEvent, window: ClipWindow | str) -> None:
    if isinstance(window, ClipWindow):
        clip.start_ms = window.start_ms
        clip.end_ms = window.end_ms
    else:  # unusable event timestamps: keep the raw event bounds visible
        clip.start_ms = event.start_ms
        clip.end_ms = event.end_ms


def _cut_one(run: _Run, clip: Clip, source: Path, window: ClipWindow) -> None:
    dest_key = clip_key(str(run.session_id), clip.ball_no, clip.camera_id)
    out_path = run.workdir / f"{clip.camera_id}-{clip.ball_no}.mp4"
    argv = build_clip_command(source, out_path, window)
    try:
        run.runner(argv)
    except FfmpegUnavailableError as exc:
        # Retryable: ffmpeg itself is gone; leave the row pending, stop the run.
        _LOG.error("ffmpeg unavailable while cutting %s: %s", dest_key, exc)
        clip.status = ClipStatus.PENDING
        clip.object_key = None
        clip.error = "ffmpeg unavailable"
        run.counts["pending"] += 1
        run.stopped_early = True
        return
    except FfmpegError as exc:
        # Per-file (including a timeout on a pathological source): mark this
        # clip FAILED with a classified, path-free message and keep going.
        # object_key is cleared like the unavailable path above — a failed
        # window-drift re-cut would otherwise leave the row pointing at live
        # pre-correction footage while claiming the corrected bounds.
        _LOG.warning("ffmpeg failed while cutting %s: %s", dest_key, exc)
        clip.status = ClipStatus.FAILED
        clip.object_key = None
        clip.error = (
            "ffmpeg failed (timeout)" if isinstance(exc, FfmpegTimeoutError) else "ffmpeg failed"
        )
        run.counts["failed"] += 1
        return
    if not out_path.is_file():
        clip.status = ClipStatus.FAILED
        clip.object_key = None  # never point a FAILED row at stale footage
        clip.error = "ffmpeg reported success but produced no output"
        run.counts["failed"] += 1
        return
    run.store.put(dest_key, out_path.read_bytes())
    clip.object_key = dest_key
    clip.status = ClipStatus.CUT
    clip.error = None
    run.counts["cut"] += 1


def _truncate(message: str) -> str:
    if len(message) <= _ERROR_MAX_LEN:
        return message
    return message[: _ERROR_MAX_LEN - 3] + "..."
