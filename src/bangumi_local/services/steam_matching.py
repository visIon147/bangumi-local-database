from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import hashlib
import json
import re
import unicodedata
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from bangumi_local.adapters.bangumi import BangumiClient
from bangumi_local.adapters.steam import fetch_store_titles
from bangumi_local.db.models import (
    BangumiSubject,
    GameProfile,
    LibraryEntry,
    LibraryMatchCandidate,
    LibraryMatchReview,
    Work,
    WorkLink,
)
from bangumi_local.db.repositories import utc_now_iso
from bangumi_local.domain.models import SubjectSearchCandidate, SubjectType
from bangumi_local.domain.plans import stable_json
from bangumi_local.services.steam_library import steam_account


class SteamMatchError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MatchCandidateView:
    subject_id: int
    title: str
    title_original: str
    release_date: str | None
    aliases: tuple[str, ...]
    score: int
    reasons: tuple[str, ...]
    url: str
    summary: str | None = None
    cover_url: str | None = None
    public_tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MatchSearchResult:
    app_id: str
    title: str
    queries: tuple[str, ...]
    candidates: tuple[MatchCandidateView, ...]


@dataclass(frozen=True, slots=True)
class PreparedMatchSearch:
    entry_id: int
    source_account_id: int
    app_id: str
    match_status: str
    work_id: int | None
    source_hash: str
    title_observed: str | None
    localized_titles: tuple[tuple[str, str], ...]
    query: str | None
    input_hash: str


@dataclass(frozen=True, slots=True)
class FetchedMatchCandidate:
    candidate: SubjectSearchCandidate
    matched_query: str
    api_rank: int
    score: int
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FetchedMatchSearch:
    prepared: PreparedMatchSearch
    title: str
    localized_titles: tuple[tuple[str, str], ...]
    store_metadata_observed: bool
    queries: tuple[str, ...]
    candidates: tuple[FetchedMatchCandidate, ...]


def _entry(session: Session, app_id: str, account_id: str | None) -> LibraryEntry:
    account = steam_account(session, account_id)
    entry = session.scalar(
        select(LibraryEntry).where(
            LibraryEntry.source_account_id == account.id,
            LibraryEntry.external_id == app_id,
        )
    )
    if entry is None:
        raise SteamMatchError(f"Steam AppID {app_id} has not been imported.")
    return entry


def _match_input_hash(session: Session, entry: LibraryEntry) -> str:
    candidate_rows = session.scalars(
        select(LibraryMatchCandidate)
        .where(LibraryMatchCandidate.library_entry_id == entry.id)
        .order_by(LibraryMatchCandidate.rank, LibraryMatchCandidate.subject_id)
    ).all()
    payload = {
        "entry_id": entry.id,
        "source_account_id": entry.source_account_id,
        "app_id": entry.external_id,
        "match_status": entry.match_status,
        "work_id": entry.work_id,
        "source_hash": entry.source_hash,
        "title_observed": entry.title_observed,
        "localized_titles_json": entry.localized_titles_json,
        "candidates": [
            {
                "subject_id": row.subject_id,
                "query": row.query,
                "rank": row.rank,
                "score": row.score,
                "reasons_json": row.reasons_json,
                "snapshot_json": row.snapshot_json,
                "observed_at": row.observed_at,
            }
            for row in candidate_rows
        ],
    }
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()


def prepare_match_input(
    session: Session,
    *,
    app_id: str,
    account_id: str | None,
    query: str | None,
) -> PreparedMatchSearch:
    entry = _entry(session, app_id, account_id)
    try:
        raw_localized = json.loads(entry.localized_titles_json or "{}")
    except json.JSONDecodeError as exc:
        raise SteamMatchError("Stored Steam localized titles are invalid.") from exc
    if not isinstance(raw_localized, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in raw_localized.items()
    ):
        raise SteamMatchError("Stored Steam localized titles are invalid.")
    normalized_query = (query or "").strip() or None
    return PreparedMatchSearch(
        entry_id=entry.id,
        source_account_id=entry.source_account_id,
        app_id=entry.external_id,
        match_status=entry.match_status,
        work_id=entry.work_id,
        source_hash=entry.source_hash,
        title_observed=entry.title_observed,
        localized_titles=tuple(sorted(raw_localized.items())),
        query=normalized_query,
        input_hash=_match_input_hash(session, entry),
    )


def prepare_match_search(
    session: Session,
    *,
    app_id: str,
    account_id: str | None,
    query: str | None,
) -> PreparedMatchSearch:
    prepared = prepare_match_input(
        session, app_id=app_id, account_id=account_id, query=query
    )
    if prepared.match_status == "confirmed":
        raise SteamMatchError("Confirmed mappings are immutable; resolve them explicitly first.")
    if prepared.match_status == "no_subject":
        raise SteamMatchError("This entry is marked no-subject; reopen it before searching.")
    return prepared


