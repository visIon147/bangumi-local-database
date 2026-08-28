"""Create the Phase 1 read-only mirror schema."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_phase1"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "games",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("bgm_subject_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("title_cn", sa.String(), nullable=True),
        sa.Column("title_original", sa.String(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("release_date", sa.String(10), nullable=True),
        sa.Column("bgm_url", sa.String(), nullable=True),
        sa.Column("cover_url", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.Column("updated_at", sa.String(32), nullable=False),
        sa.UniqueConstraint("bgm_subject_id", name="uq_games_bgm_subject_id"),
    )
    op.create_index("ix_games_bgm_subject_id", "games", ["bgm_subject_id"])

    op.create_table(
        "collection_states",
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("bgm_collection_type", sa.Integer(), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("is_private", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("confidence", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("completion", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("playtime_minutes", sa.Integer(), nullable=True),
        sa.Column("first_played_at", sa.String(32), nullable=True),
        sa.Column("last_played_at", sa.String(32), nullable=True),
        sa.Column("liked_aspects_json", sa.Text(), nullable=True),
        sa.Column("disliked_aspects_json", sa.Text(), nullable=True),
        sa.Column("notes_private", sa.Text(), nullable=True),
        sa.Column("local_updated_at", sa.String(32), nullable=False),
        sa.CheckConstraint("rating IS NULL OR (rating BETWEEN 1 AND 10)", name="ck_collection_rating"),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("game_id"),
    )

    op.create_table(
        "tags",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("sync_scope", sa.String(16), nullable=False),
        sa.Column("namespace", sa.String(), nullable=True),
        sa.CheckConstraint("sync_scope IN ('bangumi', 'local', 'both')", name="ck_tags_sync_scope"),
        sa.UniqueConstraint("name", name="uq_tags_name"),
    )

    op.create_table(
        "game_tags",
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("tag_id", sa.Integer(), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False, server_default="bangumi"),
        sa.Column("confidence", sa.String(16), nullable=True),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tag_id"], ["tags.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("game_id", "tag_id"),
    )
    op.create_index("ix_game_tags_tag_id", "game_tags", ["tag_id"])

    op.create_table(
        "external_links",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("game_id", "kind", "url", name="uq_external_link"),
    )

    op.create_table(
        "sync_shadows",
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("remote_snapshot_json", sa.Text(), nullable=False),
        sa.Column("remote_hash", sa.String(64), nullable=False),
        sa.Column("remote_updated_at", sa.String(32), nullable=True),
        sa.Column("synced_at", sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("game_id"),
    )


def downgrade() -> None:
    op.drop_table("sync_shadows")
    op.drop_table("external_links")
    op.drop_index("ix_game_tags_tag_id", table_name="game_tags")
    op.drop_table("game_tags")
    op.drop_table("tags")
    op.drop_table("collection_states")
    op.drop_index("ix_games_bgm_subject_id", table_name="games")
    op.drop_table("games")

