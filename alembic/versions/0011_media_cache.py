"""Add portable image metadata and content-addressed media cache records."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_media_cache"
down_revision: str | None = "0010_discovery_queue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_blobs",
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_relpath", sa.String(), nullable=False),
        sa.Column("mime_type", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.Column("last_verified_at", sa.String(32), nullable=True),
        sa.Column("last_accessed_at", sa.String(32), nullable=True),
        sa.PrimaryKeyConstraint("sha256"),
        sa.UniqueConstraint("storage_relpath"),
    )
    op.create_index("ix_media_blobs_last_accessed_at", "media_blobs", ["last_accessed_at"])
    op.create_table(
        "media_sources",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("variant", sa.String(32), nullable=False),
        sa.Column("locale", sa.String(16), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("remote_url", sa.String(), nullable=True),
        sa.Column("logical_locator_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("current_blob_sha256", sa.String(64), nullable=True),
        sa.Column("etag", sa.String(), nullable=True),
        sa.Column("last_modified", sa.String(), nullable=True),
        sa.Column("observed_at", sa.String(32), nullable=False),
        sa.Column("last_checked_at", sa.String(32), nullable=True),
        sa.Column("fetched_at", sa.String(32), nullable=True),
        sa.Column("failure_code", sa.String(32), nullable=True),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("retry_after", sa.String(32), nullable=True),
        sa.CheckConstraint("provider IN ('bangumi','steam')", name="ck_media_sources_provider"),
        sa.CheckConstraint("origin IN ('remote','steam_local')", name="ck_media_sources_origin"),
        sa.CheckConstraint(
            "status IN ('observed','cached','stale','missing','failed')",
            name="ck_media_sources_status",
        ),
        sa.ForeignKeyConstraint(
            ["current_blob_sha256"], ["media_blobs.sha256"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "external_id", "variant", "locale", "origin",
            name="uq_media_sources_identity",
        ),
    )
    for column in ("provider", "external_id", "current_blob_sha256"):
        op.create_index(f"ix_media_sources_{column}", "media_sources", [column])
    op.create_table(
        "media_renditions",
        sa.Column("media_source_id", sa.String(36), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("blob_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(
            ["media_source_id"], ["media_sources.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["blob_sha256"], ["media_blobs.sha256"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("media_source_id", "purpose"),
    )
    op.create_index("ix_media_renditions_blob_sha256", "media_renditions", ["blob_sha256"])
    op.create_table(
        "media_bindings",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("media_source_id", sa.String(36), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=True),
        sa.Column("library_entry_id", sa.Integer(), nullable=True),
        sa.Column("rating_queue_item_id", sa.String(36), nullable=True),
        sa.Column("discovery_candidate_id", sa.String(36), nullable=True),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("pinned_blob_sha256", sa.String(64), nullable=True),
        sa.Column("first_observed_at", sa.String(32), nullable=False),
        sa.Column("last_observed_at", sa.String(32), nullable=False),
        sa.CheckConstraint(
            "((work_id IS NOT NULL) + (library_entry_id IS NOT NULL) + "
            "(rating_queue_item_id IS NOT NULL) + (discovery_candidate_id IS NOT NULL)) = 1",
            name="ck_media_bindings_one_target",
        ),
        sa.ForeignKeyConstraint(["media_source_id"], ["media_sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["library_entry_id"], ["library_entries.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["rating_queue_item_id"], ["rating_queue_items.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["discovery_candidate_id"], ["discovery_candidates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["pinned_blob_sha256"], ["media_blobs.sha256"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "media_source_id", "work_id", "library_entry_id", "rating_queue_item_id",
            "discovery_candidate_id", "role", name="uq_media_bindings_target_role",
        ),
    )
    for column in (
        "media_source_id", "work_id", "library_entry_id", "rating_queue_item_id",
        "discovery_candidate_id", "pinned_blob_sha256",
    ):
        op.create_index(f"ix_media_bindings_{column}", "media_bindings", [column])
    for name, target in (
        ("uq_media_bindings_work_role", "work_id"),
        ("uq_media_bindings_library_role", "library_entry_id"),
        ("uq_media_bindings_rating_role", "rating_queue_item_id"),
        ("uq_media_bindings_discovery_role", "discovery_candidate_id"),
    ):
        op.create_index(
            name,
            "media_bindings",
            ["media_source_id", target, "role"],
            unique=True,
            sqlite_where=sa.text(f"{target} IS NOT NULL"),
        )


def downgrade() -> None:
    connection = op.get_bind()
    used = connection.execute(
        sa.text(
            "SELECT (SELECT COUNT(*) FROM media_sources) + "
            "(SELECT COUNT(*) FROM media_blobs) + (SELECT COUNT(*) FROM media_bindings)"
        )
    ).scalar_one()
    if used:
        raise RuntimeError("Cannot downgrade while media cache data exists; restore a pre-0011 backup.")
    op.drop_table("media_bindings")
    op.drop_table("media_renditions")
    op.drop_table("media_sources")
    op.drop_table("media_blobs")
