"""Live qualification over stdio (default) or --url http://host:port/mcp.

Only small JSON receipts are printed. No media files or signed URLs are saved.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import urllib.request

import anyio
from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
TOOLS = {"search_posts", "get_post", "get_post_comments", "get_profile", "get_profile_posts"}


def media_probe(url, headers, kind):
    request = urllib.request.Request(url, headers={"User-Agent": "fami-reference-capture/1",
                                                  "Range": "bytes=0-1023", **headers})
    with urllib.request.urlopen(request, timeout=15) as response:
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
        chunk = response.read(1024)
        if not 200 <= response.status < 300 or not content_type.startswith(kind + "/") or not chunk:
            raise RuntimeError("media response was not usable")
        return {"http_status": response.status, "content_type": content_type, "bytes_read": len(chunk)}


async def qualify(args):
    target = args.url or StdioServerParameters(
        command=sys.executable, args=[str(ROOT / "mcp_server.py"), "--stdio"], cwd=str(ROOT),
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    failures = []

    async with Client(target, read_timeout_seconds=60) as client:
        tools = (await client.list_tools()).tools
        assert {tool.name for tool in tools} == TOOLS
        assert all(tool.annotations.read_only_hint for tool in tools)
        print(json.dumps({"check": "discovery", "status": "pass", "tools": sorted(TOOLS)}), flush=True)

        async def invoke(name, params):
            result = await client.call_tool(name, params)
            data = result.structured_content
            if result.is_error:
                raise RuntimeError((data or {}).get("error", {}).get("code", "tool_error"))
            assert isinstance(data, dict) and data
            assert json.loads(result.content[0].text) == data
            return data

        cases = [
            ("search_posts", {"query": args.query, "limit": args.limit}, "videos"),
            ("get_post", {"post": args.post}, None),
            ("get_post_comments", {"post": args.post, "limit": args.limit}, "comments"),
            ("get_profile", {"username": args.username}, None),
            ("get_profile_posts", {"username": args.username, "limit": args.limit}, "videos"),
        ]
        for name, params, key in cases:
            started = time.monotonic()
            try:
                data = await invoke(name, params)
                count, pages = 0, 1
                if key:
                    assert data[key], "sample returned no items"
                    ids = [row["id"] for row in data[key]]
                    count += len(ids)
                    assert count <= args.limit
                    for _ in range(args.pages - 1):
                        if not data["has_more"]:
                            break
                        data = await invoke(name, {**params, "cursor": data["next_cursor"]})
                        next_ids = [row["id"] for row in data[key]]
                        assert len(next_ids) <= args.limit
                        assert not set(ids).intersection(next_ids), "overlapping sample pages"
                        ids += next_ids
                        count += len(next_ids)
                        pages += 1
                elif name == "get_post":
                    assert data["id"] == args.post and data["author"]["username"]
                    assert data["description"] and data["type"] == "video"
                    probe = await anyio.to_thread.run_sync(
                        lambda: media_probe(data["video"]["play_link"], data["video"]["request_headers"], "video"))
                    print(json.dumps({"check": "video_download", "status": "pass", **probe}), flush=True)
                else:
                    assert data["username"].lower() == args.username.lstrip("@").lower()
                    assert data["stats"]["follower_count"] > 0
                print(json.dumps({"check": name, "status": "pass", "items": count, "pages": pages,
                                  "elapsed_ms": round((time.monotonic() - started) * 1000)}), flush=True)
            except Exception as error:
                code = str(error) if isinstance(error, RuntimeError) else type(error).__name__
                failures.append(name)
                print(json.dumps({"check": name, "status": "fail", "error": code}), flush=True)

        try:
            photo = await invoke("get_post", {"post": args.photo})
            assert photo["id"] == args.photo and photo["type"] == "photo" and len(photo["images"]) > 1
            for image in photo["images"]:
                await anyio.to_thread.run_sync(lambda: media_probe(image["link"], {}, "image"))
            print(json.dumps({"check": "photo_downloads", "status": "pass", "slides": len(photo["images"])}), flush=True)
        except Exception as error:
            failures.append("photo_downloads")
            print(json.dumps({"check": "photo_downloads", "status": "fail", "error": type(error).__name__}), flush=True)

        invalid = await client.call_tool("get_post", {"post": "https://example.com/not-tiktok"})
        assert invalid.is_error
        print(json.dumps({"check": "invalid_input", "status": "pass"}), flush=True)
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Existing HTTP MCP server; omit to launch this repo over stdio")
    parser.add_argument("--query", default="nasi goreng")
    parser.add_argument("--username", default="tiktok")
    parser.add_argument("--post", default="7692114317151423775", help="Numeric public video ID")
    parser.add_argument("--photo", default="7692968080456092958", help="Numeric public photo post ID")
    parser.add_argument("--pages", type=int, choices=range(1, 6), default=2)
    parser.add_argument("--limit", type=int, choices=range(1, 13), default=5)
    args = parser.parse_args()
    failures = anyio.run(qualify, args)
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
