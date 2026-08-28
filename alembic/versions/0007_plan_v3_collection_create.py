"""Add v3 plan support for Steam evidence and collection lifecycle operations."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_plan_v3_collection_create"
down_revision: str | None = "0006_steam_library"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQLITE_NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}


def _drop_empty_interrupted_batch_table() -> None:
    """Allow a safe retry after SQLite stopped at the old-table DROP step."""
    connection = op.get_bind()
    temporary = "_alembic_tmp_change_plan_items"
    if temporary not in sa.inspect(connection).get_table_names():
        return
    row_count = connection.execute(
        sa.text(f'SELECT COUNT(*) FROM "{temporary}"')
    ).scalar_one()
    if row_count:
        raise RuntimeError(
            "Interrupted Alembic temporary table contains rows; restore the pre-upgrade backup."
        )
    op.drop_table(temporary)


def upgrade() -> None:
    _drop_empty_interrupted_batch_table()
    # SQLite cannot drop/rebuild change_plan_items while remote_operations still
    # references it. Rebuild the dependent table without that FK first, then
    # restore the FK after the parent table has its v3 shape.
    with op.batch_alter_table(
        "remote_operations", naming_convention=SQLITE_NAMING_CONVENTION
    ) as batch:
        batch.drop_constraint(
            "fk_remote_operations_plan_item_id_change_plan_items",
            type_="foreignkey",
        )
        batch.alter_column("before_snapshot_json", existing_type=sa.Text(), nullable=True)
        batch.alter_column("intended_snapshot_json", existing_type=sa.Text(), nullable=True)
        batch.add_column(sa.Column("request_method", sa.String(8), nullable=True))
        batch.add_column(sa.Column("remote_existed_before", sa.Boolean(), nullable=True))
    with op.batch_alter_table("change_plan_items") as batch:
        batch.drop_constraint("uq_plan_items_subject", type_="unique")
        batch.alter_column("work_id", existing_type=sa.Integer(), nullable=True)
        batch.alter_column("subject_id", existing_type=sa.Integer(), nullable=True)
        batch.alter_column("bgm_url", existing_type=sa.String(), nullable=True)
        batch.alter_column("before_snapshot_json", existing_type=sa.Text(), nullable=True)
        batch.add_column(sa.Column("source_entry_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("source_precondition_hash", sa.String(64), nullable=True))
        batch.add_column(sa.Column("remote_existence", sa.String(16), nullable=True))
        batch.create_foreign_key(
            "fk_plan_items_source_entry",
            "library_entries",
            ["source_entry_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_unique_constraint(
            "uq_plan_items_subject", ["plan_id", "subject_id"]
        )
        batch.create_unique_constraint(
            "uq_plan_items_source_entry", ["plan_id", "source_entry_id"]
        )
        batch.create_check_constraint(
            "ck_plan_items_remote_existence",
            "remote_existence IS NULL OR remote_existence IN ('present', 'absent', 'unknown')",
        )
    op.create_index(
        "ix_change_plan_items_source_entry_id",
        "change_plan_items",
        ["source_entry_id"],
    )
    with op.batch_alter_table("remote_operations") as batch:
        batch.create_foreign_key(
            "fk_remote_operations_plan_item_id_change_plan_items",
            "change_plan_items",
            ["plan_item_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    connection = op.get_bind()
    unsafe = connection.execute(
        sa.text(
            "SELECT "
            "(SELECT COUNT(*) FROM change_plans WHERE format_version >= 3) + "
            "(SELECT COUNT(*) FROM change_plan_items WHERE source_entry_id IS NOT NULL "
            " OR source_precondition_hash IS NOT NULL OR remote_existence IS NOT NULL) + "
            "(SELECT COUNT(*) FROM remote_operations WHERE request_method IS NOT NULL "
            " OR remote_existed_before IS NOT NULL)"
        )
    ).scalar_one()
    if unsafe:
        raise RuntimeError(
            "Cannot downgrade while v3 plans or Steam remote audit data exists; "
            "restore a pre-Phase-5 backup."
        )
    with op.batch_alter_table(
        "remote_operations", naming_convention=SQLITE_NAMING_CONVENTION
    ) as batch:
        batch.drop_constraint(
            "fk_remote_operations_plan_item_id_change_plan_items",
            type_="foreignkey",
        )
        batch.drop_column("remote_existed_before")
        batch.drop_column("request_method")
        batch.alter_column("intended_snapshot_json", existing_type=sa.Text(), nullable=False)
        batch.alter_column("before_snapshot_json", existing_type=sa.Text(), nullable=False)
    op.drop_index("ix_change_plan_items_source_entry_id", table_name="change_plan_items")
    with op.batch_alter_table("change_plan_items") as batch:
        batch.drop_constraint("ck_plan_items_remote_existence", type_="check")
        batch.drop_constraint("uq_plan_items_source_entry", type_="unique")
        batch.drop_constraint("uq_plan_items_subject", type_="unique")
        batch.drop_constraint("fk_plan_items_source_entry", type_="foreignkey")
        batch.drop_column("remote_existence")
        batch.drop_column("source_precondition_hash")
        batch.drop_column("source_entry_id")
        batch.alter_column("before_snapshot_json", existing_type=sa.Text(), nullable=False)
        batch.alter_column("bgm_url", existing_type=sa.String(), nullable=False)
        batch.alter_column("subject_id", existing_type=sa.Integer(), nullable=False)
        batch.alter_column("work_id", existing_type=sa.Integer(), nullable=False)
        batch.create_unique_constraint(
            "uq_plan_items_subject", ["plan_id", "subject_id"]
        )
    with op.batch_alter_table("remote_operations") as batch:
        batch.create_foreign_key(
            "fk_remote_operations_plan_item_id_change_plan_items",
            "change_plan_items",
            ["plan_item_id"],
            ["id"],
            ondelete="RESTRICT",
        )
