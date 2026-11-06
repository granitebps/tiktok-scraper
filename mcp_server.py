"""Five public TikTok tools. Run with --stdio locally, or HTTP by default."""
import argparse
import json
import logging
import math
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Annotated, Literal

import anyio
import anyio.to_thread
from mcp.server import MCPServer
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

import config
from schema_fields import load_query
from tiktok import cache_config, schemas, search, users, videos
from tiktok.fetch import (TikTokBadRequest, TikTokBlocked, TikTokNotFound,
                          TikTokTimeout, TikTokUpstreamError, deadline)


def _setting(name, default, cast, minimum):
    value = cast(os.environ.get(name, str(default)))
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} must be finite and >= {minimum}")
    return value


HOST = os.environ.get("MCP_HOST", "127.0.0.1")
TOOL_TIMEOUT = _setting("MCP_TOOL_TIMEOUT", 45, float, 0.001)
MAX_CONCURRENCY = _setting("MCP_MAX_CONCURRENCY", 4, int, 1)
CACHE_SIZE = _setting("MCP_CACHE_SIZE", 500, int, 0)
DEFAULT_LIMIT = 5
MAX_LIMIT = 12
log = logging.getLogger("tiktok_mcp")


class TTLCache:
    def __init__(self, size):
        self.size = size
        self._data = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None
            if hit[0] <= time.monotonic():
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return hit[1]

    def set(self, key, value, ttl):
        if self.size <= 0:
            return
        with self._lock:
            self._data[key] = (time.monotonic() + ttl.total_seconds(), value)
            self._data.move_to_end(key)
            while len(self._data) > self.size:
                self._data.popitem(last=False)


_cache = TTLCache(CACHE_SIZE)
_limiter = None


def _get_limiter():
    global _limiter
    if _limiter is None:
        _limiter = anyio.CapacityLimiter(MAX_CONCURRENCY)
    return _limiter


