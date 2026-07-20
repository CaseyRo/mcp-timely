# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Dependencies are managed with `uv`. Python >= 3.12.

```bash
uv sync                                   # install deps (incl. dev group)
uv run pytest                             # full suite (in-memory MCP client, no network)
uv run pytest tests/test_session.py -k rotated   # single test by name
uv run ruff check .                       # lint
uv run ruff format .                      # format

uv run mcp-timely auth                    # one-time OAuth bootstrap (prints URL, takes OOB code)
uv run mcp-timely                         # run stdio server (default)
TRANSPORT=http MCP_API_KEY=dev uv run mcp-timely   # HTTP server on :8000 (/mcp, /health)

docker compose up                         # HTTP server on :8011 (maps to container :8000)
```

## Git & release conventions

Release is fully automated: any push to `main` that changes non-doc files triggers `.github/workflows/release.yml`, which runs tests + pip-audit, bumps the patch in `pyproject.toml`, appends to `CHANGELOG.md`, tags `vX.Y.Z`, builds/pushes a multi-arch image to `ghcr.io/caseyro/mcp-timely`, then triggers the Komodo stack redeploy via signed webhook.

- **Never hand-edit `version` in `pyproject.toml`** — the CI owns it.
- Include `[skip ci]` in a commit message to skip the release.
- Pure-markdown and `tests/**` changes don't trigger a release (`paths-ignore`).
- Rebase feature branches before merging to main, or the auto-bump commit races yours.
- A green run is not a release: confirm the `v*` tag appeared.
- The Komodo trigger step is skipped when the `KOMODO_WEBHOOK_SECRET` repo secret is unset; when set, it must match the stack's webhook secret exactly (Komodo 401s silently on mismatch).
- Dependabot runs weekly (uv, docker, actions ecosystems) with auto-merge; each merged PR ships a release.

## Architecture

FastMCP 3.x server (src layout, `src/mcp_timely/`) exposing three question-shaped, read-only tools over Timely's API. No write path, no user/team parameters: everything is scoped to the authorized user (account + user id resolved lazily at first call and cached).

**Tools** (`server.py`) — each is 1–2 upstream calls; Timely does the aggregation:
- `projects_overview` — `GET /1.1/{acc}/projects`; hours, budget burn %, unbilled figures
- `time_spent` — project/client grouping via `GET /reports` (client rollups with nested projects), day/label via `POST /reports/filter` (group keys are plural: `days`, `labels`; unknown keys return totals with silently empty group arrays). Day buckets follow the account's timezone.
- `work_log` — user-scoped `GET /users/{id}/events`; entries with notes, billable/billed, timer state

All tools return pydantic envelopes with a human-readable `summary` and raise `ToolError` on failure. Durations/money from Timely are objects (`{total_hours, formatted}` / `{amount, formatted, currency_code}`) — the `_hours`/`_formatted`/`_money` helpers parse defensively.

**OAuth session** (`session.py`) — the load-bearing part. Spike-verified Timely behavior: access tokens carry no `expires_in`; **refresh tokens rotate on every refresh**. Strategy: use the access token until a 401, refresh once (atomic token-file write via tmp + `os.replace`, guarded by an asyncio lock), retry once. A failed refresh raises `ToolError` naming `mcp-timely auth`. Token values never appear in errors or logs.

**The token file is a singleton session.** `TIMELY_TOKEN_FILE` (default `timely_tokens.json`, `/data/timely_tokens.json` in Docker) holds the only valid refresh token. Running `mcp-timely auth` while a deployed instance is live rotates the family and strands the deployed pair — re-bootstrap and re-place the file if that happens.

**Config** (`config.py`): pydantic-settings from env. `TRANSPORT=http` refuses to start without `MCP_API_KEY` (bearer auth, `auth.py`, timing-safe compare).

## Deployment (CDIT-specific)

Komodo stack `git-mcp-timely-nebula` on `nebula-1` (see `komodo.toml`), container port 8000 → host 8011, image `ghcr.io/caseyro/mcp-timely:latest`. Env vars map Komodo variables `TIMELY_CLIENT_ID`, `TIMELY_CLIENT_SECRET`, `MCP_TIMELY_API_KEY`; the token file lives on the `timely-data` volume. Exposed as `mcp-timely.cdit-dev.de` via the cdit-ingress tunnel and registered in the Cloudflare MCP Portal as "Timely" (portal registration is dashboard-only; API tokens lack the AI-Controls scope). Planning lives in the `cdit` OpenSpec store (`openspec/config.yaml` references it); the shipped change is archived there as `2026-07-20-add-timely-mcp`.

## Conventions

- Tests use FastMCP's in-memory `Client(mcp)`; upstream is mocked at the session (`AsyncMock`) or via `httpx.MockTransport`. Keep the read-only surface test (`test_tool_surface_is_exactly_three_read_only_tools`) green — it is the spec's guarantee.
- Public repo: no CDIT internals in README, no secrets anywhere; `.env` and the token file are gitignored.
- New tools must stay question-shaped: if answering requires the caller to loop or sum, push the aggregation upstream or reshape the tool.
