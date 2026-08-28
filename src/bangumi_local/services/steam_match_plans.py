from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import re
import time
from typing import Callable, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from bangumi_local.adapters.bangumi import BangumiClient
from bangumi_local.db.models import (
    BangumiSubject,
    ChangePlan,
    LibraryCollection,
    LibraryEntry,
    LibraryEntryCollection,
    LibraryMatchCandidate,
    WorkLink,
)
from bangumi_local.domain.models import SubjectSearchCandidate, SubjectType
from bangumi_local.domain.plans import PlanCandidate, stable_json
from bangumi_local.services.plans import PlanError, StoredPlan, _save_plan, load_plan
from bangumi_local.services.steam_library import steam_account
from bangumi_local.services.steam_matching import (
    FetchedMatchSearch,
    MatchCandidateView,
    PreparedMatchSearch,
    SteamMatchError,
    fetch_match_search,
    normalize_title,
    persist_match_search,
    prepare_match_input,
    verify_match_input,
)
from bangumi_local.services.steam_plans import steam_source_precondition


@dataclass(frozen=True, slots=True)
class AutoMatchPolicy:
    score_threshold: int = 95
    minimum_margin: int = 20
    require_exact: bool = True

    def __post_init__(self) -> None:
        if not 0 <= self.score_threshold <= 100:
            raise PlanError("Automatic match threshold must be between 0 and 100.")
        if not 0 <= self.minimum_margin <= 100:
            raise PlanError("Automatic match margin must be between 0 and 100.")

    def as_dict(self) -> dict[str, object]:
        return {
            "score_threshold": self.score_threshold,
            "minimum_margin": self.minimum_margin,
            "require_exact": self.require_exact,
        }


@dataclass(frozen=True, slots=True)
class PreparedSteamMatchPlan:
    selector_json: str
    entries: tuple[PreparedMatchSearch, ...]
    policy: AutoMatchPolicy
    candidate_limit: int
    include_no_subject: bool = False
    include_deferred: bool = False


@dataclass(frozen=True, slots=True)
class FetchedSteamMatchPlanEntry:
    prepared: PreparedMatchSearch
    fetched: FetchedMatchSearch | None
    title_unavailable: bool = False


@dataclass(frozen=True, slots=True)
class FetchedSteamMatchPlan:
    prepared: PreparedSteamMatchPlan
    entries: tuple[FetchedSteamMatchPlanEntry, ...]


def steam_match_source_precondition(
    session: Session, entry_id: int
) -> tuple[str, tuple[str, ...]]:
    base_hash, collections = steam_source_precondition(session, entry_id)
    entry = session.get(LibraryEntry, entry_id)
    if entry is None:
        raise PlanError("Steam source entry is missing.")
    candidate_rows = session.scalars(
        select(LibraryMatchCandidate)
        .where(LibraryMatchCandidate.library_entry_id == entry_id)
        .order_by(LibraryMatchCandidate.rank)
    ).all()
    payload = {
        "base_hash": base_hash,
        "title_observed": entry.title_observed,
        "localized_titles_json": entry.localized_titles_json,
        "candidates": [
            {
                "subject_id": row.subject_id,
                "rank": row.rank,
                "score": row.score,
                "reasons_json": row.reasons_json,
                "snapshot_json": row.snapshot_json,
                "observed_at": row.observed_at,
            }
            for row in candidate_rows
        ],
    }
    digest = hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()
    return digest, collections


def _candidate_dict(candidate: MatchCandidateView) -> dict[str, object]:
    return {
        "subject_id": candidate.subject_id,
        "title": candidate.title,
        "title_original": candidate.title_original,
        "release_date": candidate.release_date,
        "aliases": list(candidate.aliases),
        "score": candidate.score,
        "reasons": list(candidate.reasons),
        "url": candidate.url,
        "summary": candidate.summary,
        "cover_url": candidate.cover_url,
        "public_tags": list(candidate.public_tags),
    }


