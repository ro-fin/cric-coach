"""US-F5 contact-fusion job: track + audio + bat detections -> contact metrics.

For every valid ball event of a session that has a US-F3 ball track (a
``ball_tracks`` row whose pinned payload
``sessions/{sid}/balls/{n}/track-{camera}.json`` exists in the object store),
fuses the available modalities via :mod:`cricai_coaching.contact_fusion` and
writes two phase='contact' metric keys to ``ball_metrics``:

- ``contact_quality`` — {value: middle|edge|miss, unit: 'class', confidence,
  proxy: false, source naming the modalities used};
- ``bat_path`` — {value: straight|across|inside_out, unit: 'class',
  confidence, proxy: false}, null-with-reason when no detection provider is
  configured, the ball has no contact time, or the bat boxes are too few/still.

MERGE, never replace: the existing contact-phase row is upserted by key so the
US-E3 pose-proxy metrics (``bat_path_class``, ``contact_point_class``, ...)
always survive — only the two fusion-owned keys
(:data:`cricai_coaching.contact_fusion.FUSION_METRIC_KEYS`) are (re)written,
per ball, with a per-ball commit (the extract_pose crash-safety pattern). The
guarantee is mutual: the metrics PUT endpoint preserves those same keys when a
job replaces the whole phase set, so an E3 re-run never silently deletes
fusion outputs. Re-runs are idempotent: identical inputs write identical
values.

Degradation is data, not failure: a missing audio feed or detection provider
just narrows the modalities named in provenance; a malformed track payload
drops ONLY the track modality (the ball still fuses from audio/bat when
available); balls with no track row/payload are skipped loudly in the summary
("for balls with a track payload", US-F5). Onsets are consumed through
``cricai_vision.audio_onset.refine_contact`` — the same post-release window
rule as US-D3, so a pre-release machine clank can never masquerade as contact
evidence.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from cricai_coaching.contact_fusion import (
    DEFAULT_FUSION_CONFIG,
    FUSION_METRIC_KEYS,
    FusionConfig,
    FusionError,
    ModalitySignal,
    bat_path_from_detections,
    bat_signal,
    fuse_contact,
    track_signal,
)
from cricai_coaching.contact_metrics import MetricValue
from cricai_data.db import session_scope
from cricai_data.enums import MetricPhase
from cricai_data.models import BallEvent, BallMetrics, BallTrack
from cricai_data.models import Session as SessionRow
from cricai_vision.audio_onset import Onset, RefinedContact, refine_contact
from cricai_vision.detect import DetectionProvider
from sqlalchemy import select
from sqlalchemy.orm import Session

from cricai_worker.context import WorkerContext

#: Side-on reference camera: the track consumed for fusion (US-F3 payload).
DEFAULT_CAMERA = "C1"

#: Frame rate handed to the detection provider for the contact window.
DEFAULT_FPS = 30.0

#: Skip reasons reported in :class:`FuseSummary.skipped`.
SKIP_NO_TRACK_ROW = "no ball track row"
SKIP_NO_PAYLOAD = "track payload missing from store"

#: Onsets for the session: a precomputed sequence, a lazy provider, or None
#: (no usable audio) — mirrors the US-D3 refinement job's input shape.
OnsetSource = Sequence[Onset] | Callable[[], "Sequence[Onset] | None"] | None

#: The two ball_metrics keys this job owns (shared with the metrics PUT guard).
CONTACT_QUALITY_KEY, BAT_PATH_KEY = FUSION_METRIC_KEYS


@dataclass(frozen=True)
class FuseInputs:
    """Optional modality feeds and knobs for one fusion run."""

    onsets: OnsetSource = None
    detector: DetectionProvider | None = None
    camera_id: str = DEFAULT_CAMERA
    fps: float = DEFAULT_FPS
    config: FusionConfig = DEFAULT_FUSION_CONFIG

    def __post_init__(self) -> None:
        if self.fps <= 0:
            raise ValueError(f"fps must be positive, got {self.fps}")


@dataclass(frozen=True)
class FusedBall:
    """One ball's fusion outcome as written to ball_metrics."""

    ball_no: int
    contact_quality: str | None
    confidence: float
    modalities: tuple[str, ...]
    bat_path: str | None


@dataclass(frozen=True)
class FuseSummary:
    """What one fusion run did: balls fused vs skipped (with reasons)."""

    session_id: uuid.UUID
    fused: tuple[FusedBall, ...]
    skipped: tuple[tuple[int, str], ...]  # (ball_no, reason)

    @property
    def fused_count(self) -> int:
        return len(self.fused)


def _parse_track_payload(raw: bytes) -> dict[str, Any] | None:
    """The payload as a JSON object, or None when malformed (degrade, not crash)."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _post_contact_start_ms(payload: dict[str, Any] | None) -> float | None:
    """Tolerant read of the post_contact segment start for the bat-plane fallback."""
    segments = payload.get("segments") if payload is not None else None
    if not isinstance(segments, list):
        return None
    for segment in segments:
        if isinstance(segment, dict) and segment.get("kind") == "post_contact":
            start = segment.get("start_ms")
            if isinstance(start, int | float):
                return float(start)
    return None


def _track_modality(
    payload: dict[str, Any] | None, contact_ms: float | None, config: FusionConfig
) -> ModalitySignal | None:
    """Track signal, or None when the payload is malformed (modality degrades)."""
    if payload is None:
        return None
    try:
        return track_signal(payload, contact_ms=contact_ms, config=config)
    except FusionError:
        return None


def _audio_modality(onsets: Sequence[Onset] | None, event: BallEvent) -> RefinedContact | None:
    """Audio vote via the US-D3 public API; None when audio has nothing usable."""
    if onsets is None:
        return None
    return refine_contact(
        (float(event.start_ms), float(event.end_ms)), onsets, release_ms=float(event.release_ms)
    )


def _payload_points(payload: dict[str, Any] | None) -> list[Any] | None:
    """Tolerant read of the payload's points for the bat-modality identity gate."""
    points = payload.get("points") if payload is not None else None
    return points if isinstance(points, list) else None


