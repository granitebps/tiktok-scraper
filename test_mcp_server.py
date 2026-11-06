"""MCP contracts against real parsers and captured payloads; no live network."""
import copy
import json
import time
from datetime import timedelta
from pathlib import Path

import anyio
import pytest
from mcp import Client

import mcp_server as mcp
from tiktok import fetch, search, users, videos

FIXTURES = Path(__file__).parent / "tiktok" / "fixtures"
VIDEO_ID = "7692114317151423775"


def load(name):
    return json.loads((FIXTURES / name).read_text())


def call(tool, **params):
    result = anyio.run(lambda: tool(**params))
    assert json.loads(result.content[0].text) == result.structured_content
    return result


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(mcp, "_cache", mcp.TTLCache(100))
    monkeypatch.setattr(mcp, "_limiter", None)
    users._lookups.clear()


def test_post_cache_normalizes_references_and_keeps_media_contract(monkeypatch):
    calls = []
    def api(path, params, **kwargs):
        calls.append(path)
        return load("item_detail.json")
    monkeypatch.setattr(videos, "api", api)
    first = call(mcp.get_post, post=VIDEO_ID)
    second = call(mcp.get_post, post=f"https://www.tiktok.com/@tiktok/video/{VIDEO_ID}?tracking=1")
    assert not first.is_error and first.structured_content == second.structured_content
    data = first.structured_content
    assert data["id"] == VIDEO_ID and isinstance(data["id"], str)
    assert data["video"]["request_headers"] == {"Referer": "https://www.tiktok.com/"}
    assert data["description"] and data["author"]["username"] == "tiktok"
    assert "profile_picture" not in first.content[0].text and "play_links" not in first.content[0].text
    assert calls == ["/api/item/detail/"]


def test_photo_detail_preserves_slide_order(monkeypatch):
    fixture = load("item_detail_photo.json")
    monkeypatch.setattr(videos, "api", lambda *a, **kw: fixture)
    data = call(mcp.get_post, post=fixture["itemInfo"]["itemStruct"]["id"]).structured_content
    expected = [i["imageURL"]["urlList"][0] for i in fixture["itemInfo"]["itemStruct"]["imagePost"]["images"]]
    assert data["type"] == "photo" and [i["link"] for i in data["images"]] == expected
    assert "video" not in data


def test_profile_parses_real_fixture(monkeypatch):
    monkeypatch.setattr(users, "api", lambda *a, **kw: load("user_detail.json"))
    data = call(mcp.get_profile, username="@MrBeast").structured_content
    assert data["username"] == "mrbeast" and data["stats"]["follower_count"] > 0


@pytest.mark.parametrize("tool", ["search_posts", "get_post_comments", "get_profile_posts"])
def test_pagination_does_not_skip_items_and_preserves_upstream_cursor(monkeypatch, tool):
    item = load("item_detail.json")["itemInfo"]["itemStruct"]
    comment = load("comments.json")["comments"][0]
    offsets = []
    def api(path, params, **kwargs):
        if path == "/api/user/detail/":
            return load("user_detail.json")
        offset = int(params.get("offset", params.get("cursor", 0)))
        offsets.append(offset)
        rows = []
        for i in range(offset, min(offset + 5, 7)):
            row = copy.deepcopy(comment if tool == "get_post_comments" else item)
            row["cid" if tool == "get_post_comments" else "id"] = str(int(VIDEO_ID) + i)
            rows.append(row)
        if tool == "search_posts":
            return {"status_code": 0, "item_list": rows, "has_more": offset == 0,
                    "cursor": 5, "log_pb": {"impr_id": "20261106SEARCHID"}}
        return {"statusCode": 0, "comments" if tool == "get_post_comments" else "itemList": rows,
                "has_more" if tool == "get_post_comments" else "hasMore": offset == 0,
                "cursor": 5, "total": 7}
    for module in (search, videos, users):
        monkeypatch.setattr(module, "api", api)
    params = {"query": "food"} if tool == "search_posts" else {"post": VIDEO_ID} if tool == "get_post_comments" else {"username": "tiktok"}
    key = "comments" if tool == "get_post_comments" else "videos"
    seen, cursor = [], None
    for _ in range(5):
        result = call(getattr(mcp, tool), **params, limit=2, cursor=cursor)
        assert not result.is_error
        page = result.structured_content
        assert len(page[key]) <= 2
        seen += [row["id"] for row in page[key]]
        if not page["has_more"]:
            assert "next_cursor" not in page
            break
        cursor = page["next_cursor"]
    assert seen == [str(int(VIDEO_ID) + i) for i in range(7)]
    assert offsets == [0, 5]