class ToolFailure(Exception):
    def __init__(self, code, message, retryable=False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


def compact(value, top=True, summary=False):
    omit = {"profile_picture", "play_links"}
    if summary:
        omit |= {"qualities", "subtitles", "download_link", "origin_cover", "animated_cover", "cover", "links"}
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in omit:
                continue
            item = compact(item, top=False, summary=summary)
            if item is None or (not top and (item == "" or item == [] or item == {})):
                continue
            out[key] = item
        return out
    if isinstance(value, list):
        return [compact(item, top=False, summary=summary) for item in value]
    return value


def _result(data, is_error=False):
    return CallToolResult(
        content=[TextContent(text=json.dumps(data, ensure_ascii=False, separators=(",", ":")))],
        structured_content=data,
        is_error=is_error,
    )


def _error(code, message, retryable=False):
    return _result({"error": {"code": code, "message": message, "retryable": retryable}}, is_error=True)


def _validate(schema, params):
    data, error = load_query(schema, {k: v for k, v in params.items() if v is not None})
    if error:
        raise ToolFailure("invalid_input", error["error"])
    return data


def _fetch(tool, fn, data, ttl):
    key = (tool, json.dumps(data, sort_keys=True))
    hit = _cache.get(key)
    if hit is not None:
        return hit
    with deadline(TOOL_TIMEOUT):
        value = compact(fn(**data))
    _cache.set(key, value, ttl)
    return value


def _split_cursor(cursor):
    if not cursor or not cursor.startswith("p:"):
        return cursor, 0
    match = re.fullmatch(r"p:(\d{1,3}):(.*)", cursor)
    if not match:
        raise ToolFailure("invalid_input", "Pass next_cursor from the previous answer unchanged")
    return match[2] or None, int(match[1])


def _paged(tool, fn, schema, params, ttl, items_key, count_key, limit):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ToolFailure("invalid_input", f"limit must be between 1 and {MAX_LIMIT}")
    upstream, offset = _split_cursor(params.pop("cursor"))
    data = _validate(schema, {**params, "cursor": upstream})
    page = dict(_fetch(tool, fn, data, ttl))
    all_items = page.get(items_key) or []
    if offset and offset >= len(all_items):
        raise ToolFailure("invalid_input", "This page changed or cursor is invalid; restart without cursor")
    items = all_items[offset:offset + limit]
    end = offset + len(items)
    upstream_next = page.pop("next_cursor", None)
    page["has_more"] = False
    if end < len(all_items):
        page["next_cursor"] = f"p:{end}:{upstream or ''}"
        page["has_more"] = True
    elif upstream_next and upstream_next != upstream:
        page["next_cursor"] = f"p:0:{upstream_next}"
        page["has_more"] = True
    page[items_key] = [compact(item, summary=True) for item in items]
    page[count_key] = len(items)
    return page


async def _run(tool, work):
    try:
        # Include queue time; the copied context also bounds every curl request.
        with deadline(TOOL_TIMEOUT), anyio.fail_after(TOOL_TIMEOUT):
            async with _get_limiter():
                data = await anyio.to_thread.run_sync(work)
        return _result(data)
    except ToolFailure as e:
        return _error(e.code, e.message, e.retryable)
    except ValueError as e:
        return _error("invalid_input", str(e))
    except TikTokNotFound:
        return _error("not_found", "TikTok post or user was not found")
    except TikTokBadRequest:
        return _error("upstream_rejected", "TikTok rejected the request")
    except (TikTokTimeout, TimeoutError):
        return _error("timeout", "Tool deadline expired", retryable=True)
    except TikTokBlocked:
        return _error("upstream_blocked", "TikTok blocked the request; retry later or check the server's egress/proxy", True)
    except TikTokUpstreamError:
        return _error("upstream_unavailable", "TikTok did not return usable data", True)
    except Exception as e:
        log.error("%s failed (%s)", tool, type(e).__name__)
        return _error("internal_error", f"{tool} failed unexpectedly")


READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
PostRef = Annotated[str, Field(min_length=1, max_length=2048, description="TikTok video/photo ID, canonical link or vm/vt/t share link.")]
UserRef = Annotated[str, Field(min_length=1, max_length=2048, description="Username, @username, profile link or secUid. Numeric account IDs cannot be looked up.")]
Cursor = Annotated[str | None, Field(max_length=512, description="Pass next_cursor unchanged; omit for the first page.")]
Limit = Annotated[int, Field(ge=1, le=MAX_LIMIT, description="Maximum items in this response; default 5. Follow next_cursor for more.")]
Country = Annotated[str | None, Field(description="Two-letter TikTok region. Changes egress only with a country-aware TIKTOK_PROXY.")]
MEDIA_NOTE = " Media URLs expire. For video.play_link, send video.request_headers (Referer). CDN alternatives may require cookies or regional egress."

server = MCPServer(
    "tiktok-scraper", version="0.1.0",
    instructions=("Read-only public TikTok data, without login. Lists are bounded and paginated. "
                  "A block or timeout is an error, not an empty result. "
                  "Post detail includes captions, ordered photos or video metadata and download headers."
                  + MEDIA_NOTE),
)


@server.tool(annotations=READ_ONLY, description="Search public TikTok posts by keyword; sort by relevance or likes and optionally filter publication age." + MEDIA_NOTE)
async def search_posts(
    query: Annotated[str, Field(min_length=1, max_length=200, description="Keyword or phrase.")],
    sort: Literal["relevance", "likes"] = "relevance",
    published: Literal["day", "week", "month", "3months", "6months"] | None = None,
    cursor: Cursor = None, limit: Limit = DEFAULT_LIMIT, country: Country = None,
) -> CallToolResult:
    return await _run("search_posts", lambda: _paged(
        "search_posts", search.search_videos, schemas.VideoSearchSchema,
        {"query": query, "sort": sort, "published": published, "cursor": cursor, "country": country},
        cache_config.SEARCH_CACHE, "videos", "video_count", limit))


@server.tool(annotations=READ_ONLY, description="Get one TikTok video or photo post: caption, author, stats, ordered images, video renditions and subtitle links." + MEDIA_NOTE)
async def get_post(post: PostRef, country: Country = None) -> CallToolResult:
    return await _run("get_post", lambda: _fetch(
        "get_post", videos.get_details, _validate(schemas.VideoSchema, {"video": post, "country": country}),
        cache_config.VIDEO_CACHE))


@server.tool(annotations=READ_ONLY, description="Get ranked top-level comments on a TikTok post with authors, likes and reply counts. Nested replies are not included. Follow next_cursor for more.")
async def get_post_comments(post: PostRef, cursor: Cursor = None, limit: Limit = DEFAULT_LIMIT,
                            country: Country = None) -> CallToolResult:
    return await _run("get_post_comments", lambda: _paged(
        "get_post_comments", videos.get_comments, schemas.VideoPageSchema,
        {"video": post, "cursor": cursor, "country": country}, cache_config.COMMENTS_CACHE,
        "comments", "comment_count", limit))


@server.tool(annotations=READ_ONLY, description="Get a public TikTok user's bio, profile link, follower/like/post counts, verification and privacy status.")
async def get_profile(username: UserRef) -> CallToolResult:
    return await _run("get_profile", lambda: _fetch(
        "get_profile", users.get_profile, _validate(schemas.UserSchema, {"user": username}),
        cache_config.PROFILE_CACHE))


@server.tool(annotations=READ_ONLY, description="Get a user's video and photo posts, sorted latest, popular or oldest. Private profiles may return no posts. Follow next_cursor for more." + MEDIA_NOTE)
async def get_profile_posts(username: UserRef, sort: Literal["latest", "popular", "oldest"] = "latest",
                            cursor: Cursor = None, limit: Limit = DEFAULT_LIMIT,
                            country: Country = None) -> CallToolResult:
    return await _run("get_profile_posts", lambda: _paged(
        "get_profile_posts", users.get_posts, schemas.UserPostsSchema,
        {"user": username, "sort": sort, "cursor": cursor, "country": country},
        cache_config.USER_VIDEOS_CACHE, "videos", "video_count", limit))


@server.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def main():
    parser = argparse.ArgumentParser(description="TikTok Scraper MCP server")
    parser.add_argument("--stdio", action="store_true", help="serve stdio instead of HTTP")
    args = parser.parse_args()
    if args.stdio:
        server.run("stdio")
    else:
        server.run("streamable-http", host=HOST, port=config.PORT, stateless_http=True, json_response=True)


if __name__ == "__main__":
    main()
