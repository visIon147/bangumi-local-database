-- Human-readable core schema reference through Phase 7.
-- Alembic migrations are authoritative; do not use this file to migrate a real DB.
PRAGMA foreign_keys = ON;

CREATE TABLE works (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    title_cn TEXT,
    title_original TEXT,
    summary TEXT,
    release_date TEXT,
    cover_url TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    bgm_subject_id INTEGER UNIQUE, -- temporary read-only migration bridge
    bgm_url TEXT                    -- temporary read-only migration bridge
);

CREATE TABLE bangumi_subjects (
    subject_id INTEGER PRIMARY KEY,
    work_id INTEGER NOT NULL UNIQUE REFERENCES works(id) ON DELETE CASCADE,
    subject_type INTEGER NOT NULL CHECK (subject_type IN (1,2,3,4,6)),
    url TEXT NOT NULL,
    metadata_available INTEGER NOT NULL DEFAULT 1,
    last_observed_at TEXT NOT NULL
);

CREATE TABLE bangumi_collection_states (
    subject_id INTEGER PRIMARY KEY REFERENCES bangumi_subjects(subject_id) ON DELETE CASCADE,
    bgm_collection_type INTEGER,
    rating INTEGER CHECK (rating IS NULL OR rating BETWEEN 1 AND 10),
    comment TEXT,
    is_private INTEGER NOT NULL DEFAULT 0,
    local_updated_at TEXT NOT NULL
);

CREATE TABLE game_profiles (
    work_id INTEGER PRIMARY KEY REFERENCES works(id) ON DELETE CASCADE,
    confidence TEXT NOT NULL DEFAULT 'unknown',
    completion TEXT NOT NULL DEFAULT 'unknown',
    playtime_minutes INTEGER,
    first_played_at TEXT,
    last_played_at TEXT,
    liked_aspects_json TEXT,
    disliked_aspects_json TEXT,
    notes_private TEXT
);

CREATE TABLE tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    sync_scope TEXT NOT NULL CHECK (sync_scope IN ('bangumi','local','both')),
    namespace TEXT
);

CREATE TABLE work_tags (
    work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    origin TEXT NOT NULL DEFAULT 'manual',
    confidence TEXT,
    PRIMARY KEY (work_id, tag_id)
);

CREATE TABLE work_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    url TEXT NOT NULL,
    external_id TEXT,
    is_primary INTEGER NOT NULL DEFAULT 0,
    match_source TEXT,
    match_confidence TEXT,
    verified_at TEXT,
    UNIQUE(work_id, source, url)
);

CREATE TABLE sync_shadows (
    subject_id INTEGER PRIMARY KEY REFERENCES bangumi_subjects(subject_id) ON DELETE CASCADE,
    remote_snapshot_json TEXT NOT NULL,
    remote_hash TEXT NOT NULL,
    remote_updated_at TEXT,
    synced_at TEXT NOT NULL
);

CREATE TABLE sync_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id INTEGER NOT NULL REFERENCES bangumi_subjects(subject_id) ON DELETE CASCADE,
    field TEXT NOT NULL,
    base_json TEXT NOT NULL,
    local_json TEXT NOT NULL,
    remote_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open','resolved','ignored')),
    resolution TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

-- Full plan/audit definitions are versioned in 0003 and 0005:
-- change_plans, change_plan_items, plan_apply_runs, remote_operations.
-- Steam source/matching tables are versioned in 0006 through 0008.
-- Rating workflow/session/event tables are versioned in 0009.
-- Bounded discovery session/candidate/review tables are versioned in 0010.
-- No table contains access tokens or credentials.