def _selected_entries(
    session: Session,
    *,
    account_id: str | None,
    app_ids: tuple[str, ...] | None,
    collection_name: str | None,
    collection_regex: str | None,
    all_unmatched: bool,
    include_no_subject: bool,
    include_deferred: bool,
) -> tuple[list[LibraryEntry], dict[str, object]]:
    choices = sum(
        (
            app_ids is not None,
            collection_name is not None,
            collection_regex is not None,
            all_unmatched,
        )
    )
    if choices != 1:
        raise PlanError(
            "Choose exactly one of appids, collection, collection_regex, or all_unmatched."
        )
    account = steam_account(session, account_id)
    statement = select(LibraryEntry).where(LibraryEntry.source_account_id == account.id)
    selector: dict[str, object]
    if app_ids is not None:
        statement = statement.where(LibraryEntry.external_id.in_(app_ids))
        selector = {"mode": "steam_appids", "app_ids": list(app_ids)}
    elif all_unmatched:
        statement = statement.where(LibraryEntry.match_status != "confirmed")
        selector = {"mode": "steam_all_unmatched"}
    else:
        pattern = None
        if collection_regex is not None:
            try:
                pattern = re.compile(collection_regex)
            except re.error as exc:
                raise PlanError(f"Invalid collection regular expression: {exc}") from exc
        memberships = session.execute(
            select(LibraryEntryCollection.library_entry_id, LibraryCollection.name)
            .join(LibraryCollection, LibraryCollection.id == LibraryEntryCollection.collection_id)
            .where(
                LibraryCollection.source_account_id == account.id,
                LibraryEntryCollection.active.is_(True),
                LibraryCollection.active.is_(True),
            )
        ).all()
        selected_ids = {
            entry_id
            for entry_id, name in memberships
            if (collection_name is not None and name == collection_name)
            or (pattern is not None and pattern.search(name))
        }
        statement = statement.where(LibraryEntry.id.in_(selected_ids))
        selector = (
            {"mode": "steam_collection", "collection": collection_name}
            if collection_name is not None
            else {"mode": "steam_collection_regex", "collection_regex": collection_regex}
        )
    if app_ids is None:
        excluded_statuses = {"confirmed"}
        if not include_no_subject:
            excluded_statuses.add("no_subject")
        if not include_deferred:
            excluded_statuses.add("deferred")
        statement = statement.where(LibraryEntry.match_status.not_in(excluded_statuses))
    entries = session.scalars(statement.order_by(LibraryEntry.external_id)).all()
    if app_ids is not None:
        found = {entry.external_id for entry in entries}
        missing = sorted(set(app_ids) - found, key=int)
        if missing:
            raise PlanError(f"Steam AppIDs are not imported: {missing}")
    if not entries:
        raise PlanError("The Steam match selector evaluated no entries.")
    return list(entries), selector


def _target_work_id(session: Session, subject_id: int) -> int | None:
    identity = session.get(BangumiSubject, subject_id)
    return identity.work_id if identity is not None else None


def target_has_other_steam_link(
    session: Session, *, subject_id: int, app_id: str
) -> bool:
    identity = session.get(BangumiSubject, subject_id)
    if identity is None:
        return False
    return (
        session.scalar(
            select(WorkLink.id).where(
                WorkLink.work_id == identity.work_id,
                WorkLink.source == "steam",
                WorkLink.external_id != app_id,
            )
        )
        is not None
    )


def source_title_collisions(session: Session, entry: LibraryEntry) -> tuple[str, ...]:
    own_titles = {
        normalize_title(value)
        for value in (
            entry.title_observed,
            *json.loads(entry.localized_titles_json or "{}").values(),
        )
        if isinstance(value, str) and normalize_title(value)
    }
    if not own_titles:
        return ()
    siblings = session.scalars(
        select(LibraryEntry).where(
            LibraryEntry.source_account_id == entry.source_account_id,
            LibraryEntry.id != entry.id,
        )
    ).all()
    collisions: list[str] = []
    for sibling in siblings:
        sibling_titles = {
            normalize_title(value)
            for value in (
                sibling.title_observed,
                *json.loads(sibling.localized_titles_json or "{}").values(),
            )
            if isinstance(value, str) and normalize_title(value)
        }
        if own_titles & sibling_titles:
            collisions.append(sibling.external_id)
    return tuple(sorted(collisions, key=int))


def _automatic_decision(
    candidates: tuple[MatchCandidateView, ...], policy: AutoMatchPolicy
) -> tuple[bool, int, dict[str, bool]]:
    if not candidates:
        return False, 0, {
            "threshold": False,
            "margin": False,
            "exact": False,
            "risk_free": False,
        }
    first = candidates[0]
    second_score = candidates[1].score if len(candidates) > 1 else 0
    margin = first.score - second_score
    reasons = set(first.reasons)
    checks = {
        "threshold": first.score >= policy.score_threshold,
        "margin": margin >= policy.minimum_margin,
        "exact": (not policy.require_exact) or "normalized_title_exact" in reasons,
        "risk_free": not any(reason.startswith("penalty_") for reason in reasons),
    }
    return all(checks.values()), margin, checks


