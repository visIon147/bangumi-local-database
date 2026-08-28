from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bangumi_local.config import Settings
from bangumi_local.domain.models import (
    CollectionStatus,
    RemoteCollection,
    RemoteSubject,
    RemoteUser,
    SubjectSearchCandidate,
    SubjectType,
)
from bangumi_local.domain.mutations import CollectionPatch


class BangumiAPIError(RuntimeError):
    """A sanitized Bangumi API error that never renders request headers."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
        timed_out: bool = False,
        error_kind: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.timed_out = timed_out
        self.error_kind = error_kind


class _APIModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _User(_APIModel):
    id: int
    username: str
    nickname: str


class _Images(_APIModel):
    large: str | None = None
    common: str | None = None
    medium: str | None = None
    small: str | None = None
    grid: str | None = None

    def preferred(self) -> str | None:
        return self.common or self.large or self.medium or self.small or self.grid


class _SubjectTag(_APIModel):
    name: str
    count: int = 0


class _SlimSubject(_APIModel):
    id: int
    type: int
    name: str
    name_cn: str = ""
    short_summary: str = ""
    date: str | None = None
    images: _Images | None = None
    tags: list[_SubjectTag] = Field(default_factory=list)


class _InfoboxItem(_APIModel):
    key: str
    value: Any = None


class _SubjectRating(_APIModel):
    rank: int = 0
    total: int = 0
    score: float = 0.0


class _SubjectDetail(_APIModel):
    id: int
    type: SubjectType | None = None
    name: str = ""
    name_cn: str = ""
    summary: str = ""
    date: str | None = None
    images: _Images | None = None
    tags: list[_SubjectTag] = Field(default_factory=list)
    infobox: list[_InfoboxItem] = Field(default_factory=list)
    rating: _SubjectRating | None = None
    nsfw: bool = False


class _SubjectSearchPage(_APIModel):
    total: int = Field(default=0, ge=0)
    limit: int = Field(default=0, ge=0)
    offset: int = Field(default=0, ge=0)
    data: list[_SubjectDetail] = Field(default_factory=list)


class _UserCollection(_APIModel):
    subject_id: int
    subject_type: SubjectType
    rate: int = Field(ge=0, le=10)
    type: CollectionStatus
    comment: str | None = None
    tags: list[str]
    updated_at: datetime
    private: bool
    subject: _SlimSubject | None = None

    def to_domain(self) -> RemoteCollection:
        subject = self.subject
        remote_subject = RemoteSubject(
            subject_id=self.subject_id,
            title_original=subject.name if subject else f"Bangumi subject {self.subject_id}",
            title_cn=(subject.name_cn.strip() or None) if subject else None,
            summary=(subject.short_summary.strip() or None) if subject else None,
            release_date=subject.date if subject else None,
            cover_url=subject.images.preferred() if subject and subject.images else None,
            metadata_available=subject is not None,
            public_tags=tuple(tag.name for tag in subject.tags) if subject else (),
        )
        return RemoteCollection(
            subject_id=self.subject_id,
            subject_type=self.subject_type,
            status=self.type,
            rate=self.rate,
            comment=self.comment,
            tags=tuple(self.tags),
            updated_at=self.updated_at,
            private=self.private,
            subject=remote_subject,
        )


class _CollectionPage(_APIModel):
    total: int = Field(default=0, ge=0)
    limit: int = Field(default=0, ge=0)
    offset: int = Field(default=0, ge=0)
    data: list[_UserCollection] = Field(default_factory=list)


class BangumiClient:
    """Typed client for the documented Bangumi /v0 collection endpoints."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        token, username, user_agent = settings.require_bangumi()
        self.username = username
        self._client = httpx.Client(
            base_url=settings.bangumi_base_url.rstrip("/"),
            timeout=settings.bangumi_request_timeout_seconds,
            headers={
                "Authorization": f"Bearer {token.get_secret_value()}",
                "User-Agent": user_agent,
                "Accept": "application/json",
            },
            transport=transport,
        )

    def __enter__(self) -> BangumiClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if raw is None:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            return None

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            response = self._client.request(method, path, params=params, json=json_body)
        except httpx.TimeoutException as exc:
            raise BangumiAPIError(
                f"Bangumi API request timed out: {method} {path}",
                timed_out=True,
                error_kind=type(exc).__name__,
            ) from exc
        except httpx.HTTPError as exc:
            error_kind = type(exc).__name__
            raise BangumiAPIError(
                f"Bangumi API transport failed ({error_kind}): {method} {path}",
                error_kind=error_kind,
            ) from exc
        if not response.is_success:
            raise BangumiAPIError(
                f"Bangumi API request failed: {method} {path} returned HTTP {response.status_code}",
                status_code=response.status_code,
                retry_after_seconds=self._retry_after(response),
            )
        return response

    def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        response = self._request("GET", path, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise BangumiAPIError(f"Bangumi API returned invalid JSON for GET {path}") from exc

    def get_me(self) -> RemoteUser:
        try:
            user = _User.model_validate(self._get_json("/v0/me"))
        except ValidationError as exc:
            raise BangumiAPIError("Bangumi /v0/me response did not match the public schema") from exc
        return RemoteUser(id=user.id, username=user.username, nickname=user.nickname)

    def get_collections(
        self,
        *,
        subject_type: SubjectType | int | str | None = None,
        page_size: int = 50,
    ) -> list[RemoteCollection]:
        if not 1 <= page_size <= 50:
            raise ValueError("page_size must be between 1 and 50")
        path = f"/v0/users/{self.username}/collections"
        offset = 0
        results: list[RemoteCollection] = []

        while True:
            params: dict[str, Any] = {"limit": page_size, "offset": offset}
            if subject_type is not None:
                params["subject_type"] = int(SubjectType.parse(subject_type))
            try:
                page = _CollectionPage.model_validate(
                    self._get_json(path, params=params)
                )
            except ValidationError as exc:
                raise BangumiAPIError(
                    "Bangumi collections response did not match the public schema"
                ) from exc
            results.extend(item.to_domain() for item in page.data)
            offset += len(page.data)
            if not page.data or offset >= page.total:
                break

        return results

    def get_collection(self, subject_id: int) -> RemoteCollection:
        path = f"/v0/users/{self.username}/collections/{subject_id}"
        try:
            collection = _UserCollection.model_validate(self._get_json(path))
        except ValidationError as exc:
            raise BangumiAPIError(
                "Bangumi collection response did not match the public schema"
            ) from exc
        return collection.to_domain()

    def get_subject_public_tags(self, subject_id: int) -> tuple[str, ...]:
        path = f"/v0/subjects/{subject_id}"
        try:
            subject = _SubjectDetail.model_validate(self._get_json(path))
        except ValidationError as exc:
            raise BangumiAPIError(
                "Bangumi subject response did not match the public schema"
            ) from exc
        return tuple(tag.name for tag in subject.tags)

    @staticmethod
    def _aliases(subject: _SubjectDetail) -> tuple[str, ...]:
        aliases: list[str] = []

        def collect(value: object) -> None:
            if isinstance(value, str):
                normalized = value.strip()
                if normalized:
                    aliases.append(normalized)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        collect(item.get("v") or item.get("value") or item.get("name"))
                    else:
                        collect(item)
            elif isinstance(value, dict):
                for item in value.values():
                    collect(item)

        for item in subject.infobox:
            if item.key.casefold() in {"别名", "alias", "alias name"}:
                collect(item.value)
        return tuple(dict.fromkeys(aliases))

    @classmethod
    def _to_search_candidate(cls, subject: _SubjectDetail) -> SubjectSearchCandidate:
        if subject.type is None:
            raise BangumiAPIError("Bangumi subject response omitted its type")
        return SubjectSearchCandidate(
            subject_id=subject.id,
            subject_type=subject.type,
            title_original=subject.name or f"Bangumi subject {subject.id}",
            title_cn=subject.name_cn.strip() or None,
            summary=subject.summary.strip() or None,
            release_date=subject.date,
            cover_url=subject.images.preferred() if subject.images else None,
            aliases=cls._aliases(subject),
            public_tags=tuple(tag.name for tag in subject.tags),
            rank=subject.rating.rank if subject.rating and subject.rating.rank > 0 else None,
            score=subject.rating.score if subject.rating and subject.rating.total > 0 else None,
            rating_count=subject.rating.total if subject.rating else None,
        )

    def get_subject(self, subject_id: int) -> SubjectSearchCandidate:
        path = f"/v0/subjects/{subject_id}"
        try:
            subject = _SubjectDetail.model_validate(self._get_json(path))
        except ValidationError as exc:
            raise BangumiAPIError(
                "Bangumi subject response did not match the public schema"
            ) from exc
        return self._to_search_candidate(subject)

    def search_subjects(
        self, keyword: str, *, subject_type: SubjectType = SubjectType.GAME, limit: int = 10
    ) -> list[SubjectSearchCandidate]:
        return self.search_subjects_filtered(
            keyword,
            subject_type=subject_type,
            limit=limit,
        )

    def search_subjects_filtered(
        self,
        keyword: str,
        *,
        subject_type: SubjectType = SubjectType.GAME,
        limit: int = 50,
        sort: str = "match",
        meta_tags: tuple[str, ...] = (),
        year_from: int | None = None,
        year_to: int | None = None,
        min_rating_count: int | None = None,
        include_nsfw: bool = False,
        page_interval_seconds: float = 0.25,
    ) -> list[SubjectSearchCandidate]:
        query = keyword.strip()
        if not query:
            raise ValueError("Bangumi search keyword must not be empty.")
        if not 1 <= limit <= 200:
            raise ValueError("Bangumi discovery search limit must be between 1 and 200.")
        if sort not in {"match", "heat", "rank", "score"}:
            raise ValueError("Bangumi search sort must be match, heat, rank, or score.")
        filters: dict[str, Any] = {
            "type": [int(subject_type)],
            "nsfw": bool(include_nsfw),
        }
        if meta_tags:
            filters["meta_tags"] = list(meta_tags)
        air_date: list[str] = []
        if year_from is not None:
            air_date.append(f">={year_from:04d}-01-01")
        if year_to is not None:
            air_date.append(f"<{year_to + 1:04d}-01-01")
        if air_date:
            filters["air_date"] = air_date
        if min_rating_count is not None:
            filters["rating_count"] = [f">={min_rating_count}"]
        results: list[SubjectSearchCandidate] = []
        offset = 0
        while len(results) < limit:
            page_limit = min(50, limit - len(results))
            response = self._request(
                "POST",
                "/v0/search/subjects",
                params={"limit": page_limit, "offset": offset},
                json_body={"keyword": query, "sort": sort, "filter": filters},
            )
            try:
                page = _SubjectSearchPage.model_validate(response.json())
            except (ValueError, ValidationError) as exc:
                raise BangumiAPIError(
                    "Bangumi subject search response did not match the public schema"
                ) from exc
            raw_batch = page.data
            batch = [
                self._to_search_candidate(item)
                for item in raw_batch
                if include_nsfw or not item.nsfw
            ]
            results.extend(batch)
            offset += len(raw_batch)
            if not raw_batch or offset >= page.total:
                break
            if page_interval_seconds > 0:
                time.sleep(page_interval_seconds)
        return results[:limit]

    def browse_game_subjects(
        self,
        *,
        year: int | None,
        platform: str | None,
        sort: str = "rank",
        limit: int = 50,
        page_interval_seconds: float = 0.25,
    ) -> list[SubjectSearchCandidate]:
        normalized_platform = platform.strip() if platform else None
        if year is None and not normalized_platform:
            raise ValueError("Bangumi browse requires a year or platform.")
        if not 1 <= limit <= 200:
            raise ValueError("Bangumi browse limit must be between 1 and 200.")
        if sort not in {"date", "rank"}:
            raise ValueError("Bangumi browse sort must be date or rank.")
        results: list[SubjectSearchCandidate] = []
        offset = 0
        while len(results) < limit:
            page_limit = min(50, limit - len(results))
            params: dict[str, Any] = {
                "type": int(SubjectType.GAME),
                "sort": sort,
                "limit": page_limit,
                "offset": offset,
            }
            if year is not None:
                params["year"] = year
            if normalized_platform:
                params["platform"] = normalized_platform
            try:
                page = _SubjectSearchPage.model_validate(
                    self._get_json("/v0/subjects", params=params)
                )
            except ValidationError as exc:
                raise BangumiAPIError(
                    "Bangumi subject browse response did not match the public schema"
                ) from exc
            raw_batch = page.data
            # /v0/subjects has no NSFW query parameter; filter its typed response locally.
            batch = [self._to_search_candidate(item) for item in raw_batch if not item.nsfw]
            results.extend(batch)
            offset += len(raw_batch)
            if not raw_batch or offset >= page.total:
                break
            if page_interval_seconds > 0:
                time.sleep(page_interval_seconds)
        return results[:limit]

    def patch_collection(self, subject_id: int, patch: CollectionPatch) -> None:
        path = f"/v0/users/-/collections/{subject_id}"
        self._request("PATCH", path, json_body=patch.as_api_payload())

    def create_collection(self, subject_id: int, patch: CollectionPatch) -> None:
        path = f"/v0/users/-/collections/{subject_id}"
        self._request("POST", path, json_body=patch.as_api_payload())

    # Phase 1–3 compatibility methods. New code uses the generic names above.
    def get_game_collections(self, *, page_size: int = 50) -> list[RemoteCollection]:
        return self.get_collections(subject_type=SubjectType.GAME, page_size=page_size)

    def get_game_collection(self, subject_id: int) -> RemoteCollection:
        return self.get_collection(subject_id)

    def patch_collection_tags(self, subject_id: int, tags: tuple[str, ...]) -> None:
        self.patch_collection(subject_id, CollectionPatch({"tags": tags}))
