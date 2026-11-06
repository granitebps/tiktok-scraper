# TikTok MCP tasks

Source: User-approved recommendation and implementation request, 2026-11-06.
Scope: This repository only. Five public read-only tools for local stdio and
homeserver Streamable HTTP. Fami integration is a separate user workstream.

## Execution guard

Stop and report if the repository contradicts the accepted scope or completion
requires a materially different architecture. Preserve existing user changes.
Do not deploy, commit, push, or modify household-ai. Never present mocked or
offline evidence as proof that TikTok works live.

- [x] T001 — Qualify and correct transport behavior
  - Depends on: None. Parallel: No.
  - Scope: TikTok fetch/search/media functions and focused regression tests.
  - Implementation: Diagnose search initialization and media access; apply
    evidence-backed fixes at existing seams. Add an overall deadline covering
    retries and multi-request operations, following the Instagram implementation.
  - Verify: Live search, detail, comments and user reads; independently usable
    video/photo URLs for Fami. Regression checks for changed transport behavior.
  - Evidence: 85 offline checks passed. Search returned 20 items after switching
    bootstrap to /search; a photo post returned five ordered photos and an image
    range request returned 206 image/jpeg. Video playback with Referer and Fami's
    User-Agent returned 206 video/mp4, 1024 bytes. Earlier live detail/comments/
    profile/user-post reads succeeded. Final MCP qualification remains T003.
- [x] T002 — Implement the five-tool MCP
  - Depends on: T001. Parallel: No.
  - Scope: mcp_server.py, requirements files, relevant tests.
  - Implementation: Follow Instagram's tools, schema reuse, structured errors,
    compact output, TTL cache, thread limiter and lossless sliced pagination.
    Keep detail media and ordered photos needed by Fami. No extra MCP tools.
  - Verify: All five tools through an MCP client; invalid inputs, error mapping,
    pagination without skipped items, cache behavior and overall deadlines.
  - Evidence: 104 offline checks passed including an in-process MCP client for
    all five tools and queue/deadline, cache, normalization and pagination cases.
    Live stdio MCP smoke passed all tools, 52 search results / 58 comments /
    59 user posts across five response pages each without duplicate sample IDs;
    video range returned 206 MP4 and all five photo ranges were readable.
- [x] T003 — Package and verify local/server usage
  - Depends on: T002. Parallel: No.
  - Scope: Dockerfile, docker-compose.yaml, README.md and reusable smoke checks.
  - Implementation: Local stdio and HTTP documentation; private ai-routing
    service, non-root/read-only container and healthcheck; retain REST entrypoint.
  - Verify: Full offline suite, real stdio and HTTP MCP handshakes/calls, live
    five-tool and media checks. Validate Compose/build when tooling is available.
    Report any environmental blocker rather than claiming untested success.
  - Evidence: Fresh repo-local Python 3.12 environment installed successfully;
    pip check passed. 104 offline tests and git diff --check passed. Compose
    config validated. Live HTTP health, discovery, all five tools and invalid
    input passed; five pages returned 52 search results, 56 comments and 59 user
    posts without duplicate sample IDs. Video range returned 206 video/mp4;
    all five photo slide ranges succeeded. The temporary HTTP server was stopped.
    Live stdio also passed search likes/month with country=id, and popular/oldest
    profile post sorts. All 41 REST routes still import successfully.

## Verification limits

- Docker image build and local Linux ARM64 non-root/read-only runtime were
  verified in the follow-up below. Other architectures and sustained load
  have not been qualified.
- No homeserver or household-ai changes. The host's egress must be qualified
  independently with scripts/smoke_mcp.py after deployment.
- Media probes read 1024-byte ranges, not full files or decoded/model content.
  They used no cookies or proxy; video required the returned Referer header.
- Live samples are point-in-time evidence, not a guarantee for every TikTok
  post, IP, region, deleted/private item or future signer version.

## Docker follow-up — 2026-11-06

User requested live qualification after starting Docker. No code fixes needed.

- `docker compose build tiktok-scraper`: PASS on linux/aarch64; image
  `sha256:f32d5483a743b5d792dc436dd93611517ba7a609afd410f9c6f4e7e9819d7bd4`.
- Compose runtime: PASS with user nobody, read-only root filesystem, no published
  ports, ai-routing network, 256 MiB / 0.5 CPU / 64 PID limits, dropped
  capabilities and no-new-privileges. Docker healthcheck became healthy.
- A separate read-only client container reached the server over ai-routing and
  passed discovery and all five tools using real TikTok responses. Five response
  pages returned 52 search results, 58 comments and 59 user posts without
  duplicate sample IDs, including crossing upstream page boundaries.
- Video detail/creator/caption, user detail, all five photo slides, invalid input,
  search likes/month with country=id, popular/oldest user posts, canonical post
  URL with tracking query, and profile URL input: PASS.
- Independent media byte-range reads from the client container: video 206 MP4
  with the returned Referer, all five photo slides readable.
- Server remained healthy, OOMKilled=false, RestartCount=0. One post-test memory
  sample was 69.24 MiB of 256 MiB; this is not peak or sustained-load evidence.
- Local stdio smoke rerun: PASS for all five tools and media; five pages returned
  52 search results, 57 comments and 59 user posts. Offline suite: 104 passed.
  pip check and Compose validation: PASS.
- Qualification server stopped; client exited successfully. Both stopped
  containers and the built image are retained. No homeserver deployment,
  household-ai changes, commit or push.

## Initial evidence

- Existing parser/schema suite: 79 passed.
- Direct local reads: detail, comments, profile and user posts returned data.
- Search: TikTokBlocked while initializing /@tiktok (missing page data).
- Anonymous range request to sampled video.play_link: HTTP 403.
- No proxy configured during those checks. Homeserver access is not authorized.
