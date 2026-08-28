"""Add durable local UI jobs, progress events and apply confirmation nonces."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_ui_jobs"
down_revision: str | None = "0011_media_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ui_jobs",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("capability", sa.String(16), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("progress_current", sa.Integer(), nullable=False),
        sa.Column("progress_total", sa.Integer(), nullable=True),
        sa.Column("phase", sa.String(64), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.Column("started_at", sa.String(32), nullable=True),
        sa.Column("heartbeat_at", sa.String(32), nullable=True),
        sa.Column("finished_at", sa.String(32), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancel_requested','cancelled','interrupted')",
            name="ck_ui_jobs_status",
        ),
        sa.CheckConstraint(
            "capability IN ('local_read','local_write','remote_read','remote_write')",
            name="ck_ui_jobs_capability",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ui_jobs_kind", "ui_jobs", ["kind"])
    op.create_index("ix_ui_jobs_status", "ui_jobs", ["status"])
    op.create_index(
        "uq_ui_jobs_idempotency_key",
        "ui_jobs",
        ["idempotency_key"],
        unique=True,
        sqlite_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_table(
        "ui_job_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("phase", sa.String(64), nullable=True),
        sa.Column("progress_current", sa.Integer(), nullable=True),
        sa.Column("progress_total", sa.Integer(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.CheckConstraint(
            "level IN ('debug','info','warning','error')", name="ck_ui_job_events_level"
        ),
        sa.ForeignKeyConstraint(["job_id"], ["ui_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "sequence", name="uq_ui_job_events_sequence"),
    )
    op.create_index("ix_ui_job_events_job_id", "ui_job_events", ["job_id"])
    op.create_table(
        "plan_confirmation_nonces",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("nonce_hash", sa.String(64), nullable=False),
        sa.Column("browser_session_hash", sa.String(64), nullable=False),
        sa.Column("plan_id", sa.String(36), nullable=False),
        sa.Column("plan_content_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.String(32), nullable=False),
        sa.Column("used_at", sa.String(32), nullable=True),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(["plan_id"], ["change_plans.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("nonce_hash"),
    )
    op.create_index(
        "ix_plan_confirmation_nonces_browser_session_hash",
        "plan_confirmation_nonces",
        ["browser_session_hash"],
    )
    op.create_index(
        "ix_plan_confirmation_nonces_plan_id", "plan_confirmation_nonces", ["plan_id"]
    )
    op.create_index(
        "ix_plan_confirmation_nonces_expires_at",
        "plan_confirmation_nonces",
        ["expires_at"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    used = connection.execute(
        sa.text(
            "SELECT (SELECT COUNT(*) FROM ui_jobs) + "
            "(SELECT COUNT(*) FROM ui_job_events) + "
            "(SELECT COUNT(*) FROM plan_confirmation_nonces)"
        )
    ).scalar_one()
    if used:
        raise RuntimeError(
            "Cannot downgrade while UI job/confirmation data exists; restore a pre-0012 backup."
        )
    op.drop_table("plan_confirmation_nonces")
    op.drop_table("ui_job_events")
    op.drop_index("uq_ui_jobs_idempotency_key", table_name="ui_jobs")
    op.drop_index("ix_ui_jobs_status", table_name="ui_jobs")
    op.drop_index("ix_ui_jobs_kind", table_name="ui_jobs")
    op.drop_table("ui_jobs")
