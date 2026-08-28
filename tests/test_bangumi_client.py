from __future__ import annotations

import json

import httpx
from pydantic import SecretStr

from bangumi_local.adapters.bangumi import BangumiAPIError, BangumiClient
from bangumi_local.config import Settings
from bangumi_local.domain.models import SubjectType
from bangumi_local.domain.mutations import CollectionPatch


def _settings(secret: str) -> Settings:
    return Settings(
        _env_file=None,
        bangumi_access_token=SecretStr(secret),
        bangumi_username="tester",
        bangumi_user_agent="tester/bgm-game-vault/0.1 (tests)",
        bangumi_base_url="https://api.bgm.tv",
    )


def _collection(subject_id: int, subject_type: int = 4) -> dict[str, object]:
    return {
        "subject_id": subject_id,
        "subject_type": subject_type,
        "rate": 8,
        "type": 2,
        "comment": "good",
        "tags": ["RPG"],
        "updated_at": "2025-01-02T03:04:05+08:00",
        "private": False,
        "subject": {
            "id": subject_id,
            "type": subject_type,
            "name": f"Game {subject_id}",
            "name_cn": "",
            "short_summary": "summary",
            "date": "2024-01-01",
            "images": {"common": "https://lain.bgm.tv/example.jpg"},
            "tags": [{"name": "Galgame", "count": 20}],
        },
    }


def test_auth_headers_and_pagination() -> None:
    secret = "".join(("runtime", "-", "credential"))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v0/me":
            return httpx.Response(
                200, json={"id": 7, "username": "tester", "nickname": "Test User"}
            )
        offset = int(request.url.params["offset"])
        data = [_collection(101)] if offset == 0 else [_collection(102)]
        return httpx.Response(
            200, json={"total": 2, "limit": 1, "offset": offset, "data": data}
        )

    with BangumiClient(_settings(secret), transport=httpx.MockTransport(handler)) as client:
        assert client.get_me().username == "tester"
        collections = client.get_game_collections(page_size=1)

    assert [item.subject_id for item in collections] == [101, 102]
    assert all(item.game.public_tags == ("Galgame",) for item in collections)
    assert len(requests) == 3
    assert all(request.method == "GET" for request in requests)
    assert all(request.headers["authorization"] == f"Bearer {secret}" for request in requests)
    assert all(
        request.headers["user-agent"] == "tester/bgm-game-vault/0.1 (tests)"
        for request in requests
    )
    collection_requests = requests[1:]
    assert all(request.url.params["subject_type"] == "4" for request in collection_requests)


def test_api_error_does_not_expose_secret() -> None:
    secret = "".join(("do-not", "-", "render"))
    transport = httpx.MockTransport(lambda _request: httpx.Response(401, json={"error": "no"}))

    with BangumiClient(_settings(secret), transport=transport) as client:
        try:
            client.get_me()
        except BangumiAPIError as exc:
            assert secret not in str(exc)
            assert "401" in str(exc)
        else:
            raise AssertionError("expected BangumiAPIError")


def test_default_collections_omits_type_and_accepts_all_official_types() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        data = [_collection(100 + value, value) for value in (1, 2, 3, 4, 6)]
        return httpx.Response(
            200, json={"total": len(data), "limit": 50, "offset": 0, "data": data}
        )

    with BangumiClient(
        _settings("runtime-credential"), transport=httpx.MockTransport(handler)
    ) as client:
        collections = client.get_collections()

    assert [item.subject_type for item in collections] == list(SubjectType)
    assert "subject_type" not in requests[0].url.params