def prepare_steam_match_plan(
    session: Session,
    *,
    account_id: str | None,
    app_ids: tuple[str, ...] | None = None,
    collection_name: str | None = None,
    collection_regex: str | None = None,
    all_unmatched: bool = False,
    include_no_subject: bool = False,
    include_deferred: bool = False,
    candidate_image_policy: str = "metadata",
    policy: AutoMatchPolicy = AutoMatchPolicy(),
    candidate_limit: int = 10,
    batch_offset: int = 0,
    max_items: int = 250,
) -> PreparedSteamMatchPlan:
    if not 1 <= candidate_limit <= 50:
        raise PlanError("Candidate limit must be between 1 and 50.")
    if batch_offset < 0 or max_items < 1:
        raise PlanError("Batch offset must be non-negative and max items must be positive.")
    if app_ids is not None and len(app_ids) > 250:
        raise PlanError("At most 250 explicit Steam AppIDs can be matched at once.")
    if candidate_image_policy not in {"none", "metadata", "cache"}:
        raise PlanError("Candidate image policy must be none, metadata, or cache.")
    entries, selector = _selected_entries(
        session,
        account_id=account_id,
        app_ids=app_ids,
        collection_name=collection_name,
        collection_regex=collection_regex,
        all_unmatched=all_unmatched,
        include_no_subject=include_no_subject,
        include_deferred=include_deferred,
    )
    eligible_count = len(entries)
    if app_ids is None:
        if eligible_count > 250 and batch_offset == 0 and max_items == 250:
            raise PlanError(
                f"The selector contains {eligible_count} entries; explicitly use a smaller "
                "max-items and stable offset, or narrow the selector."
            )
        entries = entries[batch_offset : batch_offset + max_items]
        if not entries:
            raise PlanError(
                f"Batch offset {batch_offset} is outside the {eligible_count}-entry selection."
            )
    selector.update(
        {
            "policy": policy.as_dict(),
            "scoring_version": 1,
            "candidate_limit": candidate_limit,
            "eligible_count": eligible_count,
            "batch_offset": 0 if app_ids is not None else batch_offset,
            "max_items": eligible_count if app_ids is not None else max_items,
            "evaluated_app_ids": [entry.external_id for entry in entries],
            "network_read": True,
            "include_no_subject": include_no_subject,
            "include_deferred": include_deferred,
            "candidate_image_policy": candidate_image_policy,
        }
    )
    prepared_entries = tuple(
        prepare_match_input(
            session,
            app_id=entry.external_id,
            account_id=account_id,
            query=None,
        )
        for entry in entries
    )
    return PreparedSteamMatchPlan(
        selector_json=stable_json(selector),
        entries=prepared_entries,
        policy=policy,
        candidate_limit=candidate_limit,
        include_no_subject=include_no_subject,
        include_deferred=include_deferred,
    )


