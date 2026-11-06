# TikTok Scraper MCP

Five read-only MCP tools for public TikTok posts and profiles, built on
[omkarcloud/tiktok-scraper](https://github.com/omkarcloud/tiktok-scraper).
The server talks directly to TikTok using browser-impersonated HTTP and the
bundled request signer. No Apify or RapidAPI key, account, or browser is required.
TikTok can still block a particular IP or region; direct access is not guaranteed.

## Tools

| Tool | Inputs | Result |
|---|---|---|
| `search_posts` | `query`, `sort`, `published`, `cursor`, `limit`, `country` | Matching posts in `videos`, including video and photo post records when returned by TikTok. |
| `get_post` | `post`, `country` | Full caption, creator, stats, video/media metadata, ordered photo slides, subtitle links and canonical link. |
| `get_post_comments` | `post`, `cursor`, `limit`, `country` | Ranked top-level `comments`, with authors, likes and reply counts. Nested replies are not included. |
| `get_profile` | `username` | Bio, links, counters, verification, privacy status and profile metadata. |
| `get_profile_posts` | `username`, `sort`, `cursor`, `limit`, `country` | The user's posts in `videos`, including photo posts. |

- `post` accepts a numeric video/photo ID, a canonical TikTok link, or a
  supported share link (`vm.tiktok.com`, `vt.tiktok.com`, `tiktok.com/t/...`).
- `username` accepts a username, `@username`, profile link or `secUid`. A numeric
  string is treated as a username; numeric account ID lookup is not supported.
- Search `sort`: `relevance` (default) or `likes`. Optional `published`: `day`,
  `week`, `month`, `3months`, `6months`.
- Profile post `sort`: `latest` (default), `popular`, `oldest`.
- Lists default to five items, with `limit` from 1 to 12. While `has_more` is
  true, pass `next_cursor` unchanged as `cursor`, keeping the other inputs the
  same. A cursor includes the offset within TikTok's page so small responses
  do not discard the remaining items. Paging reflects live results; pages can
  change after cache expiry or a server restart. Restart a changed page if
  the server reports an invalid cursor.
- IDs remain strings to preserve TikTok's 19–20 digit values.
- Responses contain structured JSON and matching compact JSON text. Null/empty
  nested fields, avatar URLs and duplicate playback URL lists are omitted.
  List results also omit alternate renditions, covers and subtitle tracks;
  use `get_post` for full media detail. Zero counts and false flags are kept.

## Media contract for Fami and other clients

`get_post` keeps the creator, caption, canonical `link`, and media needed for
reference capture. `type: "photo"` returns **all** slides in `images`, in the
original order. `type: "video"` returns `video.play_link`, duration, available
qualities, subtitle links and the headers required for playback:

```json
{
  "video": {
    "play_link": "https://www.tiktok.com/aweme/v1/play/?...",
    "request_headers": { "Referer": "https://www.tiktok.com/" }
  }
}
```

Download `video.play_link` with `video.request_headers`. The direct-egress
qualification returned HTTP 403 without this Referer and HTTP 206 MP4 bytes
with it, including with Fami's downloader User-Agent. Prefer `play_link` over
`download_link`: CDN alternatives can require TikTok session cookies. Photo
`images[].link` URLs were independently downloadable without cookies.

Media URLs are signed and expire. Download promptly, inspect the HTTP status
and content type, and preserve slide order. Video accessibility also depends
on regional egress; returning metadata does not guarantee every client IP can
fetch every video. The MCP returns URLs and public headers, never cookies or
proxy credentials. It does not download full media files, transcribe speech,
or analyze the video/images. Those remain client responsibilities.

The household-ai integration is separate: it must map this response and apply
these headers in its downloader. This repository does not change Fami's
provider selection or replace its Apify capture path automatically.

## Run locally

Requires Python 3.12.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Use absolute paths in your MCP client's configuration:

```json
{
  "mcpServers": {
    "tiktok": {
      "command": "/absolute/path/to/tiktok-scraper/.venv/bin/python",
      "args": ["/absolute/path/to/tiktok-scraper/mcp_server.py", "--stdio"]
    }
  }
}
```

For a local HTTP client:

```bash
.venv/bin/python mcp_server.py
```

This serves Streamable HTTP at `http://127.0.0.1:8000/mcp` and process health
at `/health`. Stdio does not bind a port. Logging stays on stderr.

## Homeserver deployment

The Compose service follows the Threads/Instagram deployment pattern: private
external `ai-routing` network, no published ports, non-root user, read-only
filesystem, temporary `/tmp`, dropped capabilities and bounded resources.
The initial 256 MiB / 0.5 CPU limits require measurement on the actual host.
The image retains Debian slim for curl_cffi's native libraries.

```bash
docker network inspect ai-routing >/dev/null 2>&1 || docker network create ai-routing
docker compose up -d --build
```

A client on `ai-routing` connects to `http://tiktok-scraper:8000/mcp` using
`streamable-http`. Configure its tool filter to include only the five tools
listed above. The HTTP server has no application authentication: keep it on
this private network, or provide an authenticated gateway if exposing it.

`GET /health` checks process health, not TikTok reachability. The live smoke
check below qualifies the upstream paths. Check on the homeserver as well as
locally because the two connections can receive different TikTok responses.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MCP_HOST` | `127.0.0.1` (`0.0.0.0` in image) | HTTP bind address |
| `PORT` | `8000` | HTTP/REST port |
| `MCP_TOOL_TIMEOUT` | `45` | Overall seconds per tool, including queue time and upstream retries |
| `MCP_MAX_CONCURRENCY` | `4` | Simultaneous tool calls |
| `MCP_CACHE_SIZE` | `500` | In-memory LRU result entries; `0` disables caching |
| `TIKTOK_PROXY` | unset | Optional proxy URL; `{country}` is replaced with the selected country |

Cache TTLs are in `tiktok/cache_config.py`: profile 30 minutes; search, post,
comments and profile posts 10 minutes. A validated input and upstream cursor
identify a cache entry. Upstream failures and results fetched after the deadline
are not cached. Restart to apply configuration changes or clear caches.

`country` changes TikTok's requested region. It changes the physical egress
only when a configured proxy supports country selection. A new HTTP session
also does not rotate an IP. Search starts at `/search` to obtain cookies;
profile bootstrap pages can be challenged independently. TikTok's signer and
endpoints can change, and blocked results are reported as errors.

Tool failures use `isError: true` and:

```json
{"error":{"code":"upstream_blocked","message":"...","retryable":true}}
```

Codes: `invalid_input`, `not_found`, `upstream_rejected`, `upstream_blocked`,
`timeout`, `upstream_unavailable`, `internal_error`. Protocol/schema validation
errors are also MCP errors and may use the SDK's validation text. A retryable
error is a hint to the client; the tool does not automatically launch another
call or paid provider. Existing bounded upstream attempts share one deadline.

## Verification

Offline tests exercise the actual parsers against captured payloads, plus MCP
tool contracts, input normalization, errors, cache eviction, pagination,
concurrency and deadlines:

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Live checks use the actual MCP protocol and TikTok. They print small receipts
and save no media files or signed URLs. Samples can be changed if deleted or
restricted. A failed check exits nonzero.

```bash
# Launch this repo as an MCP subprocess and check all five tools plus media
.venv/bin/python scripts/smoke_mcp.py

# Cross upstream page boundaries for search, comments and profile posts
.venv/bin/python scripts/smoke_mcp.py --limit 12 --pages 5

# Qualify a running HTTP server
.venv/bin/python scripts/smoke_mcp.py --url http://127.0.0.1:8000/mcp
```

Video/photo download checks request only a small byte range. They prove
initial accessibility and content type, not a complete download, decoding,
transcription or media-model processing. Homeserver runtime qualification
and the eventual Fami integration are separate checks.

## REST and direct Python access

The original 41-endpoint REST API remains available with `python run.py`.
It is not the image's default process. `main.py` shows direct Python usage;
import the scraper modules directly for individual operations. The MCP wraps
only the five selected capabilities and reuses their validation and parsers.

## License

See [LICENSE](LICENSE) and the attribution in `tiktok/signer.py` for the
bundled signer. Upstream project: [omkarcloud/tiktok-scraper](https://github.com/omkarcloud/tiktok-scraper).