def normalize_title(value: str) -> str:
    # Remove presentation-only marks before NFKC; otherwise ™ becomes the
    # literal letters "TM" and incorrectly lowers an exact-title match.
    normalized = re.sub(r"[™®©]", "", value)
    normalized = unicodedata.normalize("NFKC", normalized).casefold()
    normalized = re.sub(r"[\[\(（【].*?[\]\)）】]", " ", normalized)
    normalized = re.sub(
        r"\b(game of the year|goty|definitive|deluxe|complete|remastered|edition)\b",
        " ",
        normalized,
    )
    normalized = re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff]+", " ", normalized)
    return " ".join(normalized.split())


def title_queries(title: str, localized: dict[str, str]) -> tuple[str, ...]:
    values = [title, *localized.values()]
    queries: list[str] = []
    for value in values:
        raw = value.strip()
        normalized = normalize_title(raw)
        for candidate in (raw, normalized):
            if candidate and candidate not in queries:
                queries.append(candidate)
    return tuple(queries)


def _candidate_score(
    queries: tuple[str, ...], candidate: SubjectSearchCandidate
) -> tuple[int, tuple[str, ...]]:
    names = tuple(
        value
        for value in (candidate.title_original, candidate.title_cn, *candidate.aliases)
        if value
    )
    normalized_queries = tuple(normalize_title(value) for value in queries)
    normalized_names = tuple(normalize_title(value) for value in names)
    exact = any(query == name for query in normalized_queries for name in normalized_names)
    ratio = max(
        (
            SequenceMatcher(None, query, name).ratio()
            for query in normalized_queries
            for name in normalized_names
            if query and name
        ),
        default=0.0,
    )
    score = round(ratio * 70) + (30 if exact else 0)
    reasons = [f"title_similarity={ratio:.3f}"]
    if exact:
        reasons.append("normalized_title_exact")
    source_text = " ".join(queries).casefold()
    candidate_text = " ".join(names).casefold()
    risky = ("dlc", "demo", "soundtrack", "tool")
    for marker in risky:
        if marker in candidate_text and marker not in source_text:
            score -= 25
            reasons.append(f"penalty_{marker}")
    return max(0, score), tuple(reasons)


def _candidate_snapshot(candidate: SubjectSearchCandidate) -> dict[str, object]:
    return {
        "subject_id": candidate.subject_id,
        "subject_type": int(candidate.subject_type),
        "title_original": candidate.title_original,
        "title_cn": candidate.title_cn,
        "summary": candidate.summary,
        "release_date": candidate.release_date,
        "cover_url": candidate.cover_url,
        "aliases": list(candidate.aliases),
        "public_tags": list(candidate.public_tags),
        "url": candidate.url,
    }


def fetch_match_search(
    prepared: PreparedMatchSearch,
    client: BangumiClient,
    *,
    include_store_titles: bool,
    timeout_seconds: float,
    limit: int = 10,
) -> FetchedMatchSearch:
    if not 1 <= limit <= 50:
        raise SteamMatchError("Candidate limit must be between 1 and 50.")
    localized = dict(prepared.localized_titles)
    observed_store_metadata = False
    if include_store_titles:
        store_titles = fetch_store_titles(
            prepared.app_id, timeout_seconds=timeout_seconds
        )
        localized.update(store_titles)
        observed_store_metadata = bool(localized)
    title = (prepared.query or prepared.title_observed or "").strip()
    if not title and localized:
        title = next(iter(localized.values())).strip()
    if not title:
        raise SteamMatchError("No Steam title is available; provide --query.")
    queries = title_queries(title, localized)
    candidates_by_id: dict[int, tuple[SubjectSearchCandidate, str, int]] = {}
    for search_query in queries:
        for rank, candidate in enumerate(
            client.search_subjects(search_query, subject_type=SubjectType.GAME, limit=limit),
            start=1,
        ):
            current = candidates_by_id.get(candidate.subject_id)
            if current is None or rank < current[2]:
                candidates_by_id[candidate.subject_id] = (candidate, search_query, rank)
    scored: list[FetchedMatchCandidate] = []
    for candidate, matched_query, rank in candidates_by_id.values():
        score, reasons = _candidate_score(queries, candidate)
        scored.append(
            FetchedMatchCandidate(candidate, matched_query, rank, score, reasons)
        )
    scored.sort(key=lambda item: (-item.score, item.api_rank, item.candidate.subject_id))
    return FetchedMatchSearch(
        prepared=prepared,
        title=title,
        localized_titles=tuple(sorted(localized.items())),
        store_metadata_observed=observed_store_metadata,
        queries=queries,
        candidates=tuple(scored),
    )