@pytest.mark.parametrize("cursor", ["p:x:", "p:-1:", "p:1:garbage", "p:999:"])
def test_invalid_cursor_is_an_error(monkeypatch, cursor):
    monkeypatch.setattr(videos, "api", lambda *a, **kw: load("comments.json"))
    result = call(mcp.get_post_comments, post=VIDEO_ID, cursor=cursor)
    assert result.is_error and result.structured_content["error"]["code"] == "invalid_input"


@pytest.mark.parametrize("error,code,retryable", [
    (fetch.TikTokNotFound("private"), "not_found", False),
    (fetch.TikTokBadRequest("private"), "upstream_rejected", False),
    (fetch.TikTokBlocked("private"), "upstream_blocked", True),
    (fetch.TikTokTimeout("private"), "timeout", True),
    (fetch.TikTokUpstreamError("private"), "upstream_unavailable", True),
    (RuntimeError("private"), "internal_error", False),
])
def test_errors_are_structured_sanitized_and_not_cached(monkeypatch, error, code, retryable):
    attempts = []
    def fail(**kwargs):
        attempts.append(True)
        raise error
    monkeypatch.setattr(videos, "get_details", fail)
    for _ in range(2):
        result = call(mcp.get_post, post=VIDEO_ID)
        assert result.is_error
        assert result.structured_content["error"]["code"] == code
        assert result.structured_content["error"]["retryable"] is retryable
        assert "private" not in result.content[0].text
    assert len(attempts) == 2


def test_cache_ttl_lru_and_disabled(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(mcp.time, "monotonic", lambda: now[0])
    cache = mcp.TTLCache(2)
    cache.set("a", {"value": 1}, timedelta(seconds=2))
    cache.set("b", {}, timedelta(seconds=2))
    assert cache.get("a") == {"value": 1}
    cache.set("c", {}, timedelta(seconds=2))
    assert cache.get("b") is None
    now[0] += 2
    assert cache.get("a") is None
    disabled = mcp.TTLCache(0)
    disabled.set("a", {}, timedelta(seconds=2))
    assert disabled.get("a") is None


def test_queue_is_bounded_and_timeout_does_not_cache_late_results(monkeypatch):
    monkeypatch.setattr(mcp, "TOOL_TIMEOUT", 0.02)
    monkeypatch.setattr(mcp, "MAX_CONCURRENCY", 1)
    calls = []
    def slow(**kwargs):
        calls.append(True)
        time.sleep(0.05)
        return {"id": VIDEO_ID}
    monkeypatch.setattr(videos, "get_details", slow)
    async def run():
        results = []
        async def one():
            results.append(await mcp.get_post(VIDEO_ID))
        async with anyio.create_task_group() as group:
            group.start_soon(one)
            group.start_soon(one)
        return results
    results = anyio.run(run)
    assert len(calls) == 1
    assert all(r.structured_content["error"]["code"] == "timeout" for r in results)
    assert not mcp._cache._data


def test_mcp_discovery_validation_and_all_five_tools(monkeypatch):
    def api(path, params, **kwargs):
        return load({"/api/item/detail/": "item_detail.json", "/api/comment/list/": "comments.json",
                     "/api/user/detail/": "user_detail.json", "/api/post/item_list/": "items_rich.json",
                     "/api/search/item/full/": "search_item.json"}[path])
    for module in (users, videos, search):
        monkeypatch.setattr(module, "api", api)
    async def run():
        async with Client(mcp.server) as client:
            tools = (await client.list_tools()).tools
            assert {t.name for t in tools} == {"search_posts", "get_post", "get_post_comments", "get_profile", "get_profile_posts"}
            assert all(t.annotations.read_only_hint and not t.annotations.destructive_hint for t in tools)
            for name, args in [("search_posts", {"query": "food"}), ("get_post", {"post": VIDEO_ID}),
                               ("get_post_comments", {"post": VIDEO_ID}), ("get_profile", {"username": "tiktok"}),
                               ("get_profile_posts", {"username": "tiktok"})]:
                result = await client.call_tool(name, args)
                assert not result.is_error, result
                assert json.loads(result.content[0].text) == result.structured_content
            for name, args in [("get_post", {"post": "https://example.com/x"}),
                               ("search_posts", {"query": " "}), ("get_profile_posts", {"username": "tiktok", "limit": 0})]:
                assert (await client.call_tool(name, args)).is_error
    anyio.run(run)
