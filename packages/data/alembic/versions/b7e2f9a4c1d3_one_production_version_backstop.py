"""one production version per model backstop

Partial unique index enforcing the US-F2 invariant "exactly one production
version per model name" at the database level. The models API promotes under a
read-then-write gate that two concurrent requests can both pass; this index is
the backstop that makes the second commit fail loudly instead of leaving two
``stage='production'`` rows for one model name. PostgreSQL-only DDL (partial
index); SQLite unit databases exercise the application-level gate.

Revision ID: b7e2f9a4c1d3
Revises: c24273f059cf
Create Date: 2026-07-09 03:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e2f9a4c1d3"
down_revision: str | None = "c24273f059cf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.create_index(
        "uq_model_versions_one_production",
        "model_versions",
        ["model_name"],
        unique=True,
        postgresql_where=sa.text("stage = 'production'"),
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_index("uq_model_versions_one_production", table_name="model_versions")
