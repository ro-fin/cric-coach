"""US-A5: capture safety envelope — the software gate before machine sessions.

The required items mirror the "Safety envelope (US-A5)" section of
``docs/camera_setup.md``: rated netting/protection, an anchored machine,
protected corridor cameras, a clear ball path, helmet for machine batting,
and a tested emergency stop. A machine session cannot leave ``created``
until a parent acknowledges every item (lifecycle router enforces the
other half of the contract via ``Session.checklist_ack_id``).
"""

import uuid
from datetime import datetime
from typing import Annotated

from cricai_data.enums import Role
from cricai_data.lifecycle import SessionState
from cricai_data.models import AuditLog, ChecklistAck, Session
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db

router = APIRouter(tags=["checklists"])

#: (item_id, human label) pairs — every one must be acknowledged true before a
#: machine session may start. Documented in docs/camera_setup.md ("Safety
#: envelope (US-A5)").
REQUIRED_MACHINE_CHECKLIST: tuple[tuple[str, str], ...] = (
    ("netting_integrity", "Netting inspected and intact around the full corridor"),
    ("machine_anchored", "Bowling machine anchored and stable"),
    ("cameras_protected", "Corridor cameras behind rated polycarbonate/netting"),
    ("ball_path_clear", "Ball path clear of people and loose equipment"),
    ("helmet_on", "Batter wearing a helmet for machine batting"),
    ("estop_tested", "Emergency stop tested and within reach"),
)


class ChecklistItemOut(BaseModel):
    id: str
    label: str


class MachineChecklistOut(BaseModel):
    items: list[ChecklistItemOut]


class ChecklistAckIn(BaseModel):
    items: dict[str, bool]
    acked_by: str = Field(min_length=1, max_length=64)


class ChecklistAckOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    items: dict[str, bool]
    acked_by: str
    acked_at: datetime


@router.get("/checklists/machine")
def get_machine_checklist(
    _role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
) -> MachineChecklistOut:
    return MachineChecklistOut(
        items=[
            ChecklistItemOut(id=item_id, label=label)
            for item_id, label in REQUIRED_MACHINE_CHECKLIST
        ]
    )


@router.post("/sessions/{session_id}/checklist-ack", status_code=status.HTTP_201_CREATED)
def ack_checklist(
    session_id: uuid.UUID,
    payload: ChecklistAckIn,
    db: Annotated[OrmSession, Depends(get_db)],
    _role: Annotated[Role, Depends(require_roles(Role.PARENT))],
) -> ChecklistAckOut:
    session = db.get(Session, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    if session.state is not SessionState.CREATED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"session already started (state={session.state}); checklist is immutable",
        )

    missing = [item_id for item_id, _ in REQUIRED_MACHINE_CHECKLIST if item_id not in payload.items]
    unchecked = [
        item_id for item_id, _ in REQUIRED_MACHINE_CHECKLIST if payload.items.get(item_id) is False
    ]
    if missing or unchecked:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {
                "message": "all safety checklist items must be acknowledged true",
                "missing": missing,
                "false": unchecked,
            },
        )

    replaced_ack_id = session.checklist_ack_id  # re-ack before start replaces
    ack = ChecklistAck(items=payload.items, acked_by=payload.acked_by)
    db.add(ack)
    db.flush()
    session.checklist_ack_id = ack.id
    db.add(
        AuditLog(
            actor=payload.acked_by,
            action="checklist_ack",
            entity="session",
            entity_id=str(session.id),
            detail={
                "ack_id": str(ack.id),
                "items": payload.items,
                "replaced_ack_id": str(replaced_ack_id) if replaced_ack_id else None,
            },
        )
    )
    db.flush()
    return ChecklistAckOut.model_validate(ack)
