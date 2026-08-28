"""Add Phase 3 immutable plans and remote-write audit tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_phase3"
down_revision: str | None = "0002_phase2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "change_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("tag", sa.String(), nullable=True),
        sa.Column("old_tag", sa.String(), nullable=True),
        sa.Column("new_tag", sa.String(), nullable=True),
        sa.Column("selector_json", sa.Text(), nullable=False),
        sa.Column("summary_json", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("created_by", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("reverse_of_plan_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.Column("reviewed_at", sa.String(32), nullable=True),
        sa.Column("applied_at", sa.String(32), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft', 'reviewed', 'applying', 'applied', 'partial', 'failed', 'cancelled')",
            name="ck_change_plans_status",
        ),
        sa.ForeignKeyConstraint(["reverse_of_plan_id"], ["change_plans.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_change_plans_status", "change_plans", ["status"])
    op.create_index("ix_change_plans_reverse_of_plan_id", "change_plans", ["reverse_of_plan_id"])

    op.create_table(
        "change_plan_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("plan_id", sa.String(36), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("bgm_url", sa.String(), nullable=False),
        sa.Column("disposition", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("action_json", sa.Text(), nullable=False),
        sa.Column("selection_evidence_json", sa.Text(), nullable=False),
        sa.Column("before_snapshot_json", sa.Text(), nullable=False),
        sa.Column("intended_snapshot_json", sa.Text(), nullable=True),
        sa.Column("before_tags_json", sa.Text(), nullable=False),
        sa.Column("after_tags_json", sa.Text(), nullable=True),
        sa.Column("public_tags_json", sa.Text(), nullable=False),
        sa.Column("precondition_hash", sa.String(64), nullable=True),
        sa.Column("item_status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint("disposition IN ('planned', 'unchanged')", name="ck_plan_items_disposition"),
        sa.CheckConstraint(
            "item_status IN ('not_applicable', 'pending', 'applied', 'stale', 'failed')",
            name="ck_plan_items_status",
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["change_plans.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("plan_id", "subject_id", name="uq_plan_items_subject"),
    )
    op.create_index("ix_change_plan_items_plan_id", "change_plan_items", ["plan_id"])
    op.create_index("ix_change_plan_items_game_id", "change_plan_items", ["game_id"])

    op.create_table(
        "plan_apply_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("plan_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("backup_path", sa.String(), nullable=False),
        sa.Column("started_at", sa.String(32), nullable=False),
        sa.Column("finished_at", sa.String(32), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'applied', 'partial', 'failed', 'aborted')",
            name="ck_plan_apply_runs_status",
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["change_plans.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_plan_apply_runs_plan_id", "plan_apply_runs", ["plan_id"])

    op.create_table(
        "remote_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("plan_id", sa.String(36), nullable=False),
        sa.Column("plan_item_id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("before_snapshot_json", sa.Text(), nullable=False),
        sa.Column("intended_snapshot_json", sa.Text(), nullable=False),
        sa.Column("actual_snapshot_json", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.String(32), nullable=False),
        sa.Column("finished_at", sa.String(32), nullable=True),
        sa.CheckConstraint(
            "status IN ('started', 'applied', 'failed', 'uncertain')",
            name="ck_remote_operations_status",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["plan_apply_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["plan_id"], ["change_plans.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["plan_item_id"], ["change_plan_items.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_remote_operations_run_id", "remote_operations", ["run_id"])
    op.create_index("ix_remote_operations_plan_id", "remote_operations", ["plan_id"])
    op.create_index("ix_remote_operations_subject_id", "remote_operations", ["subject_id"])


def downgrade() -> None:
    op.drop_index("ix_remote_operations_subject_id", table_name="remote_operations")
    op.drop_index("ix_remote_operations_plan_id", table_name="remote_operations")
    op.drop_index("ix_remote_operations_run_id", table_name="remote_operations")
    op.drop_table("remote_operations")
    op.drop_index("ix_plan_apply_runs_plan_id", table_name="plan_apply_runs")
    op.drop_table("plan_apply_runs")
    op.drop_index("ix_change_plan_items_game_id", table_name="change_plan_items")
    op.drop_index("ix_change_plan_items_plan_id", table_name="change_plan_items")
    op.drop_table("change_plan_items")
    op.drop_index("ix_change_plans_reverse_of_plan_id", table_name="change_plans")
    op.drop_index("ix_change_plans_status", table_name="change_plans")
    op.drop_table("change_plans")
