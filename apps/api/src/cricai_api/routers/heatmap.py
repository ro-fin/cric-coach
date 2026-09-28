"""Line/length heatmap aggregation & render (US-C6).

- ``GET /sessions/{id}/heatmap``: JSON zone table (balls, control %,
  false-shot % per line x length cell) with block/bowler_source/shot/
  contact/control filters.
- ``GET /sessions/{id}/heatmap.png``: the same aggregation rendered as a
  top-down pitch map PNG for the daily report.
- ``GET /players/{id}/heatmap``: cross-session trend aggregation honoring the
  US-C4 exclusion contract (calibration_suspect sessions never counted).

Aggregation contract:

- The effective bounce per ball comes from the shared US-F4 precedence
  resolver (:mod:`cricai_api.services.bounce_resolve`): a manual BounceMark
  ALWAYS wins; balls with only an auto BounceEstimate join the map with
  ``source=auto`` and the estimator's confidence.
- A ball appears once even when marked from several cameras: the mark with the
  lowest camera_id is the representative; the ball is flagged if ANY of its
  camera marks is flagged_for_review (resolver rules). Flagged balls stay IN
  the cells (the review UI needs them visible) and are surfaced in
  ``flagged_balls``.
- Auto bounces with confidence below ``min_confidence`` (default 0.5) are
  excluded by default; ``include_low_confidence=true`` includes them marked
  ``hollow`` per point and counted per cell (US-F4 AC). Manual bounces are
  never hollow and never excluded.
- Bounces without derived line/length classes cannot be placed in a cell and
  are ignored.
- Balls with a bounce but no BallTag count toward ``balls`` with null
  control/false-shot percentages; they are excluded whenever a tag-dependent
  filter (block, shot, contact, control) is active, because the predicate
  cannot be evaluated for them.
- A ball's bowler_source is its tag's block-level bowler_source when the tag
  is assigned to a block, else the session-level bowler_source.
- A false shot is any contact in (edge, miss).
"""

import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Annotated

from cricai_data.enums import BowlerSource, Contact, EventSource, Length, Line, Role, Shot
from cricai_data.models import BallTag, Player, Session, SessionBlock
from cricai_vision.heatmap import render_pitch_map
from cricai_vision.zones import ZoneConfig, ZoneConfigError
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db
from cricai_api.services.bounce_resolve import resolve_session_bounces

router = APIRouter(tags=["heatmap"])

#: US-C6 zone table: a false shot is any non-middle contact.
FALSE_SHOT_CONTACTS: tuple[Contact, ...] = (Contact.EDGE, Contact.MISS)

#: US-F4: auto bounces below this confidence are excluded from the map by
#: default (query-param tunable; include_low_confidence shows them hollow).
DEFAULT_MIN_CONFIDENCE = 0.5

#: All roles may read heatmaps; player access is scoped away from guest data.
READ_ROLES: tuple[Role, ...] = (Role.PARENT, Role.COACH, Role.PLAYER)

_LINE_ORDER = {line: index for index, line in enumerate(Line)}
_LENGTH_ORDER = {length: index for index, length in enumerate(Length)}


@dataclass
class _Ball:
    """One ball's effective bounce (manual or auto) joined with its (optional) tag."""

    ball_no: int
    line: Line
    length: Length
    pitch_x: float  # effective bounce point, for to-scale rendering
    pitch_y: float
    flagged: bool
    tag: BallTag | None
    session_bowler_source: BowlerSource
    block_bowler_source: BowlerSource | None  # the tag's block, when assigned
    source: EventSource  # manual mark or auto estimate (US-F4 provenance)
    confidence: float | None  # estimator confidence; None for manual clicks
    hollow: bool  # a low-confidence auto bounce included via the toggle


@dataclass(frozen=True)
class _Filters:
    block_id: uuid.UUID | None
    bowler_source: BowlerSource | None
    shot: Shot | None
    contact: Contact | None
    control: bool | None


class SourceCounts(BaseModel):
    """Per-source ball counts inside one cell (US-F4 provenance)."""

    manual: int
    auto: int


