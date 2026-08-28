from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from bangumi_local.db.models import BangumiSubject
from bangumi_local.db.repositories import apply_remote_fields, local_snapshot
from bangumi_local.domain.mutations import CollectionPatch
from bangumi_local.domain.snapshots import CollectionSnapshot


@dataclass(frozen=True, slots=True)
class LocalEditResult:
    subject_id: int
    changed_fields: tuple[str, ...]
    before: CollectionSnapshot
    after: CollectionSnapshot


def edit_local_collection(
    session: Session,
    subject_id: int,
    patch: CollectionPatch,
) -> LocalEditResult:
    if session.get(BangumiSubject, subject_id) is None:
        raise LookupError(f"Bangumi subject is not mirrored locally: {subject_id}")
    before = local_snapshot(session, subject_id)
    after = patch.apply_to(before)
    changed_fields = tuple(
        field
        for field in patch.fields
        if before.value_for(field) != after.value_for(field)
    )
    if changed_fields:
        apply_remote_fields(session, subject_id, after, changed_fields)
        if "rate" in changed_fields and after.rating is not None:
            from bangumi_local.services.rating_queue import observe_explicit_rating

            observe_explicit_rating(
                session, subject_id, after.rating, source="collection_edit"
            )
    return LocalEditResult(
        subject_id=subject_id,
        changed_fields=changed_fields,
        before=before,
        after=after,
    )
