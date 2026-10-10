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

Releases are tag-only: any push to `main` that changes non-doc files triggers `.github/workflows/release.yml`, which runs tests + pip-audit, pushes the next `vX.Y.Z` tag (nothing is committed to the protected branch), builds/pushes a multi-arch image to `ghcr.io/caseyro/mcp-timely` with the tag baked in as `APP_VERSION` (what `/health` reports), then calls a signed deploy webhook so the host pulls the new image.

- `version` in `pyproject.toml` is stale by design; the git tag is the version. Don't bump it.
- Include `[skip ci]` in a commit message to skip the release.
- Pure-markdown and `tests/**` changes don't trigger a release (`paths-ignore`).
- A green run is not a release: confirm the `v*` tag appeared.
- The deploy-webhook step is skipped when its repo secret is unset; when set, it must match the deploy target's webhook secret exactly (a mismatch fails with a 401).
- Dependabot runs weekly (uv, docker, actions ecosystems) with auto-merge; each merged PR ships a release.

## Architecture

FastMCP 4 server (src layout, `src/mcp_timely/`) exposing three question-shaped read tools plus three guarded write tools over Timely's API. No user/team parameters: everything is scoped to the authorized user (account + user id resolved lazily at first call and cached).

**Tools** (`server.py`) — the reads are 1–2 upstream calls each; Timely does the aggregation:
- `projects_overview` — `GET /1.1/{acc}/projects`; hours, budget burn %, unbilled figures
- `time_spent` — project/client grouping via `GET /reports` (client rollups with nested projects), day/label via `POST /reports/filter` (group keys are plural: `days`, `labels`; unknown keys return totals with silently empty group arrays). Day buckets follow the account's timezone.
- `work_log` — user-scoped `GET /users/{id}/events`; entries with their event `id`, notes, label names (events carry only `label_ids`, so one `GET /labels` names them, skipped when no entry is labelled), billable/billed, timer state
- `create_entry` — write path (CDI-1956). `POST /1.1/{acc}/hours`; note that Timely creates events under `/hours`, not `/events`. Resolves `project` and `label` from names to ids so callers need no lookup of their own (the StoryKeep retainer project is renamed monthly), takes `minutes` as an int rather than float hours, and refuses a missing label instead of creating one. Pass `external_id` to make the call repeat-safe: the day's entries are checked first and a match returns `created: false` without writing. Entries price at a real hourly rate on a client retainer, so the day/minutes/note guards run before any write.
- `update_entry` / `delete_entry` — correction paths (CDI-1974). `GET`, `PUT` and `DELETE /1.1/{acc}/hours/{id}` (verified against the OpenAPI spec at developer.timely.com; PUT takes `{"event": {...}}` and changes only the fields sent). The entry is fetched first and refused when billed/invoiced, locked, another user's, or timing. Update sends only the passed fields, resolves `project`/`labels` by name like `create_entry`, and runs the same day/minutes/note validation. Both return a `before` snapshot (update also `after`) so a change can be reversed by hand; delete refuses unless `confirm` is true.

All tools return pydantic envelopes with a human-readable `summary` and raise `ToolError` on failure. Durations/money from Timely are objects (`{total_hours, formatted}` / `{amount, formatted, currency_code}`) — the `_hours`/`_formatted`/`_money` helpers parse defensively.

**OAuth session** (`session.py`) — the load-bearing part. Spike-verified Timely behavior: access tokens carry no `expires_in`; **refresh tokens rotate on every refresh**. Strategy: use the access token until a 401, refresh once (atomic token-file write via tmp + `os.replace`, guarded by an asyncio lock), retry once. A failed refresh raises `ToolError` naming `mcp-timely auth`. Token values never appear in errors or logs.

**The token file is a singleton session.** `TIMELY_TOKEN_FILE` (default `timely_tokens.json`, `/data/timely_tokens.json` in Docker) holds the only valid refresh token. Running `mcp-timely auth` while a deployed instance is live rotates the family and strands the deployed pair — re-bootstrap and re-place the file if that happens.

**Config** (`config.py`): pydantic-settings from env. `TRANSPORT=http` refuses to start without `MCP_API_KEY` (bearer auth, `auth.py`, timing-safe compare).

## Deployment

Production runs the published GHCR image via `compose.yaml` (container port 8000, host port 8011) with the token file on the `timely-data` volume and `MCP_API_KEY`, `TIMELY_CLIENT_ID`, `TIMELY_CLIENT_SECRET` injected by the deploy tool. It sits behind an authenticating tunnel. Host names, stack names and secret locations are kept out of this public repo. Planning lives in an external OpenSpec store (`openspec/config.yaml` references it).

## fastmcp 4 idioms

- `fastmcp>=4.0.10,<5.0.0`; streamable-http with `stateless_http=True` passed to `run()`, not the constructor (v4 rejects it there).
- Annotations are snake_case (`read_only_hint`, ...). CI and the release test step run with `FASTMCP_MCP_CAMELCASE_COMPAT=false`, so camelCase access fails the build.
- Failures raise `ToolError`; a returned error payload is logged by usage telemetry as `outcome: ok`.
- `src/mcp_timely/usage.py` is vendored verbatim from `CDiT-infrastructure/scripts/mcp_usage_middleware.py`; re-copy it, never edit it here.
- Testing: the `mcp-testing` skill. Release/deploy: the `cdit-release-pipeline` skill. Fleet conventions: `CDiT-infrastructure/docs/wiki/topics/mcp-fleet.md`.

## Conventions

- Tests use FastMCP's in-memory `Client(mcp)`; upstream is mocked at the session (`AsyncMock`) or via `httpx.MockTransport`. Keep the surface test (`test_tool_surface_is_three_reads_and_three_writes`) green — it is the spec's guarantee. It asserts the three reads stay read-only and that `create_entry`, `update_entry` and `delete_entry` are the only tools permitted to write, so adding another write path fails the build until someone decides that deliberately.
- Public repo: no CDIT internals in README, no secrets anywhere; `.env` and the token file are gitignored.
- New tools must stay question-shaped: if answering requires the caller to loop or sum, push the aggregation upstream or reshape the tool.
