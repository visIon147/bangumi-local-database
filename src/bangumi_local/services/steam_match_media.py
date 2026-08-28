from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from bangumi_local.db.models import MediaSource
from bangumi_local.domain.media import MediaReference
from bangumi_local.services.media import normalize_remote_image_url, register_media_references
from bangumi_local.services.plans import StoredPlan


@dataclass(frozen=True, slots=True)
class CandidateMediaSelection:
    observed: int
    missing: tuple[MediaReference, ...]


def register_match_candidate_media(
    session: Session,
    stored: StoredPlan,
    *,
    policy: str,
) -> CandidateMediaSelection:
    if policy not in {"none", "metadata", "cache"}:
        raise ValueError("Candidate image policy must be none, metadata, or cache.")
    if policy == "none":
        return CandidateMediaSelection(0, ())
    by_subject: dict[int, MediaReference] = {}
    for item in stored.candidates:
        raw_candidates = item.selection_evidence.get("match_candidates", ())
        if not isinstance(raw_candidates, list):
            continue
        for raw in raw_candidates:
            if not isinstance(raw, dict):
                continue
            subject_id = raw.get("subject_id")
            cover_url = raw.get("cover_url")
            if not isinstance(subject_id, int) or not isinstance(cover_url, str) or not cover_url:
                continue
            by_subject[subject_id] = MediaReference(
                provider="bangumi",
                external_id=str(subject_id),
                variant="preferred",
                origin="remote",
                remote_url=normalize_remote_image_url(cover_url, "bangumi"),
            )
    references = tuple(by_subject[key] for key in sorted(by_subject))
    register_media_references(session, references, policy="metadata")
    if policy != "cache":
        return CandidateMediaSelection(len(references), ())
    missing: list[MediaReference] = []
    for reference in references:
        digest = session.scalar(
            select(MediaSource.current_blob_sha256).where(
                MediaSource.provider == reference.provider,
                MediaSource.external_id == reference.external_id,
                MediaSource.variant == reference.variant,
                MediaSource.locale == reference.locale,
                MediaSource.origin == reference.origin,
            )
        )
        if digest is None:
            missing.append(reference)
    return CandidateMediaSelection(len(references), tuple(missing))
