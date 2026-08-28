from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class DiscoveryPromotionPreview:
    candidate_id: str
    status: str
    work_id: int | None
    subject_id: int | None
    library_entry_id: int | None
    detail: str


class DiscoveryPromotionPort(Protocol):
    """Boundary reserved for Phase 8; Phase 7 exposes preview only."""

    def preview(self, candidate_id: str) -> DiscoveryPromotionPreview: ...

    def promote_identity(self, candidate_id: str) -> object:
        """Future explicit local identity mutation; not implemented in Phase 7."""
        ...

    def create_status_plan(self, candidate_id: str, status: str) -> object:
        """Future explicit Bangumi status draft; never applies it."""
        ...