class CellOut(BaseModel):
    """One line x length cell of the zone table (US-C6 acceptance)."""

    line: Line
    length: Length
    balls: int
    control_pct: float | None  # null when no tagged balls land in the cell
    false_shot_pct: float | None
    sources: SourceCounts  # US-F4: manual-vs-auto provenance per cell
    hollow: int  # low-confidence auto balls included via include_low_confidence


class BouncePointOut(BaseModel):
    """One effective bounce point with its provenance (US-F4)."""

    ball_no: int
    line: Line
    length: Length
    pitch_x: float
    pitch_y: float
    source: EventSource
    confidence: float | None
    hollow: bool


class SessionHeatmapOut(BaseModel):
    session_id: uuid.UUID
    total_balls: int
    cells: list[CellOut]
    flagged_balls: list[int]  # included in cells, listed for the review UI
    points: list[BouncePointOut]  # scatter data with per-point provenance (US-F4)


class PlayerHeatmapOut(BaseModel):
    player_id: uuid.UUID
    total_balls: int
    cells: list[CellOut]
    excluded_sessions: int  # US-C4: suspect sessions excluded, visibly


def _session_or_404(db: OrmSession, session_id: uuid.UUID, role: Role) -> Session:
    session = db.get(Session, session_id)
    if session is None or (role is Role.PLAYER and session.player.is_guest):
        # US-L3: guest data is parent/coach-only; hide existence from players.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return session


def _resolve_block_id(
    db: OrmSession, session_id: uuid.UUID, block_no: int | None
) -> uuid.UUID | None:
    if block_no is None:
        return None
    block = db.scalar(
        select(SessionBlock).where(
            SessionBlock.session_id == session_id, SessionBlock.block_no == block_no
        )
    )
    if block is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"block {block_no} not found in session")
    return block.id


def _session_balls(
    db: OrmSession,
    session: Session,
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    include_low_confidence: bool = False,
) -> list[_Ball]:
    """One `_Ball` per effective bounce (manual wins over auto, US-F4): join the
    tag, resolve the block source, apply the low-confidence exclusion."""
    tags = {
        tag.ball_no: tag
        for tag in db.scalars(select(BallTag).where(BallTag.session_id == session.id))
    }
    block_sources = {
        block.id: block.bowler_source
        for block in db.scalars(select(SessionBlock).where(SessionBlock.session_id == session.id))
    }
    balls: list[_Ball] = []
    for bounce in resolve_session_bounces(db, session.id):
        line, length = bounce.line, bounce.length
        if line is None or length is None:
            continue  # cannot be placed in a cell (documented aggregation rule)
        hollow = (
            bounce.source is EventSource.AUTO
            and bounce.confidence is not None
            and bounce.confidence < min_confidence
        )
        if hollow and not include_low_confidence:
            continue  # excluded from the map by default (US-F4 AC)
        tag = tags.get(bounce.ball_no)
        balls.append(
            _Ball(
                ball_no=bounce.ball_no,
                line=line,
                length=length,
                pitch_x=bounce.pitch_x,
                pitch_y=bounce.pitch_y,
                flagged=bounce.flagged,
                tag=tag,
                session_bowler_source=session.bowler_source,
                block_bowler_source=(
                    block_sources[tag.block_id]
                    if tag is not None and tag.block_id is not None
                    else None
                ),
                source=bounce.source,
                confidence=bounce.confidence,
                hollow=hollow,
            )
        )
    return balls


def _bowler_source_of(ball: _Ball) -> BowlerSource:
    if ball.block_bowler_source is not None:
        return ball.block_bowler_source
    return ball.session_bowler_source


def _passes(ball: _Ball, filters: _Filters) -> bool:
    """Tag-dependent filters exclude tag-less balls: the predicate is unknowable."""
    tag = ball.tag
    if filters.block_id is not None and (tag is None or tag.block_id != filters.block_id):
        return False
    if filters.bowler_source is not None and _bowler_source_of(ball) is not filters.bowler_source:
        return False
    if filters.shot is not None and (tag is None or tag.shot is not filters.shot):
        return False
    if filters.contact is not None and (tag is None or tag.contact is not filters.contact):
        return False
    return not (filters.control is not None and (tag is None or tag.control != filters.control))


