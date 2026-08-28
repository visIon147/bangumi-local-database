"""Add plan-linked audit evidence for controlled automatic Steam matching."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_steam_match_plans"
down_revision: str | None = "0007_plan_v3_collection_create"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("library_match_reviews") as batch:
        batch.add_column(sa.Column("plan_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("score", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("evidence_json", sa.Text(), nullable=False, server_default="{}")
        )
        batch.create_foreign_key(
            "fk_library_match_reviews_plan",
            "change_plans",
            ["plan_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "ix_library_match_reviews_plan_id",
        "library_match_reviews",
        ["plan_id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    used = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM library_match_reviews "
            "WHERE plan_id IS NOT NULL OR score IS NOT NULL OR evidence_json != '{}'"
        )
    ).scalar_one()
    if used:
        raise RuntimeError(
            "Cannot downgrade while batch Steam match audit evidence exists; "
            "restore a pre-0008 backup."
        )
    op.drop_index("ix_library_match_reviews_plan_id", table_name="library_match_reviews")
    with op.batch_alter_table("library_match_reviews") as batch:
        batch.drop_constraint("fk_library_match_reviews_plan", type_="foreignkey")
        batch.drop_column("evidence_json")
        batch.drop_column("score")
        batch.drop_column("plan_id")
