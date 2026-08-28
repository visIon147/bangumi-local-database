"""Generalize game-centric storage into works and Bangumi subjects."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_generic_works"
down_revision: str | None = "0003_phase3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SUBJECT_TYPES = "subject_type IN (1, 2, 3, 4, 6)"
WORK_KINDS = "kind IN ('book', 'anime', 'music', 'game', 'real', 'unknown')"


def upgrade() -> None:
    connection = op.get_bind()

    op.rename_table("games", "works")
    op.add_column(
        "works", sa.Column("kind", sa.String(16), nullable=False, server_default="game")
    )
    op.drop_index("ix_games_bgm_subject_id", table_name="works")
    op.create_index("ix_works_bgm_subject_id", "works", ["bgm_subject_id"])

    op.create_table(
        "bangumi_subjects",
        sa.Column("subject_id", sa.Integer(), primary_key=True),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("subject_type", sa.Integer(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("metadata_available", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_observed_at", sa.String(32), nullable=False),
        sa.CheckConstraint(SUBJECT_TYPES, name="ck_bangumi_subjects_type"),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("work_id", name="uq_bangumi_subjects_work_id"),
    )
    op.create_index("ix_bangumi_subjects_work_id", "bangumi_subjects", ["work_id"])
    connection.execute(
        sa.text(
            """
            INSERT INTO bangumi_subjects
                (subject_id, work_id, subject_type, url, metadata_available, last_observed_at)
            SELECT bgm_subject_id, id, 4,
                   COALESCE(bgm_url, 'https://bgm.tv/subject/' || bgm_subject_id),
                   1, updated_at
            FROM works
            WHERE bgm_subject_id IS NOT NULL
            """
        )
    )

    op.create_table(
        "bangumi_collection_states",
        sa.Column("subject_id", sa.Integer(), primary_key=True),
        sa.Column("bgm_collection_type", sa.Integer(), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("is_private", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("local_updated_at", sa.String(32), nullable=False),
        sa.CheckConstraint(
            "rating IS NULL OR (rating BETWEEN 1 AND 10)", name="ck_bangumi_collection_rating"
        ),
        sa.ForeignKeyConstraint(
            ["subject_id"], ["bangumi_subjects.subject_id"], ondelete="CASCADE"
        ),
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO bangumi_collection_states
                (subject_id, bgm_collection_type, rating, comment, is_private, local_updated_at)
            SELECT w.bgm_subject_id, c.bgm_collection_type, c.rating, c.comment,
                   c.is_private, c.local_updated_at
            FROM collection_states c
            JOIN works w ON w.id = c.game_id
            WHERE w.bgm_subject_id IS NOT NULL
            """
        )
    )

    op.create_table(
        "game_profiles",
        sa.Column("work_id", sa.Integer(), primary_key=True),
        sa.Column("confidence", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("completion", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("playtime_minutes", sa.Integer(), nullable=True),
        sa.Column("first_played_at", sa.String(32), nullable=True),
        sa.Column("last_played_at", sa.String(32), nullable=True),
        sa.Column("liked_aspects_json", sa.Text(), nullable=True),
        sa.Column("disliked_aspects_json", sa.Text(), nullable=True),
        sa.Column("notes_private", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["work_id"], ["works.id"], ondelete="CASCADE"),
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO game_profiles
                (work_id, confidence, completion, playtime_minutes, first_played_at,
                 last_played_at, liked_aspects_json, disliked_aspects_json, notes_private)
            SELECT game_id, confidence, completion, playtime_minutes, first_played_at,
                   last_played_at, liked_aspects_json, disliked_aspects_json, notes_private
            FROM collection_states
            """
        )
    )

    op.rename_table("game_tags", "work_tags")
    connection.exec_driver_sql("ALTER TABLE work_tags RENAME COLUMN game_id TO work_id")
    op.drop_index("ix_game_tags_tag_id", table_name="work_tags")
    op.create_index("ix_work_tags_tag_id", "work_tags", ["tag_id"])

    op.rename_table("external_links", "work_links")
    connection.exec_driver_sql("ALTER TABLE work_links RENAME COLUMN game_id TO work_id")
    connection.exec_driver_sql("ALTER TABLE work_links RENAME COLUMN kind TO source")
    op.add_column("work_links", sa.Column("match_source", sa.String(32), nullable=True))
    op.add_column("work_links", sa.Column("match_confidence", sa.String(16), nullable=True))
    op.add_column("work_links", sa.Column("verified_at", sa.String(32), nullable=True))

    op.create_table(
        "sync_shadows_v4",
        sa.Column("subject_id", sa.Integer(), primary_key=True),
        sa.Column("remote_snapshot_json", sa.Text(), nullable=False),
        sa.Column("remote_hash", sa.String(64), nullable=False),
        sa.Column("remote_updated_at", sa.String(32), nullable=True),
        sa.Column("synced_at", sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(
            ["subject_id"], ["bangumi_subjects.subject_id"], ondelete="CASCADE"
        ),
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO sync_shadows_v4
                (subject_id, remote_snapshot_json, remote_hash, remote_updated_at, synced_at)
            SELECT w.bgm_subject_id, s.remote_snapshot_json, s.remote_hash,
                   s.remote_updated_at, s.synced_at
            FROM sync_shadows s JOIN works w ON w.id = s.game_id
            """
        )
    )

    op.create_table(
        "sync_conflicts_v4",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("subject_id", sa.Integer(), nullable=False),
        sa.Column("field", sa.String(16), nullable=False),
        sa.Column("base_json", sa.Text(), nullable=False),
        sa.Column("local_json", sa.Text(), nullable=False),
        sa.Column("remote_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.Column("resolved_at", sa.String(32), nullable=True),
        sa.CheckConstraint(
            "status IN ('open', 'resolved', 'ignored')", name="ck_conflicts_status"
        ),
        sa.ForeignKeyConstraint(
            ["subject_id"], ["bangumi_subjects.subject_id"], ondelete="CASCADE"
        ),
    )
    # The Phase 2 indexes with the final names still exist on the old table.
    # Temporary names avoid SQLite's database-wide index-name collision.
    op.create_index("ix_sync_conflicts_v4_subject_id", "sync_conflicts_v4", ["subject_id"])
    op.create_index("ix_sync_conflicts_v4_status", "sync_conflicts_v4", ["status"])
    connection.execute(
        sa.text(
            """
            INSERT INTO sync_conflicts_v4
                (id, subject_id, field, base_json, local_json, remote_json,
                 status, resolution, created_at, resolved_at)
            SELECT c.id, w.bgm_subject_id, c.field, c.base_json, c.local_json, c.remote_json,
                   c.status, c.resolution, c.created_at, c.resolved_at
            FROM sync_conflicts c JOIN works w ON w.id = c.game_id
            """
        )
    )

    expected = connection.execute(sa.text("SELECT COUNT(*) FROM works")).scalar_one()
    for table in (
        "bangumi_subjects",
        "bangumi_collection_states",
        "game_profiles",
        "sync_shadows_v4",
    ):
        actual = connection.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        if actual != expected:
            raise RuntimeError(f"Phase 4 migration count mismatch for {table}: {actual} != {expected}")

    op.drop_table("sync_conflicts")
    op.rename_table("sync_conflicts_v4", "sync_conflicts")
    op.drop_index("ix_sync_conflicts_v4_subject_id", table_name="sync_conflicts")
    op.drop_index("ix_sync_conflicts_v4_status", table_name="sync_conflicts")
    op.create_index("ix_sync_conflicts_subject_id", "sync_conflicts", ["subject_id"])
    op.create_index("ix_sync_conflicts_status", "sync_conflicts", ["status"])
    op.drop_table("sync_shadows")
    op.rename_table("sync_shadows_v4", "sync_shadows")
    op.drop_table("collection_states")

    connection.exec_driver_sql(
        "ALTER TABLE change_plan_items RENAME COLUMN game_id TO work_id"
    )
    op.drop_index("ix_change_plan_items_game_id", table_name="change_plan_items")
    op.create_index("ix_change_plan_items_work_id", "change_plan_items", ["work_id"])
    connection.exec_driver_sql(
        "ALTER TABLE remote_operations RENAME COLUMN game_id TO work_id"
    )

    # bgm_subject_id and bgm_url remain as read-only compatibility bridge columns.
    # New code resolves Bangumi identities exclusively through bangumi_subjects.

    violations = connection.execute(sa.text("PRAGMA foreign_key_check")).fetchall()
    if violations:
        raise RuntimeError(f"Phase 4 migration produced foreign-key violations: {violations!r}")


def downgrade() -> None:
    connection = op.get_bind()
    unsafe = connection.execute(
        sa.text(
            """
            SELECT COUNT(*) FROM works w
            LEFT JOIN bangumi_subjects b ON b.work_id = w.id
            WHERE w.kind != 'game' OR b.subject_id IS NULL OR b.subject_type != 4
            """
        )
    ).scalar_one()
    if unsafe:
        raise RuntimeError(
            "Cannot downgrade Phase 4 after non-game or non-Bangumi works have been imported."
        )
    counts = {
        name: connection.execute(sa.text(f"SELECT COUNT(*) FROM {name}")).scalar_one()
        for name in (
            "works",
            "bangumi_subjects",
            "bangumi_collection_states",
            "game_profiles",
        )
    }
    if len(set(counts.values())) != 1:
        raise RuntimeError(
            f"Cannot downgrade an incomplete Phase 4 identity graph: {counts!r}"
        )
    identity_mismatches = connection.execute(
        sa.text(
            """
            SELECT COUNT(*)
            FROM works w JOIN bangumi_subjects b ON b.work_id = w.id
            WHERE w.bgm_subject_id IS NULL
               OR w.bgm_subject_id != b.subject_id
               OR COALESCE(w.bgm_url, '') != b.url
            """
        )
    ).scalar_one()
    enriched_links = connection.execute(
        sa.text(
            """
            SELECT COUNT(*) FROM work_links
            WHERE match_source IS NOT NULL
               OR match_confidence IS NOT NULL
               OR verified_at IS NOT NULL
            """
        )
    ).scalar_one()
    if identity_mismatches or enriched_links:
        raise RuntimeError(
            "Cannot downgrade after Phase 4 identity/link data was added; "
            "restore the mandatory pre-upgrade backup."
        )

    connection.exec_driver_sql(
        "ALTER TABLE change_plan_items RENAME COLUMN work_id TO game_id"
    )
    op.drop_index("ix_change_plan_items_work_id", table_name="change_plan_items")
    op.create_index("ix_change_plan_items_game_id", "change_plan_items", ["game_id"])
    connection.exec_driver_sql(
        "ALTER TABLE remote_operations RENAME COLUMN work_id TO game_id"
    )

    op.create_table(
        "collection_states_v3",
        sa.Column("game_id", sa.Integer(), primary_key=True),
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
        sa.CheckConstraint(
            "rating IS NULL OR (rating BETWEEN 1 AND 10)", name="ck_collection_rating"
        ),
        sa.ForeignKeyConstraint(["game_id"], ["works.id"], ondelete="CASCADE"),
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO collection_states_v3
                (game_id, bgm_collection_type, rating, comment, is_private,
                 confidence, completion, playtime_minutes, first_played_at,
                 last_played_at, liked_aspects_json, disliked_aspects_json,
                 notes_private, local_updated_at)
            SELECT b.work_id, c.bgm_collection_type, c.rating, c.comment, c.is_private,
                   g.confidence, g.completion, g.playtime_minutes, g.first_played_at,
                   g.last_played_at, g.liked_aspects_json, g.disliked_aspects_json,
                   g.notes_private, c.local_updated_at
            FROM bangumi_collection_states c
            JOIN bangumi_subjects b ON b.subject_id = c.subject_id
            JOIN game_profiles g ON g.work_id = b.work_id
            """
        )
    )

    op.create_table(
        "sync_shadows_v3",
        sa.Column("game_id", sa.Integer(), primary_key=True),
        sa.Column("remote_snapshot_json", sa.Text(), nullable=False),
        sa.Column("remote_hash", sa.String(64), nullable=False),
        sa.Column("remote_updated_at", sa.String(32), nullable=True),
        sa.Column("synced_at", sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["works.id"], ondelete="CASCADE"),
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO sync_shadows_v3
                (game_id, remote_snapshot_json, remote_hash, remote_updated_at, synced_at)
            SELECT b.work_id, s.remote_snapshot_json, s.remote_hash,
                   s.remote_updated_at, s.synced_at
            FROM sync_shadows s JOIN bangumi_subjects b ON b.subject_id = s.subject_id
            """
        )
    )

    op.create_table(
        "sync_conflicts_v3",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("game_id", sa.Integer(), nullable=False),
        sa.Column("field", sa.String(16), nullable=False),
        sa.Column("base_json", sa.Text(), nullable=False),
        sa.Column("local_json", sa.Text(), nullable=False),
        sa.Column("remote_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(32), nullable=False),
        sa.Column("resolved_at", sa.String(32), nullable=True),
        sa.CheckConstraint(
            "status IN ('open', 'resolved', 'ignored')", name="ck_conflicts_status"
        ),
        sa.ForeignKeyConstraint(["game_id"], ["works.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_sync_conflicts_v3_game_id", "sync_conflicts_v3", ["game_id"])
    op.create_index("ix_sync_conflicts_v3_status", "sync_conflicts_v3", ["status"])
    connection.execute(
        sa.text(
            """
            INSERT INTO sync_conflicts_v3
                (id, game_id, field, base_json, local_json, remote_json,
                 status, resolution, created_at, resolved_at)
            SELECT c.id, b.work_id, c.field, c.base_json, c.local_json, c.remote_json,
                   c.status, c.resolution, c.created_at, c.resolved_at
            FROM sync_conflicts c JOIN bangumi_subjects b ON b.subject_id = c.subject_id
            """
        )
    )

    op.drop_table("sync_conflicts")
    op.rename_table("sync_conflicts_v3", "sync_conflicts")
    op.drop_index("ix_sync_conflicts_v3_game_id", table_name="sync_conflicts")
    op.drop_index("ix_sync_conflicts_v3_status", table_name="sync_conflicts")
    op.create_index("ix_sync_conflicts_game_id", "sync_conflicts", ["game_id"])
    op.create_index("ix_sync_conflicts_status", "sync_conflicts", ["status"])
    op.drop_table("sync_shadows")
    op.rename_table("sync_shadows_v3", "sync_shadows")
    op.drop_table("bangumi_collection_states")
    op.drop_table("game_profiles")
    op.drop_table("bangumi_subjects")
    op.rename_table("collection_states_v3", "collection_states")

    op.rename_table("work_tags", "game_tags")
    connection.exec_driver_sql("ALTER TABLE game_tags RENAME COLUMN work_id TO game_id")
    op.drop_index("ix_work_tags_tag_id", table_name="game_tags")
    op.create_index("ix_game_tags_tag_id", "game_tags", ["tag_id"])

    connection.exec_driver_sql("ALTER TABLE work_links DROP COLUMN verified_at")
    connection.exec_driver_sql("ALTER TABLE work_links DROP COLUMN match_confidence")
    connection.exec_driver_sql("ALTER TABLE work_links DROP COLUMN match_source")
    connection.exec_driver_sql("ALTER TABLE work_links RENAME COLUMN source TO kind")
    connection.exec_driver_sql("ALTER TABLE work_links RENAME COLUMN work_id TO game_id")
    op.rename_table("work_links", "external_links")

    op.drop_index("ix_works_bgm_subject_id", table_name="works")
    connection.exec_driver_sql("ALTER TABLE works DROP COLUMN kind")
    op.rename_table("works", "games")
    op.create_index("ix_games_bgm_subject_id", "games", ["bgm_subject_id"])

    expected = counts["works"]
    for table in ("games", "collection_states", "sync_shadows"):
        actual = connection.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        if actual != expected:
            raise RuntimeError(
                f"Phase 4 downgrade count mismatch for {table}: {actual} != {expected}"
            )
    violations = connection.execute(sa.text("PRAGMA foreign_key_check")).fetchall()
    if violations:
        raise RuntimeError(
            f"Phase 4 downgrade produced foreign-key violations: {violations!r}"
        )
