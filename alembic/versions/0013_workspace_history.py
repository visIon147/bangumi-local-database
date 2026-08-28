"""Add workspace archival state and durable job-plan links."""

from collections.abc import Sequence
import json

import sqlalchemy as sa
from alembic import op

revision: str = "0013_workspace_history"
down_revision: str | None = "0012_ui_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_PLAN_CREATING_JOBS = {
    "bangumi_pull_plan",
    "bulk_tag_plan",
    "classify_games_plan",
    "discovery_status_draft",
    "rating_sync_plan",
    "steam_match_plan",
    "steam_status_plan",
    "sync_plan",
}


def _backfill_links(connection: sa.Connection) -> None:
    plan_ids = {
        str(value)
        for value in connection.execute(sa.text("SELECT id FROM change_plans")).scalars()
    }
    rows = connection.execute(
        sa.text("SELECT id, kind, result_json, created_at FROM ui_jobs WHERE result_json IS NOT NULL")
    ).mappings()
    for row in rows:
        try:
            result = json.loads(row["result_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(result, dict):
            continue
        links: list[tuple[str, str]] = []
        value = result.get("plan_id")
        if isinstance(value, str):
            links.append((value, "created" if row["kind"] in _PLAN_CREATING_JOBS else "source"))
        for key, relation in (
            ("source_plan_id", "source"),
            ("reverse_plan_id", "reverse"),
            ("restore_plan_id", "restore"),
            ("supersedes", "supersedes"),
        ):
            value = result.get(key)
            if isinstance(value, str):
                links.append((value, relation))
        for plan_id, relation in dict.fromkeys(links):
            if plan_id not in plan_ids:
                continue
            connection.execute(
                sa.text(
                    "INSERT OR IGNORE INTO ui_job_plan_links "
                    "(job_id, plan_id, relation, created_at) "
                    "VALUES (:job_id, :plan_id, :relation, :created_at)"
                ),
                {
                    "job_id": row["id"],
                    "plan_id": plan_id,
                    "relation": relation,
                    "created_at": row["created_at"],
                },
            )


def upgrade() -> None:
    op.add_column("ui_jobs", sa.Column("archived_at", sa.String(32), nullable=True))
    op.create_index("ix_ui_jobs_archived_at", "ui_jobs", ["archived_at"])
    op.add_column("change_plans", sa.Column("archived_at", sa.String(32), nullable=True))
    op.create_index("ix_change_plans_archived_at", "change_plans", ["archived_at"])
    op.create_table(
        "ui_job_plan_links",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("plan_id", sa.String(36), nullable=False),
        sa.Column("relation", sa.String(16), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.CheckConstraint(
            "relation IN ('created', 'source', 'reverse', 'restore', 'supersedes')",
            name="ck_ui_job_plan_links_relation",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["ui_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_id"], ["change_plans.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "plan_id", "relation", name="uq_ui_job_plan_link"),
    )
    op.create_index("ix_ui_job_plan_links_job_id", "ui_job_plan_links", ["job_id"])
    op.create_index("ix_ui_job_plan_links_plan_id", "ui_job_plan_links", ["plan_id"])
    _backfill_links(op.get_bind())


def downgrade() -> None:
    connection = op.get_bind()
    archived = connection.execute(
        sa.text(
            "SELECT (SELECT COUNT(*) FROM ui_jobs WHERE archived_at IS NOT NULL) + "
            "(SELECT COUNT(*) FROM change_plans WHERE archived_at IS NOT NULL)"
        )
    ).scalar_one()
    if archived:
        raise RuntimeError(
            "Cannot downgrade while workspace archive state exists; restore a pre-0013 backup."
        )
    op.drop_index("ix_ui_job_plan_links_plan_id", table_name="ui_job_plan_links")
    op.drop_index("ix_ui_job_plan_links_job_id", table_name="ui_job_plan_links")
    op.drop_table("ui_job_plan_links")
    op.drop_index("ix_change_plans_archived_at", table_name="change_plans")
    op.drop_column("change_plans", "archived_at")
    op.drop_index("ix_ui_jobs_archived_at", table_name="ui_jobs")
    op.drop_column("ui_jobs", "archived_at")
