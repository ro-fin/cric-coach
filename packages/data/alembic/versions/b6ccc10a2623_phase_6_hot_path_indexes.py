"""phase 6 hot path indexes

Non-unique indexes for the Phase-6 hot lookups, following the phase-5
precedent (PostgreSQL does not auto-index FK columns) — finding 26:
coach_notes by player (+ optional session: notes list, privacy cascade, US-D4
renumbering probes), bowling_targets by session (target list/scoring, privacy
cascade), delivery_labels by session (label list/export, privacy cascade), and
reports by status + review_due_at (US-J5 coach review queue and publish sweep).

Revision ID: b6ccc10a2623
Revises: 3b86319159fe
Create Date: 2026-07-11 11:50:59.270334
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b6ccc10a2623"
down_revision: str | None = "3b86319159fe"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_bowling_targets_session_id", "bowling_targets", ["session_id"], unique=False
    )
    op.create_index(
        "ix_coach_notes_player_id_session_id",
        "coach_notes",
        ["player_id", "session_id"],
        unique=False,
    )
    op.create_index(
        "ix_delivery_labels_session_id", "delivery_labels", ["session_id"], unique=False
    )
    op.create_index(
        "ix_reports_status_review_due_at", "reports", ["status", "review_due_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_reports_status_review_due_at", table_name="reports")
    op.drop_index("ix_delivery_labels_session_id", table_name="delivery_labels")
    op.drop_index("ix_coach_notes_player_id_session_id", table_name="coach_notes")
    op.drop_index("ix_bowling_targets_session_id", table_name="bowling_targets")