def fetch_steam_match_plan(
    prepared: PreparedSteamMatchPlan,
    client: BangumiClient,
    *,
    include_store_titles: bool = True,
    timeout_seconds: float = 20.0,
    request_delay_seconds: float = 0.25,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> FetchedSteamMatchPlan:
    if request_delay_seconds < 0:
        raise PlanError("Batch request delay must not be negative.")
    results: list[FetchedSteamMatchPlanEntry] = []
    for index, entry in enumerate(prepared.entries):
        if index and request_delay_seconds:
            sleep_fn(request_delay_seconds)
        if entry.match_status == "confirmed" or (
            entry.match_status == "no_subject" and not prepared.include_no_subject
        ) or (
            entry.match_status == "deferred" and not prepared.include_deferred
        ):
            results.append(FetchedSteamMatchPlanEntry(entry, None))
            continue
        try:
            fetched = fetch_match_search(
                entry,
                client,
                include_store_titles=include_store_titles,
                timeout_seconds=timeout_seconds,
                limit=prepared.candidate_limit,
            )
        except SteamMatchError as exc:
            if "No Steam title is available" not in str(exc):
                raise
            results.append(
                FetchedSteamMatchPlanEntry(entry, None, title_unavailable=True)
            )
        else:
            results.append(FetchedSteamMatchPlanEntry(entry, fetched))
    return FetchedSteamMatchPlan(prepared, tuple(results))


def persist_steam_match_plan(
    session: Session,
    fetched_plan: FetchedSteamMatchPlan,
) -> StoredPlan:
    prepared_plan = fetched_plan.prepared
    # Verify the entire frozen selection before changing the first candidate row.
    # A failure rolls back the short persistence transaction without a partial plan.
    for item in prepared_plan.entries:
        verify_match_input(session, item)

    planned: list[PlanCandidate] = []
    for fetched_entry in fetched_plan.entries:
        prepared = fetched_entry.prepared
        title = prepared.title_observed or f"Steam App {prepared.app_id}"
        if prepared.match_status == "confirmed":
            source_hash, collections = steam_match_source_precondition(
                session, prepared.entry_id
            )
            planned.append(
                PlanCandidate(
                    work_id=prepared.work_id,
                    subject_id=None,
                    source_entry_id=prepared.entry_id,
                    title=title,
                    bgm_url=None,
                    disposition="unchanged",
                    reason="steam_already_confirmed",
                    action={"operation": "none"},
                    selection_evidence={
                        "steam_app_id": prepared.app_id,
                        "steam_collections": list(collections),
                        "match_status": prepared.match_status,
                        "match_candidates": [],
                    },
                    source_precondition_hash=source_hash,
                )
            )
            continue
        if prepared.match_status in {"no_subject", "deferred"} and fetched_entry.fetched is None:
            source_hash, collections = steam_match_source_precondition(
                session, prepared.entry_id
            )
            reason = (
                "steam_no_subject"
                if prepared.match_status == "no_subject"
                else "steam_match_deferred"
            )
            planned.append(
                PlanCandidate(
                    work_id=None,
                    subject_id=None,
                    source_entry_id=prepared.entry_id,
                    title=title,
                    bgm_url=None,
                    disposition="unchanged",
                    reason=reason,
                    action={"operation": "none"},
                    selection_evidence={
                        "steam_app_id": prepared.app_id,
                        "steam_collections": list(collections),
                        "match_status": prepared.match_status,
                        "match_candidates": [],
                    },
                    source_precondition_hash=source_hash,
                )
            )
            continue
        if fetched_entry.title_unavailable:
            source_hash, collections = steam_match_source_precondition(
                session, prepared.entry_id
            )
            planned.append(
                PlanCandidate(
                    work_id=None,
                    subject_id=None,
                    source_entry_id=prepared.entry_id,
                    title=title,
                    bgm_url=None,
                    disposition="unchanged",
                    reason="steam_match_title_unavailable",
                    action={"operation": "none"},
                    selection_evidence={
                        "steam_app_id": prepared.app_id,
                        "steam_collections": list(collections),
                        "match_status": prepared.match_status,
                        "match_candidates": [],
                        "review_mode": "manual_required",
                    },
                    source_precondition_hash=source_hash,
                )
            )
            continue
        if fetched_entry.fetched is None:
            raise PlanError("Steam match fetch result is incomplete.")
        result = persist_match_search(
            session,
            fetched_entry.fetched,
            preserve_match_status=prepared.match_status in {"no_subject", "deferred"},
        )
        title = result.title
        source_hash, collections = steam_match_source_precondition(
            session, prepared.entry_id
        )
        automatic, margin, checks = _automatic_decision(
            result.candidates, prepared_plan.policy
        )
        top = result.candidates[0] if result.candidates else None
        collision = (
            top is not None
            and target_has_other_steam_link(
                session, subject_id=top.subject_id, app_id=prepared.app_id
            )
        )
        entry = session.get(LibraryEntry, prepared.entry_id)
        assert entry is not None
        title_collisions = source_title_collisions(session, entry)
        automatic = automatic and not collision and not title_collisions
        evidence: dict[str, object] = {
            "steam_app_id": prepared.app_id,
            "steam_collections": list(collections),
            "match_status": entry.match_status,
            "queries": list(result.queries),
            "match_candidates": [_candidate_dict(item) for item in result.candidates],
            "selected_subject_id": top.subject_id if top is not None else None,
            "selected_score": top.score if top is not None else None,
            "top_margin": margin if top is not None else None,
            "auto_checks": checks,
            "existing_mapping_collision": collision,
            "source_title_collisions": list(title_collisions),
            "review_mode": "automatic" if automatic else "manual_required",
        }
        if top is None:
            disposition = "unchanged"
            reason = "steam_match_no_candidates"
            action: dict[str, object] = {"operation": "none"}
        elif automatic:
            disposition = "planned"
            reason = "steam_match_auto_confirm"
            action = {
                "operation": "confirm_match",
                "subject_id": top.subject_id,
                "score": top.score,
                "confirmation": "automatic",
            }
        else:
            disposition = "unchanged"
            reason = (
                "steam_match_mapping_collision"
                if collision
                else "steam_match_source_title_collision"
                if title_collisions
                else "steam_match_manual_review"
            )
            action = {"operation": "none"}
        planned.append(
            PlanCandidate(
                work_id=_target_work_id(session, top.subject_id) if top is not None else None,
                subject_id=top.subject_id if automatic and top is not None else None,
                source_entry_id=prepared.entry_id,
                title=title,
                bgm_url=top.url if top is not None else None,
                disposition=disposition,
                reason=reason,
                action=action,
                selection_evidence=evidence,
                source_precondition_hash=source_hash,
            )
        )

    automatic_by_subject: dict[int, list[int]] = {}
    for index, item in enumerate(planned):
        if item.disposition == "planned" and item.subject_id is not None:
            automatic_by_subject.setdefault(item.subject_id, []).append(index)
    for subject_id, indexes in automatic_by_subject.items():
        if len(indexes) < 2:
            continue
        for index in indexes:
            item = planned[index]
            evidence = dict(item.selection_evidence)
            evidence["batch_subject_collision"] = subject_id
            evidence["review_mode"] = "manual_required"
            planned[index] = replace(
                item,
                work_id=None,
                subject_id=None,
                disposition="unchanged",
                reason="steam_match_batch_collision",
                action={"operation": "none"},
                selection_evidence=evidence,
            )
    selector = json.loads(prepared_plan.selector_json)
    return _save_plan(
        session,
        kind="steam_match",
        operation="local_match",
        selector=selector,
        candidates=planned,
        format_version=4,
    )


def create_steam_match_plan(
    session: Session,
    client: BangumiClient,
    *,
    account_id: str | None,
    app_ids: tuple[str, ...] | None = None,
    collection_name: str | None = None,
    collection_regex: str | None = None,
    all_unmatched: bool = False,
    include_no_subject: bool = False,
    include_deferred: bool = False,
    candidate_image_policy: str = "metadata",
    policy: AutoMatchPolicy = AutoMatchPolicy(),
    include_store_titles: bool = True,
    timeout_seconds: float = 20.0,
    candidate_limit: int = 10,
    batch_offset: int = 0,
    max_items: int = 250,
    request_delay_seconds: float = 0.25,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> StoredPlan:
    """Compatibility wrapper for CLI/tests; UI jobs use the explicit stages."""

    prepared = prepare_steam_match_plan(
        session,
        account_id=account_id,
        app_ids=app_ids,
        collection_name=collection_name,
        collection_regex=collection_regex,
        all_unmatched=all_unmatched,
        include_no_subject=include_no_subject,
        include_deferred=include_deferred,
        candidate_image_policy=candidate_image_policy,
        policy=policy,
        candidate_limit=candidate_limit,
        batch_offset=batch_offset,
        max_items=max_items,
    )
    fetched = fetch_steam_match_plan(
        prepared,
        client,
        include_store_titles=include_store_titles,
        timeout_seconds=timeout_seconds,
        request_delay_seconds=request_delay_seconds,
        sleep_fn=sleep_fn,
    )
    return persist_steam_match_plan(session, fetched)


RevisionDecision = Literal["subject", "manual_review", "no_subject", "deferred"]


def revise_steam_match_plan(
    session: Session,
    client: BangumiClient | None,
    *,
    plan_id: str,
    app_id: str,
    decision: RevisionDecision,
    subject_id: int | None = None,
) -> StoredPlan:
    stored = load_plan(session, plan_id)
    if stored.plan.kind != "steam_match" or stored.plan.format_version < 4:
        raise PlanError("Only a v4 Steam match plan can be revised.")
    if stored.plan.status != "draft":
        raise PlanError("Only a draft Steam match plan can be revised.")
    matched = [
        (index, item)
        for index, item in enumerate(stored.candidates)
        if item.selection_evidence.get("steam_app_id") == app_id
    ]
    if len(matched) != 1:
        raise PlanError(f"Steam AppID {app_id} is not uniquely present in the plan.")
    index, current = matched[0]
    if current.source_entry_id is None:
        raise PlanError("Steam match plan item has no source entry.")
    source_hash, _ = steam_match_source_precondition(session, current.source_entry_id)
    if source_hash != current.source_precondition_hash:
        raise PlanError("Steam source or match state changed; generate a fresh match plan.")
    evidence = dict(current.selection_evidence)
    evidence["review_mode"] = "manual_override"
    evidence["revised_from_plan_id"] = plan_id
    if decision == "subject":
        if subject_id is None or client is None:
            raise PlanError("A subject decision requires a Bangumi subject ID and network client.")
        subject: SubjectSearchCandidate = client.get_subject(subject_id)
        if subject.subject_type != SubjectType.GAME:
            raise PlanError("Steam entries can only be matched to Bangumi game subjects.")
        existing = next(
            (
                raw
                for raw in evidence.get("match_candidates", [])
                if isinstance(raw, dict) and raw.get("subject_id") == subject_id
            ),
            None,
        )
        score = int(existing["score"]) if isinstance(existing, dict) else None
        if existing is None:
            raw_candidates = evidence.get("match_candidates")
            candidate_evidence = list(raw_candidates) if isinstance(raw_candidates, list) else []
            candidate_evidence.append(
                {
                    "subject_id": subject.subject_id,
                    "title": subject.display_title,
                    "title_original": subject.title_original,
                    "release_date": subject.release_date,
                    "aliases": list(subject.aliases),
                    "score": None,
                    "reasons": ["manual_subject_id"],
                    "url": subject.url,
                    "summary": subject.summary,
                    "cover_url": subject.cover_url,
                    "public_tags": list(subject.public_tags),
                }
            )
            evidence["match_candidates"] = candidate_evidence
        evidence.update(
            {
                "selected_subject_id": subject_id,
                "selected_score": score,
                "manual_subject_not_in_candidates": existing is None,
            }
        )
        replacement = replace(
            current,
            work_id=_target_work_id(session, subject_id),
            subject_id=subject_id,
            bgm_url=subject.url,
            disposition="planned",
            reason="steam_match_manual_override",
            action={
                "operation": "confirm_match",
                "subject_id": subject_id,
                "score": score,
                "confirmation": "manual_override",
            },
            selection_evidence=evidence,
        )
    elif decision == "no_subject":
        evidence["selected_subject_id"] = None
        replacement = replace(
            current,
            work_id=None,
            subject_id=None,
            bgm_url=None,
            disposition="planned",
            reason="steam_match_mark_no_subject",
            action={"operation": "set_no_subject"},
            selection_evidence=evidence,
        )
    elif decision == "deferred":
        evidence["selected_subject_id"] = None
        replacement = replace(
            current,
            work_id=None,
            subject_id=None,
            bgm_url=None,
            disposition="planned",
            reason="steam_match_mark_deferred",
            action={"operation": "defer_match"},
            selection_evidence=evidence,
        )
    else:
        evidence["review_mode"] = "manual_required"
        replacement = replace(
            current,
            work_id=None,
            subject_id=None,
            disposition="unchanged",
            reason="steam_match_manual_review",
            action={"operation": "none"},
            selection_evidence=evidence,
        )
    candidates = list(stored.candidates)
    candidates[index] = replacement
    selected_subjects = [
        item.subject_id
        for item in candidates
        if item.disposition == "planned" and item.subject_id is not None
    ]
    if len(selected_subjects) != len(set(selected_subjects)):
        raise PlanError("The revised plan would map multiple Steam entries to one subject.")
    selector = dict(json.loads(stored.plan.selector_json))
    selector["supersedes_plan_id"] = plan_id
    selector["revision"] = {
        "steam_app_id": app_id,
        "decision": decision,
        "subject_id": subject_id,
    }
    successor = _save_plan(
        session,
        kind="steam_match",
        operation="local_match",
        selector=selector,
        candidates=candidates,
        format_version=4,
    )
    source_plan = session.get(ChangePlan, plan_id)
    assert source_plan is not None
    source_plan.status = "cancelled"
    return successor