def _filtered_session_balls(
    db: OrmSession,
    session: Session,
    block: int | None,
    bowler_source: BowlerSource | None,
    shot: Shot | None,
    contact: Contact | None,
    control: bool | None,
    min_confidence: float,
    include_low_confidence: bool,
) -> list[_Ball]:
    filters = _Filters(
        block_id=_resolve_block_id(db, session.id, block),
        bowler_source=bowler_source,
        shot=shot,
        contact=contact,
        control=control,
    )
    balls = _session_balls(
        db,
        session,
        min_confidence=min_confidence,
        include_low_confidence=include_low_confidence,
    )
    return [ball for ball in balls if _passes(ball, filters)]


def _parse_zone_config(raw: str | None) -> ZoneConfig | None:
    """JSON-encoded query param -> ZoneConfig; malformed input is a 422, never a 500.

    The raw dict is passed straight through to ``ZoneConfig.from_dict`` (no field
    enumeration here) so new config fields compose without router changes.
    """
    if raw is None:
        return None
    try:
        return ZoneConfig.from_dict(json.loads(raw))
    except (json.JSONDecodeError, ZoneConfigError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"invalid zone config: {exc}"
        ) from exc


def _cells(balls: list[_Ball]) -> list[CellOut]:
    """Zone table rows for every non-empty cell, in canonical line/length order."""
    grouped: dict[tuple[Line, Length], list[_Ball]] = defaultdict(list)
    for ball in balls:
        grouped[(ball.line, ball.length)].append(ball)
    cells: list[CellOut] = []
    for line, length in sorted(
        grouped, key=lambda key: (_LINE_ORDER[key[0]], _LENGTH_ORDER[key[1]])
    ):
        cell_balls = grouped[(line, length)]
        tags = [ball.tag for ball in cell_balls if ball.tag is not None]
        control_pct: float | None = None
        false_shot_pct: float | None = None
        if tags:
            controlled = sum(1 for tag in tags if tag.control)
            false_shots = sum(1 for tag in tags if tag.contact in FALSE_SHOT_CONTACTS)
            control_pct = round(100.0 * controlled / len(tags), 1)
            false_shot_pct = round(100.0 * false_shots / len(tags), 1)
        manual = sum(1 for ball in cell_balls if ball.source is EventSource.MANUAL)
        cells.append(
            CellOut(
                line=line,
                length=length,
                balls=len(cell_balls),
                control_pct=control_pct,
                false_shot_pct=false_shot_pct,
                sources=SourceCounts(manual=manual, auto=len(cell_balls) - manual),
                hollow=sum(1 for ball in cell_balls if ball.hollow),
            )
        )
    return cells


def _points(balls: list[_Ball]) -> list[BouncePointOut]:
    return [
        BouncePointOut(
            ball_no=ball.ball_no,
            line=ball.line,
            length=ball.length,
            pitch_x=ball.pitch_x,
            pitch_y=ball.pitch_y,
            source=ball.source,
            confidence=ball.confidence,
            hollow=ball.hollow,
        )
        for ball in balls
    ]


@router.get("/sessions/{session_id}/heatmap")
def session_heatmap(
    session_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(*READ_ROLES))],
    block: int | None = None,
    bowler_source: BowlerSource | None = None,
    shot: Shot | None = None,
    contact: Contact | None = None,
    control: bool | None = None,
    min_confidence: Annotated[float, Query(ge=0.0, le=1.0)] = DEFAULT_MIN_CONFIDENCE,
    include_low_confidence: bool = False,
) -> SessionHeatmapOut:
    """US-C6 zone table over the effective bounces (manual wins over auto,
    US-F4). Auto bounces below ``min_confidence`` are excluded by default;
    ``include_low_confidence=true`` includes them marked hollow.
    """
    session = _session_or_404(db, session_id, role)
    balls = _filtered_session_balls(
        db,
        session,
        block,
        bowler_source,
        shot,
        contact,
        control,
        min_confidence,
        include_low_confidence,
    )
    return SessionHeatmapOut(
        session_id=session_id,
        total_balls=len(balls),
        cells=_cells(balls),
        flagged_balls=[ball.ball_no for ball in balls if ball.flagged],
        points=_points(balls),
    )