def _bat_modality(
    inputs: FuseInputs,
    event: BallEvent,
    bat_plane_ms: float | None,
    payload: dict[str, Any] | None,
) -> tuple[ModalitySignal | None, MetricValue]:
    """(bat overlap signal | None, bat_path metric) for one ball.

    The track payload's points gate the overlap vote's ball identity (US-F3
    decoy stress: a dropout at the contact frame must not pair the bat with a
    decoy ball); bat_path is ball-independent and needs no gate.
    """
    if inputs.detector is None:
        return None, MetricValue(None, "class", 0.0, reason="no detection provider configured")
    if bat_plane_ms is None:
        return None, MetricValue(
            None,
            "class",
            0.0,
            reason="no contact time (no contact_ms on the event and no post-contact segment)",
        )
    detections = inputs.detector.detect(
        start_ms=float(event.start_ms), end_ms=float(event.end_ms), fps=inputs.fps
    )
    return (
        bat_signal(
            detections,
            contact_ts_ms=bat_plane_ms,
            track_points=_payload_points(payload),
            config=inputs.config,
        ),
        bat_path_from_detections(detections, contact_ts_ms=bat_plane_ms, config=inputs.config),
    )


def _merge_contact_metrics(
    db: Session, session_id: uuid.UUID, ball_no: int, entries: dict[str, dict[str, Any]]
) -> None:
    """Upsert-by-key into the ball's contact-phase metrics row, preserving the rest."""
    row = db.execute(
        select(BallMetrics).where(
            BallMetrics.session_id == session_id,
            BallMetrics.ball_no == ball_no,
            BallMetrics.phase == MetricPhase.CONTACT,
        )
    ).scalar_one_or_none()
    if row is None:
        row = BallMetrics(
            session_id=session_id, ball_no=ball_no, phase=MetricPhase.CONTACT, metrics={}
        )
        db.add(row)
    merged = dict(row.metrics)  # E3 proxy keys and any other keys survive (pinned)
    merged.update(entries)
    row.metrics = merged  # fresh dict: plain JSON columns don't track in-place edits


def fuse_session_contacts(
    ctx: WorkerContext, session_id: uuid.UUID, *, inputs: FuseInputs | None = None
) -> FuseSummary:
    """Fuse contact metrics for every valid ball event with a track payload."""
    opts = inputs if inputs is not None else FuseInputs()
    resolved = opts.onsets() if callable(opts.onsets) else opts.onsets
    fused: list[FusedBall] = []
    skipped: list[tuple[int, str]] = []
    with session_scope(ctx.session_factory) as db:
        if db.get(SessionRow, session_id) is None:
            raise ValueError(f"session not found: {session_id}")
        events = (
            db.execute(
                select(BallEvent)
                .where(BallEvent.session_id == session_id, BallEvent.valid.is_(True))
                .order_by(BallEvent.ball_no)
            )
            .scalars()
            .all()
        )
        for event in events:
            track_row = db.execute(
                select(BallTrack).where(
                    BallTrack.session_id == session_id,
                    BallTrack.ball_no == event.ball_no,
                    BallTrack.camera_id == opts.camera_id,
                )
            ).scalar_one_or_none()
            if track_row is None:
                skipped.append((event.ball_no, SKIP_NO_TRACK_ROW))
                continue
            if not ctx.store.exists(track_row.points_key):
                skipped.append((event.ball_no, SKIP_NO_PAYLOAD))
                continue
            payload = _parse_track_payload(ctx.store.get(track_row.points_key))
            contact_ms = float(event.contact_ms) if event.contact_ms is not None else None
            bat_plane_ms = contact_ms if contact_ms is not None else _post_contact_start_ms(payload)
            track = _track_modality(payload, contact_ms, opts.config)
            audio = _audio_modality(resolved, event)
            bat, bat_path = _bat_modality(opts, event, bat_plane_ms, payload)
            result = fuse_contact(track=track, audio=audio, bat=bat, config=opts.config)
            _merge_contact_metrics(
                db,
                session_id,
                event.ball_no,
                {
                    CONTACT_QUALITY_KEY: result.contact_quality.to_payload(),
                    BAT_PATH_KEY: bat_path.to_payload(),
                },
            )
            db.commit()  # per-ball durability: crash-resume is a plain re-run
            quality = result.contact_quality.value
            path = bat_path.value
            fused.append(
                FusedBall(
                    ball_no=event.ball_no,
                    contact_quality=quality if isinstance(quality, str) else None,
                    confidence=result.contact_quality.confidence,
                    modalities=result.used,
                    bat_path=path if isinstance(path, str) else None,
                )
            )
    return FuseSummary(session_id=session_id, fused=tuple(fused), skipped=tuple(skipped))
