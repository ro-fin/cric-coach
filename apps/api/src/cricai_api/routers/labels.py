"""US-I6: delivery labels — per-ball leg-spin variation ground truth.

``variation_intent`` is what the bowler/coach MEANT to bowl: manual, human,
guardian-written ground truth (the tags.py discipline — every intent edit is
audit-logged, import/export round-trips losslessly through the labelio
delivery-task channel). ``variation_detected`` is the classifier's honest
output and is writable ONLY through the worker seam
(``cricai_worker.classify_variations``): no endpoint here accepts it, so the
two can never be conflated (contract #5) and a manual intent always survives a
model re-run.
"""

import hashlib
import uuid
from typing import Annotated, Any

from cricai_data.enums import BowlingVariation, Role
from cricai_data.labelio import (
    ImportedDelivery,
    LabelIOError,
    deliveries_from_label_studio,
    to_delivery_label_task,
)
from cricai_data.models import AuditLog, DeliveryLabel, Session
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from cricai_api.auth import require_roles
from cricai_api.deps import get_db

router = APIRouter(prefix="/sessions/{session_id}/labels", tags=["labels"])

MANUAL_SOURCE = "manual"


class LabelIn(BaseModel):
    ball_no: int = Field(ge=1)
    variation_intent: BowlingVariation


class LabelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ball_no: int
    variation_intent: BowlingVariation
    variation_detected: BowlingVariation | None
    labeler: str
    source: str


class LabelPatch(BaseModel):
    variation_intent: BowlingVariation


class ImportResult(BaseModel):
    created: int
    updated: int
    audits_written: int


def _get_session_or_404(db: OrmSession, session_id: uuid.UUID, role: Role) -> Session:
    session = db.get(Session, session_id)
    if session is None or (role is Role.PLAYER and session.player.is_guest):
        # US-L3: guest data is parent/coach-only — hide existence from players
        # (the metrics/events/clips reader idiom; finding 56).
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    return session


def _get_label(db: OrmSession, session_id: uuid.UUID, ball_no: int) -> DeliveryLabel | None:
    return db.scalar(
        select(DeliveryLabel).where(
            DeliveryLabel.session_id == session_id, DeliveryLabel.ball_no == ball_no
        )
    )


def _set_intent(db: OrmSession, label: DeliveryLabel, intent: BowlingVariation, actor: str) -> int:
    """Assert one human intent onto an existing label (PATCH + import path).

    A human touching the intent forces provenance back to manual (a model
    pre-label the coach confirmed is now ground truth); an actual value change
    writes one audit-trail row (who, old -> new). ``variation_detected`` is
    NEVER touched — that column belongs to the worker seam alone.
    """
    audits = 0
    if label.variation_intent is not intent:
        db.add(
            AuditLog(
                actor=actor,
                action="delivery_label_edit",
                entity="delivery_label",
                entity_id=str(label.id),
                detail={
                    "ball_no": label.ball_no,
                    "field": "variation_intent",
                    "old_value": label.variation_intent.value,
                    "new_value": intent.value,
                },
            )
        )
        label.variation_intent = intent
        audits = 1
    label.source = MANUAL_SOURCE
    label.labeler = actor
    return audits


@router.post("", status_code=status.HTTP_201_CREATED)
def create_label(
    session_id: uuid.UUID,
    payload: LabelIn,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> LabelOut:
    """Declare one delivery's intent (guardian-only manual ground truth)."""
    _get_session_or_404(db, session_id, role)
    if _get_label(db, session_id, payload.ball_no) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"ball {payload.ball_no} already labeled; use PATCH to edit the intent",
        )
    label = DeliveryLabel(
        session_id=session_id,
        ball_no=payload.ball_no,
        variation_intent=payload.variation_intent,
        labeler=role.value,
        source=MANUAL_SOURCE,
    )
    db.add(label)
    try:
        db.flush()
    except IntegrityError as exc:
        # TOCTOU: a concurrent request labeled this ball between the existence
        # check and the flush — uq_delivery_label_session_ball is the real
        # guard; surface this endpoint's own 409, never a 500 (finding 17).
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"ball {payload.ball_no} already labeled; use PATCH to edit the intent",
        ) from exc
    return LabelOut.model_validate(label)


