"""Add persistent, fixed-dataset rating review queues."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_rating_review_queue"
down_revision: str | None = "0008_steam_match_plans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rating_review_states",
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("last_rating", sa.Integer(), nullable=True),
        sa.Column("reason_private", sa.Text(), nullable=True),
        sa.Column("reason_state", sa.String(16), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("reviewed_at", sa.String(32), nullable=True),
        sa.Column("updated_at", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("state IN ('pending','rated','skipped','deferred')", name="ck_rating_review_states_state"),
        sa.CheckConstraint("reason_state IN ('unknown','provided','skipped')", name="ck_rating_review_states_reason_state"),
        sa.CheckConstraint("last_rating IS NULL OR (last_rating BETWEEN 1 AND 10)", name="ck_rating_review_states_rating"),
        sa.ForeignKeyConstraint(["subject_id"], ["bangumi_subjects.subject_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("subject_id"),
    )
    op.create_table(
        "rating_queue_sessions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("selector_json", sa.Text(), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("order_name", sa.String(32), nullable=False),
        sa.Column("random_seed", sa.Integer(), nullable=True),
        sa.Column("cursor_position", sa.Integer(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.Column("updated_at", sa.String(32), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('building','active','completed','cancelled','failed')", name="ck_rating_queue_sessions_status"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rating_queue_sessions_status", "rating_queue_sessions", ["status"])
    op.create_table(
        "rating_queue_items",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("initial_snapshot_json", sa.Text(), nullable=False),
        sa.Column("initial_snapshot_hash", sa.String(64), nullable=False),
        sa.Column("subject_snapshot_json", sa.Text(), nullable=False),
        sa.Column("enrichment_status", sa.String(16), nullable=False),
        sa.Column("item_status", sa.String(16), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=True),
        sa.Column("decided_at", sa.String(32), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint("item_status IN ('pending','completed','stale','suppressed')", name="ck_rating_queue_items_status"),
        sa.CheckConstraint("outcome IS NULL OR outcome IN ('rated','skipped','deferred')", name="ck_rating_queue_items_outcome"),
        sa.CheckConstraint("enrichment_status IN ('local','fresh','failed')", name="ck_rating_queue_items_enrichment"),
        sa.ForeignKeyConstraint(["session_id"], ["rating_queue_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subject_id"], ["bangumi_subjects.subject_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "subject_id", name="uq_rating_queue_item_subject"),
        sa.UniqueConstraint("session_id", "position", name="uq_rating_queue_item_position"),
    )
    op.create_index("ix_rating_queue_items_session_id", "rating_queue_items", ["session_id"])
    op.create_index("ix_rating_queue_items_subject_id", "rating_queue_items", ["subject_id"])
    op.create_table(
        "rating_review_events",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=True),
        sa.Column("queue_item_id", sa.String(36), nullable=True),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("previous_state", sa.String(16), nullable=True),
        sa.Column("next_state", sa.String(16), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("reason_private", sa.Text(), nullable=True),
        sa.Column("reason_state", sa.String(16), nullable=False),
        sa.Column("comment_action", sa.String(32), nullable=False),
        sa.Column("before_hash", sa.String(64), nullable=False),
        sa.Column("after_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["rating_queue_sessions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["queue_item_id"], ["rating_queue_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["subject_id"], ["bangumi_subjects.subject_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rating_review_events_session_id", "rating_review_events", ["session_id"])
    op.create_index("ix_rating_review_events_queue_item_id", "rating_review_events", ["queue_item_id"])
    op.create_index("ix_rating_review_events_subject_id", "rating_review_events", ["subject_id"])


def downgrade() -> None:
    connection = op.get_bind()
    used = connection.execute(sa.text("SELECT (SELECT COUNT(*) FROM rating_review_states) + (SELECT COUNT(*) FROM rating_queue_sessions) + (SELECT COUNT(*) FROM rating_queue_items) + (SELECT COUNT(*) FROM rating_review_events)" )).scalar_one()
    if used:
        raise RuntimeError("Cannot downgrade while rating queue data exists; restore a pre-0009 backup.")
    op.drop_table("rating_review_events")
    op.drop_table("rating_queue_items")
    op.drop_index("ix_rating_queue_sessions_status", table_name="rating_queue_sessions")
    op.drop_table("rating_queue_sessions")
    op.drop_table("rating_review_states")
