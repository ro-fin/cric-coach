"""Audio-assisted contact refinement job (US-D3).

CONTACT REFINEMENT RULE (pinned, cross-group): refinement updates
``BallEvent.contact_ms`` ONLY on rows with ``source == EventSource.AUTO``.
Human rows (MANUAL) and human-corrected rows (CORRECTED) are never overwritten,
and rejected rows (``valid == False``) are never touched or resurrected. Every
applied update records the millisecond shift and the audio confidence in the
returned :class:`RefineSummary` (``BallEvent.confidence`` stays the detector's
event confidence and is not modified here). The job never guesses: when audio
is missing every AUTO row keeps its vision-derived timing and is counted in
``flagged_missing_audio``; when audio exists but no acceptable onset lands in
an event's window (or only a low-confidence fallback does) the row is left
untouched and counted in ``flagged_low_confidence`` (US-D3 degrades gracefully).

ORDERING: onsets are searched strictly inside ``(release_ms, end_ms]`` - bat
contact cannot precede release - so every applied update preserves the pinned
event ordering invariant ``start_ms <= release_ms <= contact_ms <= end_ms``
that the corrections API enforces.

PROVENANCE: an applied refinement is recorded on the ``ball_events`` row itself
by composing ``detector_version`` as ``"<detector>+<refiner>"`` (for example
``cue-segmenter-1.0.0+audio-refine-1.0.0``, refiner from
:data:`REFINER_VERSION`), so the US-D4 ground-truth export can distinguish
audio-refined contacts from raw vision contacts. ``source`` stays ``AUTO`` and
no ``event_corrections`` row is written - those are reserved for human ground
truth (US-D4). Re-running replaces any prior refiner suffix instead of
stacking, so the value stays stable across runs.

Idempotent: re-running with the same onsets writes the same contact times and
the same composed ``detector_version``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from cricai_data.db import session_scope
from cricai_data.enums import EventSource
from cricai_data.models import BallEvent
from cricai_vision.audio_onset import Onset, OnsetKind, refine_contact
from sqlalchemy import select

from cricai_worker.context import WorkerContext

#: Refinements below this confidence are flagged, not applied (no guessing).
DEFAULT_MIN_CONFIDENCE = 0.3

#: Version of this audio refinement pass, composed onto ``detector_version``
#: (``"<detector>+<refiner>"``) on every row whose contact it rewrites.
REFINER_VERSION = "audio-refine-1.0.0"

#: Callable that yields the session's audio onsets, or ``None`` when the
#: reference camera has no usable audio track.
OnsetsProvider = Callable[[], Sequence[Onset] | None]

#: Accepted onset inputs: precomputed onsets, a lazy provider, or ``None``
#: (audio known to be missing up front).
OnsetSource = Sequence[Onset] | OnsetsProvider | None


def _composed_detector_version(current: str) -> str:
    """``detector_version`` with the refiner recorded: ``"<detector>+<refiner>"``.

    Any prior refiner suffix is replaced, not stacked, so re-runs are stable;
    an empty detector version yields the bare :data:`REFINER_VERSION`.
    """
    base = current.split("+", 1)[0]
    return f"{base}+{REFINER_VERSION}" if base else REFINER_VERSION


@dataclass(frozen=True)
class RefinedBall:
    """One applied contact update, with the shift recorded in confidence terms."""

    ball_no: int
    contact_ms: int
    previous_contact_ms: int | None
    shift_ms: int | None  # None when the event had no prior contact estimate
    confidence: float
    kind: OnsetKind


@dataclass(frozen=True)
class RefineSummary:
    """What one refinement run did to a session's ball events."""

    session_id: uuid.UUID
    audio_available: bool
    refined: tuple[RefinedBall, ...]
    skipped_human: int  # MANUAL/CORRECTED rows left untouched per the pinned rule
    flagged_low_confidence: int  # AUTO rows kept on vision timing (no good onset)
    flagged_missing_audio: int  # AUTO rows flagged because audio was missing

    @property
    def refined_count(self) -> int:
        return len(self.refined)


def refine_session_contacts(
    ctx: WorkerContext,
    session_id: uuid.UUID,
    onsets: OnsetSource,
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    prefer: OnsetKind = OnsetKind.BAT_CRACK,
) -> RefineSummary:
    """Refine contact times for every eligible AUTO event of one session."""
    resolved = onsets() if callable(onsets) else onsets
    with session_scope(ctx.session_factory) as db:
        rows = (
            db.execute(
                select(BallEvent)
                .where(BallEvent.session_id == session_id, BallEvent.valid.is_(True))
                .order_by(BallEvent.ball_no)
            )
            .scalars()
            .all()
        )
        auto_rows = [row for row in rows if row.source is EventSource.AUTO]
        skipped_human = len(rows) - len(auto_rows)
        if resolved is None:
            return RefineSummary(
                session_id=session_id,
                audio_available=False,
                refined=(),
                skipped_human=skipped_human,
                flagged_low_confidence=0,
                flagged_missing_audio=len(auto_rows),
            )
        applied: list[RefinedBall] = []
        flagged_low_confidence = 0
        for row in auto_rows:
            result = refine_contact(
                (row.start_ms, row.end_ms), resolved, release_ms=row.release_ms, prefer=prefer
            )
            if result is None or result.confidence < min_confidence:
                flagged_low_confidence += 1
                continue
            previous = row.contact_ms
            contact_ms = round(result.contact_ms)
            row.contact_ms = contact_ms
            row.detector_version = _composed_detector_version(row.detector_version)
            applied.append(
                RefinedBall(
                    ball_no=row.ball_no,
                    contact_ms=contact_ms,
                    previous_contact_ms=previous,
                    shift_ms=None if previous is None else contact_ms - previous,
                    confidence=result.confidence,
                    kind=result.kind,
                )
            )
        return RefineSummary(
            session_id=session_id,
            audio_available=True,
            refined=tuple(applied),
            skipped_human=skipped_human,
            flagged_low_confidence=flagged_low_confidence,
            flagged_missing_audio=0,
        )