@router.patch("/{ball_no}")
def edit_label(
    session_id: uuid.UUID,
    ball_no: int,
    payload: LabelPatch,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> LabelOut:
    """Correct one delivery's intent; the change is audit-logged (who, old->new)."""
    _get_session_or_404(db, session_id, role)
    label = _get_label(db, session_id, ball_no)
    if label is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "delivery label not found")
    _set_intent(db, label, payload.variation_intent, actor=role.value)
    db.flush()
    return LabelOut.model_validate(label)


@router.get("")
def list_labels(
    session_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH, Role.PLAYER))],
) -> list[LabelOut]:
    """Every delivery label of the session — intent and honest detection side
    by side, never merged (US-I6: the report shows agreement, not assertion).
    Player reads are scoped away from guest sessions (US-L3, finding 56)."""
    _get_session_or_404(db, session_id, role)
    labels = db.scalars(
        select(DeliveryLabel)
        .where(DeliveryLabel.session_id == session_id)
        .order_by(DeliveryLabel.ball_no)
    ).all()
    return [LabelOut.model_validate(label) for label in labels]


@router.get("/export")
def export_labels(
    session_id: uuid.UUID,
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT, Role.COACH))],
) -> JSONResponse:
    """Export the session's labels as Label Studio delivery tasks (US-I6).

    Guardian roles only; every call is audit-logged with the payload digest
    (US-L3 sharing discipline, the tags-export pattern).
    """
    _get_session_or_404(db, session_id, role)
    labels = db.scalars(
        select(DeliveryLabel)
        .where(DeliveryLabel.session_id == session_id)
        .order_by(DeliveryLabel.ball_no)
    ).all()
    response = JSONResponse([to_delivery_label_task(label) for label in labels])
    db.add(
        AuditLog(
            actor=role.value,
            action="share_export",
            entity="session",
            entity_id=str(session_id),
            detail={
                "format": "label_studio",
                "sha256": hashlib.sha256(response.body).hexdigest(),
                "surface": "labels_export",
            },
        )
    )
    return response


def _import_one(
    db: OrmSession, session_id: uuid.UUID, delivery: ImportedDelivery, actor: str
) -> tuple[int, int, int]:
    """(created, updated, audits) for one imported delivery task."""
    if delivery.provenance.session_id != str(session_id):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"ball {delivery.provenance.ball_no} belongs to session "
            f"{delivery.provenance.session_id}, not {session_id} — provenance is "
            "never reassigned (US-F1 discipline)",
        )
    label = _get_label(db, session_id, delivery.provenance.ball_no)
    if label is None:
        db.add(
            DeliveryLabel(
                session_id=session_id,
                ball_no=delivery.provenance.ball_no,
                variation_intent=delivery.variation_intent,
                labeler=actor,
                source=MANUAL_SOURCE,
            )
        )
        try:
            db.flush()  # per-delivery, so a uq race names the conflicting ball
        except IntegrityError as exc:
            # TOCTOU: a manual label landed for this ball between the check and
            # the flush (finding 17). The whole import rolls back (atomic); a
            # retry takes the update path for this ball and succeeds.
            db.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"ball {delivery.provenance.ball_no} was labeled concurrently; retry the import",
            ) from exc
        return 1, 0, 0
    audits = _set_intent(db, label, delivery.variation_intent, actor)
    return 0, 1 if audits else 0, audits


@router.post("/import")
def import_labels(
    session_id: uuid.UUID,
    payload: list[dict[str, Any]],
    db: Annotated[OrmSession, Depends(get_db)],
    role: Annotated[Role, Depends(require_roles(Role.PARENT))],
) -> ImportResult:
    """Upsert intents from a labelio delivery-task export; provenance is forced
    back to manual. Any ``variation_detected`` in the file is review context
    and is deliberately IGNORED: model output enters only via the worker seam.
    """
    _get_session_or_404(db, session_id, role)
    try:
        deliveries = deliveries_from_label_studio(payload)
    except LabelIOError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    created = updated = audits_written = 0
    for delivery in deliveries:
        one_created, one_updated, one_audits = _import_one(db, session_id, delivery, role.value)
        created += one_created
        updated += one_updated
        audits_written += one_audits
    db.flush()
    return ImportResult(created=created, updated=updated, audits_written=audits_written)
