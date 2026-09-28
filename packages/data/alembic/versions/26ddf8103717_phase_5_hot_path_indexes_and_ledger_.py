"""phase 5 hot path indexes and ledger auto backfill backstop

Non-unique indexes for the Phase-5 hot lookups (PostgreSQL does not auto-index
FK columns): findings by session, ledger by player+date, alerts inbox by
audience+acknowledged, pipeline runs/stages by session/run, wellness check-ins
by player+date, reports by player+kind, metric baselines by player+metric.

Plus the US-H1 DB backstop for the backfill job's idempotency invariant:
``backfill_workload`` delete-and-recreates AT MOST ONE ``source='auto_backfill'``
ledger row per session, but that guarantee is application-level only — two
overlapping runs under READ COMMITTED both insert and silently double-count a
child's bowling workload. The partial unique index makes the second commit die
loudly instead. PostgreSQL-only DDL (partial unique index); SQLite unit
databases exercise the application-level delete-then-insert guard.

Revision ID: 26ddf8103717
Revises: 14d5a3cd5357
Create Date: 2026-07-10 23:42:20.486301
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "26ddf8103717"
down_revision: str | None = "14d5a3cd5357"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_alerts_audience_acknowledged", "alerts", ["audience", "acknowledged"], unique=False
    )
    op.create_index(
        "ix_ledger_player_id_entry_date",
        "bowling_ledger_entries",
        ["player_id", "entry_date"],
        unique=False,
    )
    op.create_index("ix_findings_session_id", "findings", ["session_id"], unique=False)
    op.create_index(
        "ix_baselines_player_id_metric", "metric_baselines", ["player_id", "metric"], unique=False
    )
    op.create_index("ix_pipeline_runs_session_id", "pipeline_runs", ["session_id"], unique=False)
    op.create_index("ix_pipeline_stages_run_id", "pipeline_stages", ["run_id"], unique=False)
    op.create_index("ix_reports_player_id_kind", "reports", ["player_id", "kind"], unique=False)
    op.create_index(
        "ix_wellness_player_id_checkin_date",
        "wellness_checkins",
        ["player_id", "checkin_date"],
        unique=False,
    )
    if op.get_bind().dialect.name != "postgresql":
        return
    op.create_index(
        "uq_ledger_auto_backfill_session_intensity",
        "bowling_ledger_entries",
        ["session_id", "intensity"],
        unique=True,
        postgresql_where=sa.text("source = 'auto_backfill'"),
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_index(
            "uq_ledger_auto_backfill_session_intensity", table_name="bowling_ledger_entries"
        )
    op.drop_index("ix_wellness_player_id_checkin_date", table_name="wellness_checkins")
    op.drop_index("ix_reports_player_id_kind", table_name="reports")
    op.drop_index("ix_pipeline_stages_run_id", table_name="pipeline_stages")
    op.drop_index("ix_pipeline_runs_session_id", table_name="pipeline_runs")
    op.drop_index("ix_baselines_player_id_metric", table_name="metric_baselines")
    op.drop_index("ix_findings_session_id", table_name="findings")
    op.drop_index("ix_ledger_player_id_entry_date", table_name="bowling_ledger_entries")
    op.drop_index("ix_alerts_audience_acknowledged", table_name="alerts")
