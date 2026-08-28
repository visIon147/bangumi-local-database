"""Add Steam library import and manual matching tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_steam_library"
down_revision: str | None = "0005_generic_plan_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_accounts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("external_account_id", sa.String(), nullable=False),
        sa.Column("account_name", sa.String(), nullable=True),
        sa.Column("config_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("first_seen_at", sa.String(32), nullable=False),
        sa.Column("last_seen_at", sa.String(32), nullable=False),
        sa.UniqueConstraint("source", "external_account_id", name="uq_source_accounts_identity"),
    )
    op.create_table(
        "library_import_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "source_account_id",
            sa.Integer(),
            sa.ForeignKey("source_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("counts_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.String(32), nullable=False),
        sa.Column("finished_at", sa.String(32), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'applied', 'failed')", name="ck_library_import_runs_status"
        ),
    )
    op.create_index(
        "ix_library_import_runs_source_account_id",
        "library_import_runs",
        ["source_account_id"],
    )
    op.create_table(
        "library_entries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "source_account_id",
            sa.Integer(),
            sa.ForeignKey("source_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("work_id", sa.Integer(), sa.ForeignKey("works.id", ondelete="SET NULL")),
        sa.Column("title_observed", sa.String(), nullable=True),
        sa.Column("localized_titles_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("ownership_scope", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("installed", sa.Boolean(), nullable=True),
        sa.Column("playtime_minutes", sa.Integer(), nullable=True),
        sa.Column("last_played_at", sa.String(32), nullable=True),
        sa.Column("metadata_source", sa.String(32), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("match_status", sa.String(16), nullable=False, server_default="unmatched"),
        sa.Column("match_reason", sa.Text(), nullable=True),
        sa.Column("match_updated_at", sa.String(32), nullable=True),
        sa.Column("first_seen_at", sa.String(32), nullable=False),
        sa.Column("last_seen_at", sa.String(32), nullable=False),
        sa.UniqueConstraint(
            "source_account_id", "external_id", name="uq_library_entries_external"
        ),
        sa.CheckConstraint(
            "match_status IN ('unmatched', 'candidates', 'confirmed', 'no_subject', 'deferred')",
            name="ck_library_entries_match_status",
        ),
        sa.CheckConstraint(
            "ownership_scope IN ('owned', 'visible', 'categorized', 'installed', 'unknown')",
            name="ck_library_entries_ownership_scope",
        ),
    )
    op.create_index("ix_library_entries_source_account_id", "library_entries", ["source_account_id"])
    op.create_index("ix_library_entries_work_id", "library_entries", ["work_id"])
    op.create_table(
        "library_collections",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "source_account_id",
            sa.Integer(),
            sa.ForeignKey("source_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="manual"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("first_seen_at", sa.String(32), nullable=False),
        sa.Column("last_seen_at", sa.String(32), nullable=False),
        sa.UniqueConstraint(
            "source_account_id", "external_id", name="uq_library_collections_external"
        ),
    )
    op.create_index(
        "ix_library_collections_source_account_id",
        "library_collections",
        ["source_account_id"],
    )
    op.create_table(
        "library_entry_collections",
        sa.Column(
            "library_entry_id",
            sa.Integer(),
            sa.ForeignKey("library_entries.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "collection_id",
            sa.Integer(),
            sa.ForeignKey("library_collections.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("first_seen_at", sa.String(32), nullable=False),
        sa.Column("last_seen_at", sa.String(32), nullable=False),
    )
    op.create_table(
        "library_match_candidates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "library_entry_id",
            sa.Integer(),
            sa.ForeignKey("library_entries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("query", sa.String(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("reasons_json", sa.Text(), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.String(32), nullable=False),
        sa.UniqueConstraint(
            "library_entry_id", "subject_id", name="uq_library_match_candidates_subject"
        ),
    )
    op.create_index(
        "ix_library_match_candidates_library_entry_id",
        "library_match_candidates",
        ["library_entry_id"],
    )
    op.create_table(
        "library_match_reviews",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "library_entry_id",
            sa.Integer(),
            sa.ForeignKey("library_entries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("subject_id", sa.Integer(), nullable=True),
        sa.Column("previous_status", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.CheckConstraint(
            "decision IN ('confirmed', 'no_subject', 'deferred', 'reopened')",
            name="ck_library_match_reviews_decision",
        ),
    )
    op.create_index(
        "ix_library_match_reviews_library_entry_id",
        "library_match_reviews",
        ["library_entry_id"],
    )
    op.create_index(
        "uq_work_links_source_external_id",
        "work_links",
        ["source", "external_id"],
        unique=True,
        sqlite_where=sa.text("external_id IS NOT NULL"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    populated = connection.execute(
        sa.text(
            "SELECT "
            "(SELECT COUNT(*) FROM source_accounts) + "
            "(SELECT COUNT(*) FROM library_import_runs) + "
            "(SELECT COUNT(*) FROM library_entries) + "
            "(SELECT COUNT(*) FROM library_collections) + "
            "(SELECT COUNT(*) FROM library_match_candidates) + "
            "(SELECT COUNT(*) FROM library_match_reviews)"
        )
    ).scalar_one()
    if populated:
        raise RuntimeError(
            "Cannot downgrade while Phase 5 Steam data exists; restore a pre-upgrade backup."
        )
    op.drop_index("uq_work_links_source_external_id", table_name="work_links")
    op.drop_index("ix_library_match_reviews_library_entry_id", table_name="library_match_reviews")
    op.drop_table("library_match_reviews")
    op.drop_index(
        "ix_library_match_candidates_library_entry_id", table_name="library_match_candidates"
    )
    op.drop_table("library_match_candidates")
    op.drop_table("library_entry_collections")
    op.drop_index("ix_library_collections_source_account_id", table_name="library_collections")
    op.drop_table("library_collections")
    op.drop_index("ix_library_entries_work_id", table_name="library_entries")
    op.drop_index("ix_library_entries_source_account_id", table_name="library_entries")
    op.drop_table("library_entries")
    op.drop_index("ix_library_import_runs_source_account_id", table_name="library_import_runs")
    op.drop_table("library_import_runs")
    op.drop_table("source_accounts")
