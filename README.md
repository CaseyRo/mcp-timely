# mcp-timely

MCP server for [Timely](https://timelyapp.com) time tracking — built for asking
questions, not wrapping endpoints. Three read-only tools, each answered in a
single upstream call with Timely doing the aggregation server-side, so an LLM
never pages through raw entries to sum hours (and never gets the sum wrong).

| Tool | The question it answers |
|------|------------------------|
| `projects_overview` | *What's on my plate?* — projects with client, logged hours, budget burn, unbilled amounts |
| `time_spent` | *Where did my time go?* — date-range rollups grouped by project, client, label, or day, with billable split |
| `work_log` | *What did I actually do?* — individual entries with notes for a range; standup/diary/invoicing material |

Scope is deliberate: **read-only**, and always scoped to the authorized user.
There are no write tools and no user/team parameters.

## Setup

1. **Create a Timely OAuth app** at
   `https://app.timelyapp.com/<account>/oauth_applications` with callback URL
   `urn:ietf:wg:oauth:2.0:oob`.

2. **Configure**

   ```bash
   cp .env.example .env   # fill in TIMELY_CLIENT_ID and TIMELY_CLIENT_SECRET
   ```

3. **Authorize (once)**

   ```bash
   uv sync
   uv run mcp-timely auth
   ```

   Open the printed URL, authorize, paste the code. Tokens land in
   `TIMELY_TOKEN_FILE` (default `timely_tokens.json`) and refresh themselves
   from then on.

4. **Add to Claude Code**

   ```bash
   claude mcp add timely -- uv run --directory /path/to/mcp-timely mcp-timely
   ```

## Token handling

Timely rotates refresh tokens on every refresh and its access tokens carry no
expiry. The session layer uses the access token until a 401, then refreshes
once — persisting the new pair atomically *before* continuing — and retries the
request once. The token file is the only state; keep it on a persistent volume
in Docker. Re-authorization is only needed if the grant is revoked.

## HTTP deployment

Set `TRANSPORT=http` and `MCP_API_KEY` (the server refuses to start
unauthenticated HTTP). Serves streamable-http on `/mcp`, liveness on
`/health`. See `compose.yaml`.

## Development

```bash
uv run pytest        # unit tests (in-memory MCP client, no network)
uv run ruff check .
```

## Releases

Every push to `main` that touches non-doc files releases automatically: tests
and a security audit run, the patch version bumps, a `vX.Y.Z` tag lands, and a
multi-arch image is published to `ghcr.io/caseyro/mcp-timely`. Consequences:

- Don't edit `version` in `pyproject.toml` by hand — CI owns it.
- Add `[skip ci]` to a commit message to skip a release.
- Rebase PRs on `main` before merging so the auto-bump commit doesn't race yours.
- Markdown-only and test-only changes don't trigger a release.

## License

MIT