def verify_match_input(session: Session, prepared: PreparedMatchSearch) -> LibraryEntry:
    entry = session.get(LibraryEntry, prepared.entry_id)
    if (
        entry is None
        or entry.source_account_id != prepared.source_account_id
        or entry.external_id != prepared.app_id
        or _match_input_hash(session, entry) != prepared.input_hash
    ):
        raise SteamMatchError(
            "Steam source or stored candidates changed during search; retry with a fresh request."
        )
    return entry


def persist_match_search(
    session: Session,
    fetched: FetchedMatchSearch,
    *,
    preserve_match_status: bool = False,
) -> MatchSearchResult:
    entry = verify_match_input(session, fetched.prepared)
    localized = dict(fetched.localized_titles)
    entry.localized_titles_json = stable_json(localized)
    if entry.title_observed is None and localized:
        entry.title_observed = next(iter(localized.values()))
    if fetched.store_metadata_observed:
        entry.metadata_source = "steam_store_page"
    session.execute(
        delete(LibraryMatchCandidate).where(
            LibraryMatchCandidate.library_entry_id == entry.id
        )
    )
    now = utc_now_iso()
    views: list[MatchCandidateView] = []
    for display_rank, item in enumerate(fetched.candidates, start=1):
        candidate = item.candidate
        session.add(
            LibraryMatchCandidate(
                library_entry_id=entry.id,
                subject_id=candidate.subject_id,
                query=item.matched_query,
                rank=display_rank,
                score=item.score,
                reasons_json=stable_json(list(item.reasons)),
                snapshot_json=stable_json(_candidate_snapshot(candidate)),
                observed_at=now,
            )
        )
        views.append(
            MatchCandidateView(
                subject_id=candidate.subject_id,
                title=candidate.display_title,
                title_original=candidate.title_original,
                release_date=candidate.release_date,
                aliases=candidate.aliases,
                score=item.score,
                reasons=item.reasons,
                url=candidate.url,
                summary=candidate.summary,
                cover_url=candidate.cover_url,
                public_tags=candidate.public_tags,
            )
        )
    if not preserve_match_status:
        entry.match_status = "candidates" if views else "unmatched"
        entry.match_reason = "candidate_search_complete" if views else "candidate_search_no_results"
    entry.match_updated_at = now
    return MatchSearchResult(
        app_id=fetched.prepared.app_id,
        title=fetched.title,
        queries=fetched.queries,
        candidates=tuple(views),
    )


def search_matches(
    session: Session,
    client: BangumiClient,
    *,
    app_id: str,
    account_id: str | None,
    query: str | None,
    include_store_titles: bool,
    timeout_seconds: float,
    limit: int = 10,
) -> MatchSearchResult:
    """Compatibility wrapper; new orchestration should call the three stages."""

    prepared = prepare_match_search(
        session, app_id=app_id, account_id=account_id, query=query
    )
    fetched = fetch_match_search(
        prepared,
        client,
        include_store_titles=include_store_titles,
        timeout_seconds=timeout_seconds,
        limit=limit,
    )
    return persist_match_search(session, fetched)


def _record_review(
    session: Session,
    entry: LibraryEntry,
    *,
    decision: str,
    subject_id: int | None,
    previous_status: str,
    reason: str | None,
    plan_id: str | None = None,
    score: int | None = None,
    evidence: dict[str, object] | None = None,
) -> None:
    session.add(
        LibraryMatchReview(
            id=str(uuid4()),
            library_entry_id=entry.id,
            plan_id=plan_id,
            decision=decision,
            subject_id=subject_id,
            score=score,
            evidence_json=stable_json(evidence or {}),
            previous_status=previous_status,
            reason=reason,
            created_at=utc_now_iso(),
        )
    )


def confirm_match(
    session: Session,
    client: BangumiClient,
    *,
    app_id: str,
    subject_id: int,
    account_id: str | None,
    match_source: str = "manual_steam_review",
    match_confidence: str = "confirmed",
    review_reason: str = "manual_subject_confirmation",
    plan_id: str | None = None,
    score: int | None = None,
    evidence: dict[str, object] | None = None,
) -> tuple[LibraryEntry, Work]:
    candidate = client.get_subject(subject_id)
    return confirm_match_subject(
        session,
        candidate=candidate,
        app_id=app_id,
        account_id=account_id,
        match_source=match_source,
        match_confidence=match_confidence,
        review_reason=review_reason,
        plan_id=plan_id,
        score=score,
        evidence=evidence,
    )