@router.get("/sessions/{session_id}/heatmap.png")
def session_heatmap_png(
    session_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(*READ_ROLES))],
    block: int | None = None,
    bowler_source: BowlerSource | None = None,
    shot: Shot | None = None,
    contact: Contact | None = None,
    control: bool | None = None,
    zone_config: str | None = None,
    min_confidence: Annotated[float, Query(ge=0.0, le=1.0)] = DEFAULT_MIN_CONFIDENCE,
    include_low_confidence: bool = False,
) -> Response:
    """Render the filtered session heatmap as a top-down pitch-map PNG.

    Each included ball's effective bounce (manual wins over auto, US-F4) is
    scattered to scale over the shaded zone cells (US-C6). The US-F4
    ``min_confidence``/``include_low_confidence`` params apply exactly as in
    the JSON endpoint, but the PNG renders every included point alike — the
    hollow distinction is JSON-only (the renderer is a shared module outside
    this story). ``zone_config`` is an optional JSON-encoded
    :class:`ZoneConfig` dict controlling the drawn cell geometry; malformed
    values are a 422. Callers must supply the SAME config that was used when
    the marks were classified — the config is not persisted with the marks, so
    the server cannot recover it (known V1 limitation; omitting it draws the
    default geometry, which is wrong for marks classified under a custom one).
    """
    session = _session_or_404(db, session_id, role)
    config = _parse_zone_config(zone_config)
    balls = _filtered_session_balls(
        db,
        session,
        block,
        bowler_source,
        shot,
        contact,
        control,
        min_confidence,
        include_low_confidence,
    )
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for ball in balls:
        counts[(ball.line.value, ball.length.value)] += 1
    png = render_pitch_map(
        counts,
        title=f"pitch map {session.session_date.isoformat()} ({len(balls)} balls)",
        config=config,
        points=[(ball.pitch_x, ball.pitch_y) for ball in balls],
    )
    return Response(content=png, media_type="image/png")


@router.get("/players/{player_id}/heatmap")
def player_heatmap(
    player_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(*READ_ROLES))],
    min_confidence: Annotated[float, Query(ge=0.0, le=1.0)] = DEFAULT_MIN_CONFIDENCE,
    include_low_confidence: bool = False,
) -> PlayerHeatmapOut:
    """Cross-session trend aggregation (US-C4 exclusion contract).

    Sessions flagged ``calibration_suspect`` are NEVER aggregated into trends;
    ``excluded_sessions`` makes the exclusion visible instead of silent. Each
    session's balls resolve through the US-F4 precedence rules with the same
    ``min_confidence``/``include_low_confidence`` semantics as the session
    endpoint (no ``points`` here: ball numbers only identify balls within one
    session).
    """
    player = db.get(Player, player_id)
    if player is None or (role is Role.PLAYER and player.is_guest):
        # US-L3: guest data is parent/coach-only; hide existence from players.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "player not found")
    sessions = db.scalars(
        select(Session)
        .where(Session.player_id == player_id, Session.calibration_suspect.is_(False))
        .order_by(Session.created_at)
    ).all()
    excluded = db.scalar(
        select(func.count())
        .select_from(Session)
        .where(Session.player_id == player_id, Session.calibration_suspect.is_(True))
    )
    balls: list[_Ball] = []
    for session in sessions:
        balls.extend(
            _session_balls(
                db,
                session,
                min_confidence=min_confidence,
                include_low_confidence=include_low_confidence,
            )
        )
    return PlayerHeatmapOut(
        player_id=player_id,
        total_balls=len(balls),
        cells=_cells(balls),
        excluded_sessions=excluded if excluded is not None else 0,
    )
