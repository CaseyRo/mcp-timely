# mcp-timely

An MCP server for [Timely](https://timelyapp.com) time tracking, built for
asking questions rather than wrapping endpoints. It is for Timely users who want
Claude, or any other MCP client, to answer "where did my time go?" reliably.
Each of its three read-only tools is answered in a single upstream call with
Timely doing the aggregation server-side, so the model never pages through raw
entries to sum hours (and never gets the sum wrong).

| Tool | The question it answers |
|------|------------------------|
| `projects_overview` | *What's on my plate?* Projects with client, logged hours, budget burn, unbilled amounts |
| `time_spent` | *Where did my time go?* Date-range rollups grouped by project, client, label or day, with billable split |
| `work_log` | *What did I actually do?* Individual entries with notes for a range; standup, diary or invoicing material |

Scope is deliberate: **read-only**, and always scoped to the authorized user.
There are no write tools and no user or team parameters.

## Requirements

- Python 3.12 or newer and [uv](https://docs.astral.sh/uv/)
- FastMCP 4 (`fastmcp>=4.0.10,<5.0.0`, installed by `uv sync`)
- A Timely account and a Timely OAuth application
- Docker, only for the containerized HTTP deployment

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

4. **Add to Claude Code** (stdio)

   ```bash
   claude mcp add timely -- uv run --directory /path/to/mcp-timely mcp-timely
   ```

## Configuration

Settings come from environment variables (see `.env.example`).

| Variable | Default | Purpose |
|----------|---------|---------|
| `TIMELY_CLIENT_ID` | empty | Timely OAuth application ID. |
| `TIMELY_CLIENT_SECRET` | empty | Timely OAuth application secret. |
| `TIMELY_TOKEN_FILE` | `timely_tokens.json` | Where the token pair is stored. Must be writable and persistent. |
| `TIMELY_API_BASE` | `https://api.timelyapp.com` | Timely API base URL. |
| `TRANSPORT` | `stdio` | `stdio` or `http` (streamable HTTP on `/mcp`). |
| `HOST` | `127.0.0.1` | Bind address for `http`. |
| `PORT` | `8000` | Listen port for `http`. |
| `MCP_API_KEY` | empty | Bearer token for the MCP endpoint. Required when `TRANSPORT=http`. |
| `APP_VERSION` | unset | Version string reported by `/health`; set by the release image build. |

## Authentication

There are two separate credentials:

- **Upstream (Timely):** OAuth 2.0, bootstrapped once with `mcp-timely auth`.
  Timely rotates refresh tokens on every refresh and its access tokens carry no
  expiry. The server uses the access token until a 401, then refreshes once,
  writes the new pair atomically *before* continuing, and retries the request
  once. The token file is the only state. Running `mcp-timely auth` again
  rotates the token family, so any other running instance needs the new file.
  Otherwise re-authorization is only needed if the grant is revoked.
- **Clients (HTTP only):** with `TRANSPORT=http`, every request needs
  `Authorization: Bearer <MCP_API_KEY>` (constant-time compare). The server
  refuses to start over HTTP without a key.

## HTTP deployment

`TRANSPORT=http` serves streamable HTTP on `/mcp` and liveness on `/health`
(also `/healthz`). The `Dockerfile` builds a non-root image with the token file
at `/data/timely_tokens.json`; `compose.yaml` runs the published image on host
port 8011 with a persistent `timely-data` volume. Run `mcp-timely auth` first
and place the resulting token file in that volume.

```bash
docker compose up
```

## Usage telemetry

A small middleware (`src/mcp_timely/usage.py`) writes one JSON line per tool
call to stderr: server name, tool name, duration, outcome and MCP protocol
version. It never records arguments or results. Tool failures raise
`ToolError`, so they are logged with `outcome: error`.

## Development

```bash
uv sync
uv run pytest        # in-memory MCP client, no network
uv run ruff check .
```

CI (`.github/workflows/ci.yml`) runs ruff and the tests as the `test` check,
which is required before a pull request can merge into `main`.

## Releases

Every push to `main` that touches non-doc files releases automatically: tests
and a security audit run, the next `vX.Y.Z` tag is pushed (no version-bump
commit), and a multi-arch image is published to `ghcr.io/caseyro/mcp-timely`
with that version baked in (reported by `/health`). Consequences:

- The git tag is the version; `version` in `pyproject.toml` is not bumped.
- Add `[skip ci]` to a commit message to skip a release.
- Markdown-only and test-only changes don't trigger a release.

## Support

If this server saves you time, you can [buy me a coffee](https://buymeacoffee.com/caseyberlin).

## License

MIT, see [LICENSE](LICENSE).
