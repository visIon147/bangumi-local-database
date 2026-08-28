"""Add bounded discovery sessions and durable review decisions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_discovery_queue"
down_revision: str | None = "0009_rating_review_queue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "discovery_sessions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("filters_json", sa.Text(), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("cursor_position", sa.Integer(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.Column("updated_at", sa.String(32), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint("provider IN ('steam','bangumi_search','bangumi_browse')", name="ck_discovery_sessions_provider"),
        sa.CheckConstraint("status IN ('building','active','completed','cancelled','failed')", name="ck_discovery_sessions_status"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_discovery_sessions_status", "discovery_sessions", ["status"])
    op.create_table(
        "discovery_review_states",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("candidate_key", sa.String(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=True),
        sa.Column("subject_id", sa.Integer(), nullable=True),
        sa.Column("library_entry_id", sa.Integer(), nullable=True),
        sa.Column("decision", sa.String(16), nullable=True),
        sa.Column("reason_private", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.String(32), nullable=True),
        sa.Column("updated_at", sa.String(32), nullable=False),
        sa.CheckConstraint("decision IS NULL OR decision IN ('played','not_played','unsure','deferred')", name="ck_discovery_review_states_decision"),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["library_entry_id"], ["library_entries.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_key", name="uq_discovery_review_states_key"),
    )
    op.create_index("ix_discovery_review_states_work_id", "discovery_review_states", ["work_id"])
    op.create_index("ix_discovery_review_states_subject_id", "discovery_review_states", ["subject_id"])
    op.create_index("ix_discovery_review_states_library_entry_id", "discovery_review_states", ["library_entry_id"])
    op.create_table(
        "discovery_candidates",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("review_state_id", sa.String(36), nullable=True),
        sa.Column("candidate_key", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=True),
        sa.Column("subject_id", sa.Integer(), nullable=True),
        sa.Column("library_entry_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("cover_url", sa.String(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("public_tags_json", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("priority_score", sa.Integer(), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("item_status", sa.String(24), nullable=False),
        sa.Column("decision", sa.String(16), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.String(32), nullable=True),
        sa.CheckConstraint("item_status IN ('pending','decided','suppressed','identity_conflict')", name="ck_discovery_candidates_status"),
        sa.CheckConstraint("decision IS NULL OR decision IN ('played','not_played','unsure','deferred')", name="ck_discovery_candidates_decision"),
        sa.ForeignKeyConstraint(["session_id"], ["discovery_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["review_state_id"], ["discovery_review_states.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["library_entry_id"], ["library_entries.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "candidate_key", name="uq_discovery_candidate_key"),
        sa.UniqueConstraint("session_id", "position", name="uq_discovery_candidate_position"),
    )
    for column in ("session_id", "review_state_id", "work_id", "subject_id", "library_entry_id"):
        op.create_index(f"ix_discovery_candidates_{column}", "discovery_candidates", [column])
    op.create_table(
        "discovery_review_events",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=True),
        sa.Column("candidate_id", sa.String(36), nullable=True),
        sa.Column("review_state_id", sa.String(36), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("previous_decision", sa.String(16), nullable=True),
        sa.Column("next_decision", sa.String(16), nullable=True),
        sa.Column("reason_private", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["discovery_sessions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["candidate_id"], ["discovery_candidates.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["review_state_id"], ["discovery_review_states.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("session_id", "candidate_id", "review_state_id"):
        op.create_index(f"ix_discovery_review_events_{column}", "discovery_review_events", [column])


def downgrade() -> None:
    connection = op.get_bind()
    used = connection.execute(sa.text("SELECT (SELECT COUNT(*) FROM discovery_sessions) + (SELECT COUNT(*) FROM discovery_candidates) + (SELECT COUNT(*) FROM discovery_review_states) + (SELECT COUNT(*) FROM discovery_review_events)" )).scalar_one()
    if used:
        raise RuntimeError("Cannot downgrade while discovery data exists; restore a pre-0010 backup.")
    op.drop_table("discovery_review_events")
    op.drop_table("discovery_candidates")
    op.drop_table("discovery_review_states")
    op.drop_index("ix_discovery_sessions_status", table_name="discovery_sessions")
    op.drop_table("discovery_sessions")
