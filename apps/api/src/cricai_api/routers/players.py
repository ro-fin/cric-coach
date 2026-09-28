"""Player registry (foundation for US-B1; guest segregation per US-L3)."""

import uuid
from datetime import date
from typing import Annotated

from cricai_data.enums import Handedness, Role
from cricai_data.models import Player
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from cricai_api.auth import require_roles
from cricai_api.deps import get_db

router = APIRouter(prefix="/players", tags=["players"])


class PlayerIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    birthdate: date
    handedness: Handedness = Handedness.RIGHT
    is_guest: bool = False


class PlayerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    birthdate: date
    handedness: Handedness
    is_guest: bool


@router.post("", status_code=status.HTTP_201_CREATED)
def create_player(
    payload: PlayerIn,
    db: Annotated[Session, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT))],
) -> PlayerOut:
    player = Player(
        name=payload.name,
        birthdate=payload.birthdate,
        handedness=payload.handedness,
        is_guest=payload.is_guest,
    )
    db.add(player)
    db.flush()
    return PlayerOut.model_validate(player)


@router.get("")
def list_players(
    db: Annotated[Session, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
) -> list[PlayerOut]:
    query = select(Player).order_by(Player.name)
    if role is Role.PLAYER:  # US-L3: guest data is parent/coach-only
        query = query.where(Player.is_guest.is_(False))
    players = db.scalars(query).all()
    return [PlayerOut.model_validate(p) for p in players]


@router.get("/{player_id}")
def get_player(
    player_id: uuid.UUID,
    db: Annotated[Session, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
) -> PlayerOut:
    player = db.get(Player, player_id)
    if player is None or (role is Role.PLAYER and player.is_guest):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "player not found")
    return PlayerOut.model_validate(player)
