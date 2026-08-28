"""Add Phase 2 sync conflict persistence."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_phase2"
down_revision: str | None = "0001_phase1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sync_conflicts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("field", sa.String(16), nullable=False),
        sa.Column("base_json", sa.Text(), nullable=False),
        sa.Column("local_json", sa.Text(), nullable=False),
        sa.Column("remote_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.Column("resolved_at", sa.String(32), nullable=True),
        sa.CheckConstraint(
            "status IN ('open', 'resolved', 'ignored')", name="ck_conflicts_status"
        ),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_sync_conflicts_game_id", "sync_conflicts", ["game_id"])
    op.create_index("ix_sync_conflicts_status", "sync_conflicts", ["status"])


def downgrade() -> None:
    op.drop_index("ix_sync_conflicts_status", table_name="sync_conflicts")
    op.drop_index("ix_sync_conflicts_game_id", table_name="sync_conflicts")
    op.drop_table("sync_conflicts")

