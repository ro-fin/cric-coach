"""US-G5/US-K4: milestone log — personal bests, volume and streak markers.

Read-only feed for the progress dashboard (US-K4): the rollup job
(``cricai_worker.rollup_reports``) is the only writer, guarded by the
progress agent's statistical guardrails, so nothing here can invent an
achievement. Guardian roles AND the player may read — milestones are the
celebratory surface built for the child (US-K4 kid mode).
"""

import uuid
from datetime import date, datetime
from typing import Annotated, Any

from cricai_data.enums import MilestoneKind, Role
from cricai_data.models import Milestone, Player
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db

router = APIRouter(prefix="/milestones", tags=["milestones"])


class MilestoneOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    player_id: uuid.UUID
    kind: MilestoneKind
    metric: str
    value: float
    context: dict[str, Any]
    achieved_on: date
    created_at: datetime


@router.get("/players/{player_id}")
def list_milestones(
    player_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
    kind: MilestoneKind | None = None,
) -> list[MilestoneOut]:
    """One player's milestones, newest first (US-K4 feed), optionally by kind.

    Ordering is deterministic: achieved date desc, then kind and metric (the
    milestone unique key), so the dashboard's data-parity check is stable.
    """
    if db.get(Player, player_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "player not found")
    query = select(Milestone).where(Milestone.player_id == player_id)
    if kind is not None:
        query = query.where(Milestone.kind == kind)
    query = query.order_by(Milestone.achieved_on.desc(), Milestone.kind, Milestone.metric)
    return [MilestoneOut.model_validate(row) for row in db.scalars(query)]