def test_single_collection_subject_tags_and_patch() -> None:
    state = {"tags": ["RPG"]}
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/collections/101"):
            payload = _collection(101)
            payload["tags"] = list(state["tags"])
            return httpx.Response(200, json=payload)
        if request.method == "GET" and request.url.path == "/v0/subjects/101":
            return httpx.Response(
                200,
                json={"id": 101, "tags": [{"name": "Galgame", "count": 50}]},
            )
        if request.method == "PATCH":
            state["tags"] = request.read().decode("utf-8")
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with BangumiClient(
        _settings("".join(("runtime", "-", "write", "-", "credential"))),
        transport=httpx.MockTransport(handler),
    ) as client:
        assert client.get_game_collection(101).tags == ("RPG",)
        assert client.get_subject_public_tags(101) == ("Galgame",)
        client.patch_collection_tags(101, ("RPG", "Galgame"))

    assert [request.method for request in requests] == ["GET", "GET", "PATCH"]
    assert requests[-1].url.path == "/v0/users/-/collections/101"
    assert requests[-1].read() == b'{"tags":["RPG","Galgame"]}'


def test_game_search_and_collection_create_use_documented_v0_routes() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v0/search/subjects":
            return httpx.Response(
                200,
                json={
                    "total": 1,
                    "limit": 10,
                    "offset": 0,
                    "data": [
                        {
                            "id": 321,
                            "type": 4,
                            "name": "Original title",
                            "name_cn": "中文标题",
                            "date": "2024-03-01",
                            "tags": [{"name": "Steam", "count": 3}],
                            "infobox": [
                                {
                                    "key": "别名",
                                    "value": [{"v": "English title"}, "日本語タイトル"],
                                }
                            ],
                        }
                    ],
                },
            )
        if request.method == "POST":
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with BangumiClient(
        _settings("runtime-credential"), transport=httpx.MockTransport(handler)
    ) as client:
        candidates = client.search_subjects("English title")
        client.create_collection(321, CollectionPatch({"type": 2}))

    assert candidates[0].subject_id == 321
    assert candidates[0].aliases == ("English title", "日本語タイトル")
    search = requests[0]
    assert search.method == "POST"
    assert search.url.params["limit"] == "10"
    assert search.read() == (
        b'{"keyword":"English title","sort":"match","filter":{"type":[4],"nsfw":false}}'
    )
    assert requests[1].method == "POST"
    assert requests[1].url.path == "/v0/users/-/collections/321"
    assert requests[1].read() == b'{"type":2}'
    assert len(requests) == 2


def test_bounded_discovery_search_and_browse_filters_and_rating_snapshot() -> None:
    requests: list[httpx.Request] = []

    def subject(subject_id: int) -> dict[str, object]:
        return {
            "id": subject_id,
            "type": 4,
            "name": f"Game {subject_id}",
            "name_cn": "",
            "date": "2022-06-01",
            "rating": {"rank": 42, "total": 321, "score": 8.4},
        }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        data = [subject(401)]
        if request.method == "GET":
            hidden = subject(499)
            hidden["nsfw"] = True
            data = [hidden, subject(402)]
        return httpx.Response(
            200,
            json={
                "total": len(data),
                "limit": int(request.url.params["limit"]),
                "offset": int(request.url.params["offset"]),
                "data": data,
            },
        )

    with BangumiClient(
        _settings("runtime-credential"), transport=httpx.MockTransport(handler)
    ) as client:
        search = client.search_subjects_filtered(
            "bounded query", subject_type=SubjectType.GAME, limit=5,
            sort="score", meta_tags=("RPG",), year_from=2020,
            year_to=2022, min_rating_count=100, include_nsfw=False,
        )
        browse = client.browse_game_subjects(
            year=2022, platform="PC", sort="rank", limit=5
        )

    assert (search[0].rank, search[0].score, search[0].rating_count) == (42, 8.4, 321)
    search_body = json.loads(requests[0].read())
    assert search_body == {
        "keyword": "bounded query",
        "sort": "score",
        "filter": {
            "type": [4],
            "nsfw": False,
            "meta_tags": ["RPG"],
            "air_date": [">=2020-01-01", "<2023-01-01"],
            "rating_count": [">=100"],
        },
    }
    assert requests[1].method == "GET"
    assert requests[1].url.params["type"] == "4"
    assert requests[1].url.params["year"] == "2022"
    assert requests[1].url.params["platform"] == "PC"
    assert [item.subject_id for item in browse] == [402]
