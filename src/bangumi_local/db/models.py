from __future__ import annotations

from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Work(Base):
    __tablename__ = "works"
    __table_args__ = (UniqueConstraint("bgm_subject_id", name="uq_games_bgm_subject_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    title: Mapped[str] = mapped_column(String, nullable=False)
    title_cn: Mapped[str | None] = mapped_column(String)
    title_original: Mapped[str | None] = mapped_column(String)
    summary: Mapped[str | None] = mapped_column(Text)
    release_date: Mapped[str | None] = mapped_column(String(10))
    cover_url: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)
    # Read-only compatibility bridge retained by 0004; new identity uses bangumi_subjects.
    bgm_subject_id: Mapped[int | None] = mapped_column(Integer, index=True)
    bgm_url: Mapped[str | None] = mapped_column(String)

    bangumi_subject: Mapped[Optional[BangumiSubject]] = relationship(
        back_populates="work", cascade="all, delete-orphan", uselist=False
    )
    game_profile: Mapped[Optional[GameProfile]] = relationship(
        back_populates="work", cascade="all, delete-orphan", uselist=False
    )


class BangumiSubject(Base):
    __tablename__ = "bangumi_subjects"
    __table_args__ = (
        CheckConstraint("subject_type IN (1, 2, 3, 4, 6)", name="ck_bangumi_subjects_type"),
        UniqueConstraint("work_id", name="uq_bangumi_subjects_work_id"),
    )

    subject_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    work_id: Mapped[int] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject_type: Mapped[int] = mapped_column(Integer, nullable=False)
    url: Mapped[str] = mapped_column(String, nullable=False)
    metadata_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_observed_at: Mapped[str] = mapped_column(String(32), nullable=False)

    work: Mapped[Work] = relationship(back_populates="bangumi_subject")
    collection_state: Mapped[Optional[BangumiCollectionState]] = relationship(
        back_populates="subject", cascade="all, delete-orphan", uselist=False
    )
    shadow: Mapped[Optional[SyncShadow]] = relationship(
        back_populates="subject", cascade="all, delete-orphan", uselist=False
    )
    conflicts: Mapped[list[SyncConflict]] = relationship(
        back_populates="subject", cascade="all, delete-orphan"
    )


class ChangePlan(Base):
    __tablename__ = "change_plans"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'reviewed', 'applying', 'applied', 'partial', 'failed', 'cancelled')",
            name="ck_change_plans_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    format_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    tag: Mapped[str | None] = mapped_column(String)
    old_tag: Mapped[str | None] = mapped_column(String)
    new_tag: Mapped[str | None] = mapped_column(String)
    selector_json: Mapped[str] = mapped_column(Text, nullable=False)
    summary_json: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", index=True)
    created_by: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    reverse_of_plan_id: Mapped[str | None] = mapped_column(
        ForeignKey("change_plans.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    reviewed_at: Mapped[str | None] = mapped_column(String(32))
    applied_at: Mapped[str | None] = mapped_column(String(32))


class ChangePlanItem(Base):
    __tablename__ = "change_plan_items"
    __table_args__ = (
        CheckConstraint(
            "disposition IN ('planned', 'unchanged')", name="ck_plan_items_disposition"
        ),
        CheckConstraint(
            "item_status IN ('not_applicable', 'pending', 'applied', 'stale', 'failed')",
            name="ck_plan_items_status",
        ),
        UniqueConstraint("plan_id", "subject_id", name="uq_plan_items_subject"),
        UniqueConstraint("plan_id", "source_entry_id", name="uq_plan_items_source_entry"),
        CheckConstraint(
            "remote_existence IS NULL OR remote_existence IN ('present', 'absent', 'unknown')",
            name="ck_plan_items_remote_existence",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("change_plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    work_id: Mapped[int | None] = mapped_column(
        ForeignKey("works.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    subject_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("library_entries.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    bgm_url: Mapped[str | None] = mapped_column(String, nullable=True)
    disposition: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    action_json: Mapped[str] = mapped_column(Text, nullable=False)
    selection_evidence_json: Mapped[str] = mapped_column(Text, nullable=False)
    before_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    intended_snapshot_json: Mapped[str | None] = mapped_column(Text)
    before_tags_json: Mapped[str] = mapped_column(Text, nullable=False)
    after_tags_json: Mapped[str | None] = mapped_column(Text)
    public_tags_json: Mapped[str] = mapped_column(Text, nullable=False)
    precondition_hash: Mapped[str | None] = mapped_column(String(64))
    changed_fields_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    local_precondition_hash: Mapped[str | None] = mapped_column(String(64))
    source_precondition_hash: Mapped[str | None] = mapped_column(String(64))
    remote_existence: Mapped[str | None] = mapped_column(String(16))
    item_status: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)


class PlanApplyRun(Base):
    __tablename__ = "plan_apply_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'applied', 'partial', 'failed', 'aborted')",
            name="ck_plan_apply_runs_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("change_plans.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    backup_path: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[str] = mapped_column(String(32), nullable=False)
    finished_at: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)


class RemoteOperation(Base):
    __tablename__ = "remote_operations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('started', 'applied', 'failed', 'uncertain')",
            name="ck_remote_operations_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("plan_apply_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("change_plans.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    plan_item_id: Mapped[int] = mapped_column(
        ForeignKey("change_plan_items.id", ondelete="RESTRICT"), nullable=False
    )
    work_id: Mapped[int] = mapped_column(
        ForeignKey("works.id", ondelete="RESTRICT"), nullable=False
    )
    subject_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    before_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    intended_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    actual_snapshot_json: Mapped[str | None] = mapped_column(Text)
    request_payload_json: Mapped[str | None] = mapped_column(Text)
    request_method: Mapped[str | None] = mapped_column(String(8))
    remote_existed_before: Mapped[bool | None] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[str] = mapped_column(String(32), nullable=False)
    finished_at: Mapped[str | None] = mapped_column(String(32))


class BangumiCollectionState(Base):
    __tablename__ = "bangumi_collection_states"
    __table_args__ = (
        CheckConstraint(
            "rating IS NULL OR (rating BETWEEN 1 AND 10)",
            name="ck_bangumi_collection_rating",
        ),
    )

    subject_id: Mapped[int] = mapped_column(
        ForeignKey("bangumi_subjects.subject_id", ondelete="CASCADE"), primary_key=True
    )
    bgm_collection_type: Mapped[int | None] = mapped_column(Integer)
    rating: Mapped[int | None] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(Text)
    is_private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    local_updated_at: Mapped[str] = mapped_column(String(32), nullable=False)
    subject: Mapped[BangumiSubject] = relationship(back_populates="collection_state")


class GameProfile(Base):
    __tablename__ = "game_profiles"

    work_id: Mapped[int] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), primary_key=True
    )
    confidence: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    completion: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    playtime_minutes: Mapped[int | None] = mapped_column(Integer)
    first_played_at: Mapped[str | None] = mapped_column(String(32))
    last_played_at: Mapped[str | None] = mapped_column(String(32))
    liked_aspects_json: Mapped[str | None] = mapped_column(Text)
    disliked_aspects_json: Mapped[str | None] = mapped_column(Text)
    notes_private: Mapped[str | None] = mapped_column(Text)
    work: Mapped[Work] = relationship(back_populates="game_profile")


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (
        CheckConstraint("sync_scope IN ('bangumi', 'local', 'both')", name="ck_tags_sync_scope"),
        UniqueConstraint("name", name="uq_tags_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    sync_scope: Mapped[str] = mapped_column(String(16), nullable=False)
    namespace: Mapped[str | None] = mapped_column(String)


class WorkTag(Base):
    __tablename__ = "work_tags"

    work_id: Mapped[int] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(
        ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="bangumi")
    confidence: Mapped[str | None] = mapped_column(String(16))


class WorkLink(Base):
    __tablename__ = "work_links"
    __table_args__ = (
        UniqueConstraint("work_id", "source", "url", name="uq_external_link"),
        Index(
            "uq_work_links_source_external_id",
            "source",
            "external_id",
            unique=True,
            sqlite_where=text("external_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    work_id: Mapped[int] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    url: Mapped[str] = mapped_column(String, nullable=False)
    external_id: Mapped[str | None] = mapped_column(String)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    match_source: Mapped[str | None] = mapped_column(String(32))
    match_confidence: Mapped[str | None] = mapped_column(String(16))
    verified_at: Mapped[str | None] = mapped_column(String(32))


class SourceAccount(Base):
    __tablename__ = "source_accounts"
    __table_args__ = (
        UniqueConstraint("source", "external_account_id", name="uq_source_accounts_identity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    external_account_id: Mapped[str] = mapped_column(String, nullable=False)
    account_name: Mapped[str | None] = mapped_column(String)
    config_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    first_seen_at: Mapped[str] = mapped_column(String(32), nullable=False)
    last_seen_at: Mapped[str] = mapped_column(String(32), nullable=False)


class LibraryImportRun(Base):
    __tablename__ = "library_import_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'applied', 'failed')", name="ck_library_import_runs_status"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_account_id: Mapped[int] = mapped_column(
        ForeignKey("source_accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    counts_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[str] = mapped_column(String(32), nullable=False)
    finished_at: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)


class LibraryEntry(Base):
    __tablename__ = "library_entries"
    __table_args__ = (
        UniqueConstraint("source_account_id", "external_id", name="uq_library_entries_external"),
        CheckConstraint(
            "match_status IN ('unmatched', 'candidates', 'confirmed', 'no_subject', "
            "'deferred')",
            name="ck_library_entries_match_status",
        ),
        CheckConstraint(
            "ownership_scope IN ('owned', 'visible', 'categorized', 'installed', 'unknown')",
            name="ck_library_entries_ownership_scope",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_account_id: Mapped[int] = mapped_column(
        ForeignKey("source_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    work_id: Mapped[int | None] = mapped_column(
        ForeignKey("works.id", ondelete="SET NULL"), index=True
    )
    title_observed: Mapped[str | None] = mapped_column(String)
    localized_titles_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    ownership_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    installed: Mapped[bool | None] = mapped_column(Boolean)
    playtime_minutes: Mapped[int | None] = mapped_column(Integer)
    last_played_at: Mapped[str | None] = mapped_column(String(32))
    metadata_source: Mapped[str | None] = mapped_column(String(32))
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    match_status: Mapped[str] = mapped_column(String(16), nullable=False, default="unmatched")
    match_reason: Mapped[str | None] = mapped_column(Text)
    match_updated_at: Mapped[str | None] = mapped_column(String(32))
    first_seen_at: Mapped[str] = mapped_column(String(32), nullable=False)
    last_seen_at: Mapped[str] = mapped_column(String(32), nullable=False)


class LibraryCollection(Base):
    __tablename__ = "library_collections"
    __table_args__ = (
        UniqueConstraint(
            "source_account_id", "external_id", name="uq_library_collections_external"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source_account_id: Mapped[int] = mapped_column(
        ForeignKey("source_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    first_seen_at: Mapped[str] = mapped_column(String(32), nullable=False)
    last_seen_at: Mapped[str] = mapped_column(String(32), nullable=False)


class LibraryEntryCollection(Base):
    __tablename__ = "library_entry_collections"

    library_entry_id: Mapped[int] = mapped_column(
        ForeignKey("library_entries.id", ondelete="CASCADE"), primary_key=True
    )
    collection_id: Mapped[int] = mapped_column(
        ForeignKey("library_collections.id", ondelete="CASCADE"), primary_key=True
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    first_seen_at: Mapped[str] = mapped_column(String(32), nullable=False)
    last_seen_at: Mapped[str] = mapped_column(String(32), nullable=False)


class LibraryMatchCandidate(Base):
    __tablename__ = "library_match_candidates"
    __table_args__ = (
        UniqueConstraint(
            "library_entry_id", "subject_id", name="uq_library_match_candidates_subject"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    library_entry_id: Mapped[int] = mapped_column(
        ForeignKey("library_entries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject_id: Mapped[int] = mapped_column(Integer, nullable=False)
    query: Mapped[str] = mapped_column(String, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    reasons_json: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    observed_at: Mapped[str] = mapped_column(String(32), nullable=False)


class LibraryMatchReview(Base):
    __tablename__ = "library_match_reviews"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('confirmed', 'no_subject', 'deferred', 'reopened')",
            name="ck_library_match_reviews_decision",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    library_entry_id: Mapped[int] = mapped_column(
        ForeignKey("library_entries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_id: Mapped[str | None] = mapped_column(
        ForeignKey("change_plans.id", ondelete="SET NULL"), nullable=True, index=True
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_id: Mapped[int | None] = mapped_column(Integer)
    score: Mapped[int | None] = mapped_column(Integer)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    previous_status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)


class RatingReviewState(Base):
    __tablename__ = "rating_review_states"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'rated', 'skipped', 'deferred')",
            name="ck_rating_review_states_state",
        ),
        CheckConstraint(
            "reason_state IN ('unknown', 'provided', 'skipped')",
            name="ck_rating_review_states_reason_state",
        ),
        CheckConstraint(
            "last_rating IS NULL OR (last_rating BETWEEN 1 AND 10)",
            name="ck_rating_review_states_rating",
        ),
    )

    subject_id: Mapped[int] = mapped_column(
        ForeignKey("bangumi_subjects.subject_id", ondelete="RESTRICT"), primary_key=True
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    last_rating: Mapped[int | None] = mapped_column(Integer)
    reason_private: Mapped[str | None] = mapped_column(Text)
    reason_state: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown")
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    reviewed_at: Mapped[str | None] = mapped_column(String(32))
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class RatingQueueSession(Base):
    __tablename__ = "rating_queue_sessions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('building', 'active', 'completed', 'cancelled', 'failed')",
            name="ck_rating_queue_sessions_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    selector_json: Mapped[str] = mapped_column(Text, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    order_name: Mapped[str] = mapped_column(String(32), nullable=False)
    random_seed: Mapped[int | None] = mapped_column(Integer)
    cursor_position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)


class RatingQueueItem(Base):
    __tablename__ = "rating_queue_items"
    __table_args__ = (
        UniqueConstraint("session_id", "subject_id", name="uq_rating_queue_item_subject"),
        UniqueConstraint("session_id", "position", name="uq_rating_queue_item_position"),
        CheckConstraint(
            "item_status IN ('pending', 'completed', 'stale', 'suppressed')",
            name="ck_rating_queue_items_status",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('rated', 'skipped', 'deferred')",
            name="ck_rating_queue_items_outcome",
        ),
        CheckConstraint(
            "enrichment_status IN ('local', 'fresh', 'failed')",
            name="ck_rating_queue_items_enrichment",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("rating_queue_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject_id: Mapped[int] = mapped_column(
        ForeignKey("bangumi_subjects.subject_id", ondelete="RESTRICT"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    initial_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    initial_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    enrichment_status: Mapped[str] = mapped_column(String(16), nullable=False)
    item_status: Mapped[str] = mapped_column(String(16), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(16))
    decided_at: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)


class RatingReviewEvent(Base):
    __tablename__ = "rating_review_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("rating_queue_sessions.id", ondelete="SET NULL"), index=True
    )
    queue_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("rating_queue_items.id", ondelete="SET NULL"), index=True
    )
    subject_id: Mapped[int] = mapped_column(
        ForeignKey("bangumi_subjects.subject_id", ondelete="RESTRICT"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    previous_state: Mapped[str | None] = mapped_column(String(16))
    next_state: Mapped[str] = mapped_column(String(16), nullable=False)
    rating: Mapped[int | None] = mapped_column(Integer)
    reason_private: Mapped[str | None] = mapped_column(Text)
    reason_state: Mapped[str] = mapped_column(String(16), nullable=False)
    comment_action: Mapped[str] = mapped_column(String(32), nullable=False)
    before_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    after_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)


class DiscoverySession(Base):
    __tablename__ = "discovery_sessions"
    __table_args__ = (
        CheckConstraint(
            "provider IN ('steam', 'bangumi_search', 'bangumi_browse')",
            name="ck_discovery_sessions_provider",
        ),
        CheckConstraint(
            "status IN ('building', 'active', 'completed', 'cancelled', 'failed')",
            name="ck_discovery_sessions_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    filters_json: Mapped[str] = mapped_column(Text, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    cursor_position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)


class DiscoveryReviewState(Base):
    __tablename__ = "discovery_review_states"
    __table_args__ = (
        CheckConstraint(
            "decision IS NULL OR decision IN ('played', 'not_played', 'unsure', 'deferred')",
            name="ck_discovery_review_states_decision",
        ),
        UniqueConstraint("candidate_key", name="uq_discovery_review_states_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    candidate_key: Mapped[str] = mapped_column(String, nullable=False)
    work_id: Mapped[int | None] = mapped_column(
        ForeignKey("works.id", ondelete="SET NULL"), index=True
    )
    subject_id: Mapped[int | None] = mapped_column(Integer, index=True)
    library_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("library_entries.id", ondelete="SET NULL"), index=True
    )
    decision: Mapped[str | None] = mapped_column(String(16))
    reason_private: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[str | None] = mapped_column(String(32))
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


class DiscoveryCandidate(Base):
    __tablename__ = "discovery_candidates"
    __table_args__ = (
        UniqueConstraint("session_id", "candidate_key", name="uq_discovery_candidate_key"),
        UniqueConstraint("session_id", "position", name="uq_discovery_candidate_position"),
        CheckConstraint(
            "item_status IN ('pending', 'decided', 'suppressed', 'identity_conflict')",
            name="ck_discovery_candidates_status",
        ),
        CheckConstraint(
            "decision IS NULL OR decision IN ('played', 'not_played', 'unsure', 'deferred')",
            name="ck_discovery_candidates_decision",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("discovery_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    review_state_id: Mapped[str | None] = mapped_column(
        ForeignKey("discovery_review_states.id", ondelete="SET NULL"), index=True
    )
    candidate_key: Mapped[str] = mapped_column(String, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    work_id: Mapped[int | None] = mapped_column(
        ForeignKey("works.id", ondelete="SET NULL"), index=True
    )
    subject_id: Mapped[int | None] = mapped_column(Integer, index=True)
    library_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("library_entries.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    cover_url: Mapped[str | None] = mapped_column(String)
    summary: Mapped[str | None] = mapped_column(Text)
    public_tags_json: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False)
    priority_score: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    item_status: Mapped[str] = mapped_column(String(24), nullable=False)
    decision: Mapped[str | None] = mapped_column(String(16))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[str | None] = mapped_column(String(32))


class DiscoveryReviewEvent(Base):
    __tablename__ = "discovery_review_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("discovery_sessions.id", ondelete="SET NULL"), index=True
    )
    candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("discovery_candidates.id", ondelete="SET NULL"), index=True
    )
    review_state_id: Mapped[str] = mapped_column(
        ForeignKey("discovery_review_states.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    previous_decision: Mapped[str | None] = mapped_column(String(16))
    next_decision: Mapped[str | None] = mapped_column(String(16))
    reason_private: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)


class MediaBlob(Base):
    __tablename__ = "media_blobs"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    storage_relpath: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    last_verified_at: Mapped[str | None] = mapped_column(String(32))
    last_accessed_at: Mapped[str | None] = mapped_column(String(32), index=True)


class MediaSource(Base):
    __tablename__ = "media_sources"
    __table_args__ = (
        CheckConstraint("provider IN ('bangumi', 'steam')", name="ck_media_sources_provider"),
        CheckConstraint("origin IN ('remote', 'steam_local')", name="ck_media_sources_origin"),
        CheckConstraint(
            "status IN ('observed', 'cached', 'stale', 'missing', 'failed')",
            name="ck_media_sources_status",
        ),
        UniqueConstraint(
            "provider",
            "external_id",
            "variant",
            "locale",
            "origin",
            name="uq_media_sources_identity",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    provider: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    variant: Mapped[str] = mapped_column(String(32), nullable=False)
    locale: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    remote_url: Mapped[str | None] = mapped_column(String)
    logical_locator_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="observed")
    current_blob_sha256: Mapped[str | None] = mapped_column(
        ForeignKey("media_blobs.sha256", ondelete="SET NULL"), index=True
    )
    etag: Mapped[str | None] = mapped_column(String)
    last_modified: Mapped[str | None] = mapped_column(String)
    observed_at: Mapped[str] = mapped_column(String(32), nullable=False)
    last_checked_at: Mapped[str | None] = mapped_column(String(32))
    fetched_at: Mapped[str | None] = mapped_column(String(32))
    failure_code: Mapped[str | None] = mapped_column(String(32))
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_after: Mapped[str | None] = mapped_column(String(32))


class MediaRendition(Base):
    __tablename__ = "media_renditions"

    media_source_id: Mapped[str] = mapped_column(
        ForeignKey("media_sources.id", ondelete="CASCADE"), primary_key=True
    )
    purpose: Mapped[str] = mapped_column(String(32), primary_key=True)
    blob_sha256: Mapped[str] = mapped_column(
        ForeignKey("media_blobs.sha256", ondelete="RESTRICT"), nullable=False, index=True
    )
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)


class MediaBinding(Base):
    __tablename__ = "media_bindings"
    __table_args__ = (
        CheckConstraint(
            "((work_id IS NOT NULL) + (library_entry_id IS NOT NULL) + "
            "(rating_queue_item_id IS NOT NULL) + (discovery_candidate_id IS NOT NULL)) = 1",
            name="ck_media_bindings_one_target",
        ),
        UniqueConstraint(
            "media_source_id",
            "work_id",
            "library_entry_id",
            "rating_queue_item_id",
            "discovery_candidate_id",
            "role",
            name="uq_media_bindings_target_role",
        ),
        Index(
            "uq_media_bindings_work_role",
            "media_source_id",
            "work_id",
            "role",
            unique=True,
            sqlite_where=text("work_id IS NOT NULL"),
        ),
        Index(
            "uq_media_bindings_library_role",
            "media_source_id",
            "library_entry_id",
            "role",
            unique=True,
            sqlite_where=text("library_entry_id IS NOT NULL"),
        ),
        Index(
            "uq_media_bindings_rating_role",
            "media_source_id",
            "rating_queue_item_id",
            "role",
            unique=True,
            sqlite_where=text("rating_queue_item_id IS NOT NULL"),
        ),
        Index(
            "uq_media_bindings_discovery_role",
            "media_source_id",
            "discovery_candidate_id",
            "role",
            unique=True,
            sqlite_where=text("discovery_candidate_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    media_source_id: Mapped[str] = mapped_column(
        ForeignKey("media_sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    work_id: Mapped[int | None] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), index=True
    )
    library_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("library_entries.id", ondelete="CASCADE"), index=True
    )
    rating_queue_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("rating_queue_items.id", ondelete="CASCADE"), index=True
    )
    discovery_candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("discovery_candidates.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pinned_blob_sha256: Mapped[str | None] = mapped_column(
        ForeignKey("media_blobs.sha256", ondelete="SET NULL"), index=True
    )
    first_observed_at: Mapped[str] = mapped_column(String(32), nullable=False)
    last_observed_at: Mapped[str] = mapped_column(String(32), nullable=False)


class UiJob(Base):
    __tablename__ = "ui_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', "
            "'cancel_requested', 'cancelled', 'interrupted')",
            name="ck_ui_jobs_status",
        ),
        CheckConstraint(
            "capability IN ('local_read', 'local_write', 'remote_read', 'remote_write')",
            name="ck_ui_jobs_capability",
        ),
        Index(
            "uq_ui_jobs_idempotency_key",
            "idempotency_key",
            unique=True,
            sqlite_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    capability: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    config_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    result_json: Mapped[str | None] = mapped_column(Text)
    progress_current: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_total: Mapped[int | None] = mapped_column(Integer)
    phase: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[str | None] = mapped_column(String(32))
    heartbeat_at: Mapped[str | None] = mapped_column(String(32))
    finished_at: Mapped[str | None] = mapped_column(String(32))


class UiJobEvent(Base):
    __tablename__ = "ui_job_events"
    __table_args__ = (
        UniqueConstraint("job_id", "sequence", name="uq_ui_job_events_sequence"),
        CheckConstraint(
            "level IN ('debug', 'info', 'warning', 'error')",
            name="ck_ui_job_events_level",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("ui_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    phase: Mapped[str | None] = mapped_column(String(64))
    progress_current: Mapped[int | None] = mapped_column(Integer)
    progress_total: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)


class PlanConfirmationNonce(Base):
    __tablename__ = "plan_confirmation_nonces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    nonce_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    browser_session_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("change_plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    used_at: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)


class SyncShadow(Base):
    __tablename__ = "sync_shadows"

    subject_id: Mapped[int] = mapped_column(
        ForeignKey("bangumi_subjects.subject_id", ondelete="CASCADE"), primary_key=True
    )
    remote_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    remote_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    remote_updated_at: Mapped[str | None] = mapped_column(String(32))
    synced_at: Mapped[str] = mapped_column(String(32), nullable=False)
    subject: Mapped[BangumiSubject] = relationship(back_populates="shadow")


class SyncConflict(Base):
    __tablename__ = "sync_conflicts"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'resolved', 'ignored')", name="ck_conflicts_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    subject_id: Mapped[int] = mapped_column(
        ForeignKey("bangumi_subjects.subject_id", ondelete="CASCADE"), nullable=False, index=True
    )
    field: Mapped[str] = mapped_column(String(16), nullable=False)
    base_json: Mapped[str] = mapped_column(Text, nullable=False)
    local_json: Mapped[str] = mapped_column(Text, nullable=False)
    remote_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)
    resolution: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    resolved_at: Mapped[str | None] = mapped_column(String(32))
    subject: Mapped[BangumiSubject] = relationship(back_populates="conflicts")


# Transitional aliases for callers; new code uses generic names.
Game = Work
CollectionState = BangumiCollectionState
GameTag = WorkTag
ExternalLink = WorkLink
