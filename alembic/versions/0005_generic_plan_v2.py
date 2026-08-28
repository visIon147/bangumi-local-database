"""Add versioned generic collection mutation plan fields."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_generic_plan_v2"
down_revision: str | None = "0004_generic_works"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("change_plans") as batch:
        batch.add_column(
            sa.Column("format_version", sa.Integer(), nullable=False, server_default="1")
        )
    with op.batch_alter_table("change_plan_items") as batch:
        batch.add_column(
            sa.Column("changed_fields_json", sa.Text(), nullable=False, server_default="[]")
        )
        batch.add_column(sa.Column("local_precondition_hash", sa.String(64), nullable=True))
    with op.batch_alter_table("remote_operations") as batch:
        batch.add_column(sa.Column("request_payload_json", sa.Text(), nullable=True))


def downgrade() -> None:
    connection = op.get_bind()
    unsafe_generic_data = connection.execute(
        sa.text(
            """
            SELECT
              (SELECT COUNT(*) FROM works w
               LEFT JOIN bangumi_subjects b ON b.work_id = w.id
               WHERE w.kind != 'game' OR b.subject_id IS NULL OR b.subject_type != 4)
              +
              (SELECT COUNT(*) FROM work_links
               WHERE match_source IS NOT NULL
                  OR match_confidence IS NOT NULL
                  OR verified_at IS NOT NULL)
              +
              (SELECT CASE WHEN
                   (SELECT COUNT(*) FROM works) != (SELECT COUNT(*) FROM bangumi_subjects)
                OR (SELECT COUNT(*) FROM works) != (SELECT COUNT(*) FROM bangumi_collection_states)
                OR (SELECT COUNT(*) FROM works) != (SELECT COUNT(*) FROM game_profiles)
                THEN 1 ELSE 0 END)
            """
        )
    ).scalar_one()
    if unsafe_generic_data:
        raise RuntimeError(
            "Cannot downgrade after non-game, unlinked, enriched, or incomplete "
            "Phase 4 data was added; restore the pre-upgrade backup."
        )
    v2_count = connection.execute(
        sa.text("SELECT COUNT(*) FROM change_plans WHERE format_version != 1")
    ).scalar_one()
    if v2_count:
        raise RuntimeError("Cannot downgrade while v2 plans exist; restore a pre-Phase-4 backup.")
    populated_v2_fields = connection.execute(
        sa.text(
            """
            SELECT
              (SELECT COUNT(*) FROM change_plan_items
               WHERE changed_fields_json != '[]' OR local_precondition_hash IS NOT NULL)
              +
              (SELECT COUNT(*) FROM remote_operations
               WHERE request_payload_json IS NOT NULL)
            """
        )
    ).scalar_one()
    if populated_v2_fields:
        raise RuntimeError(
            "Cannot downgrade while Phase 4 plan/audit fields contain data; "
            "restore a pre-Phase-4 backup."
        )
    with op.batch_alter_table("remote_operations") as batch:
        batch.drop_column("request_payload_json")
    with op.batch_alter_table("change_plan_items") as batch:
        batch.drop_column("local_precondition_hash")
        batch.drop_column("changed_fields_json")
    with op.batch_alter_table("change_plans") as batch:
        batch.drop_column("format_version")