def confirm_match_subject(
    session: Session,
    *,
    candidate: SubjectSearchCandidate,
    app_id: str,
    account_id: str | None,
    match_source: str = "manual_steam_review",
    match_confidence: str = "confirmed",
    review_reason: str = "manual_subject_confirmation",
    plan_id: str | None = None,
    score: int | None = None,
    evidence: dict[str, object] | None = None,
) -> tuple[LibraryEntry, Work]:
    entry = _entry(session, app_id, account_id)
    subject_id = candidate.subject_id
    if candidate.subject_type != SubjectType.GAME:
        raise SteamMatchError("Steam entries can only be matched to Bangumi game subjects.")
    linked = session.scalar(
        select(WorkLink).where(
            WorkLink.source == "steam", WorkLink.external_id == app_id
        )
    )
    identity = session.get(BangumiSubject, subject_id)
    if identity is not None:
        work = session.get(Work, identity.work_id)
        if work is None:
            raise SteamMatchError("Bangumi identity references a missing local work.")
    else:
        now = utc_now_iso()
        work = Work(
            kind="game",
            title=candidate.display_title,
            title_cn=candidate.title_cn,
            title_original=candidate.title_original,
            summary=candidate.summary,
            release_date=candidate.release_date,
            cover_url=candidate.cover_url,
            created_at=now,
            updated_at=now,
            bgm_subject_id=subject_id,
            bgm_url=candidate.url,
        )
        session.add(work)
        session.flush()
        session.add(
            BangumiSubject(
                subject_id=subject_id,
                work_id=work.id,
                subject_type=int(SubjectType.GAME),
                url=candidate.url,
                metadata_available=True,
                last_observed_at=now,
            )
        )
        session.add(GameProfile(work_id=work.id))
        session.add(
            WorkLink(
                work_id=work.id,
                source="bangumi",
                url=candidate.url,
                external_id=str(subject_id),
                is_primary=True,
                match_source=match_source,
                match_confidence=match_confidence,
                verified_at=now,
            )
        )
    if linked is not None and linked.work_id != work.id:
        raise SteamMatchError("Steam AppID is already linked to a different work.")
    previous = entry.match_status
    if linked is None:
        session.add(
            WorkLink(
                work_id=work.id,
                source="steam",
                url=f"https://store.steampowered.com/app/{app_id}/",
                external_id=app_id,
                is_primary=True,
                match_source=match_source,
                match_confidence=match_confidence,
                verified_at=utc_now_iso(),
            )
        )
    entry.work_id = work.id
    entry.match_status = "confirmed"
    entry.match_reason = review_reason
    entry.match_updated_at = utc_now_iso()
    _record_review(
        session,
        entry,
        decision="confirmed",
        subject_id=subject_id,
        previous_status=previous,
        reason=review_reason,
        plan_id=plan_id,
        score=score,
        evidence=evidence,
    )
    return entry, work


def set_match_disposition(
    session: Session,
    *,
    app_id: str,
    account_id: str | None,
    decision: str,
    reason: str | None,
    plan_id: str | None = None,
    evidence: dict[str, object] | None = None,
) -> LibraryEntry:
    if decision not in {"no_subject", "deferred", "reopened"}:
        raise SteamMatchError("Unsupported match review decision.")
    entry = _entry(session, app_id, account_id)
    if entry.match_status == "confirmed":
        raise SteamMatchError("A confirmed mapping cannot be changed by this command.")
    previous = entry.match_status
    if decision == "reopened":
        entry.match_status = "unmatched"
        entry.match_reason = reason or "manual_reopen"
    else:
        entry.match_status = decision
        entry.match_reason = reason
    entry.match_updated_at = utc_now_iso()
    _record_review(
        session,
        entry,
        decision=decision,
        subject_id=None,
        previous_status=previous,
        reason=reason,
        plan_id=plan_id,
        evidence=evidence,
    )
    return entry


def match_details(
    session: Session, *, app_id: str, account_id: str | None
) -> tuple[LibraryEntry, tuple[MatchCandidateView, ...]]:
    entry = _entry(session, app_id, account_id)
    rows = session.scalars(
        select(LibraryMatchCandidate)
        .where(LibraryMatchCandidate.library_entry_id == entry.id)
        .order_by(LibraryMatchCandidate.rank)
    ).all()
    candidates: list[MatchCandidateView] = []
    for row in rows:
        snapshot = json.loads(row.snapshot_json)
        candidates.append(
            MatchCandidateView(
                subject_id=row.subject_id,
                title=snapshot.get("title_cn") or snapshot["title_original"],
                title_original=snapshot["title_original"],
                release_date=snapshot.get("release_date"),
                aliases=tuple(snapshot.get("aliases", ())),
                score=row.score,
                reasons=tuple(json.loads(row.reasons_json)),
                url=snapshot["url"],
            )
        )
    return entry, tuple(candidates)
