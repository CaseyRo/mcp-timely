"""Session layer: token persistence, rotation, 401 retry-once, error paths."""

from __future__ import annotations

import json

import httpx
import pytest
from fastmcp.exceptions import ToolError

from mcp_timely.config import Settings
from mcp_timely.session import TimelySession

OLD = {"access_token": "old-access", "refresh_token": "old-refresh"}
NEW = {"access_token": "new-access", "refresh_token": "new-refresh"}


def make_session(tmp_path, handler, tokens=OLD) -> TimelySession:
    settings = Settings(
        timely_client_id="cid",
        timely_client_secret="csecret",
        timely_token_file=tmp_path / "tokens.json",
    )
    if tokens is not None:
        settings.timely_token_file.write_text(json.dumps(tokens))
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.test"
    )
    return TimelySession(settings, client=client)


async def test_missing_token_file_names_bootstrap(tmp_path):
    session = make_session(tmp_path, lambda req: httpx.Response(200), tokens=None)
    with pytest.raises(ToolError, match="mcp-timely auth"):
        await session.get("/1.1/accounts")


async def test_401_refreshes_once_retries_and_persists_rotated_pair(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, request.headers.get("authorization")))
        if request.url.path == "/1.1/oauth/token":
            return httpx.Response(200, json=NEW)
        if request.headers.get("authorization") == "Bearer new-access":
            return httpx.Response(200, json=[{"id": 1, "name": "acct"}])
        return httpx.Response(401)

    session = make_session(tmp_path, handler)
    resp = await session.get("/1.1/accounts")

    assert resp.json() == [{"id": 1, "name": "acct"}]
    # rotated pair persisted to disk before the retry
    on_disk = json.loads((tmp_path / "tokens.json").read_text())
    assert on_disk == NEW
    # exactly one refresh, one retry
    assert [p for p, _ in calls] == [
        "/1.1/accounts",
        "/1.1/oauth/token",
        "/1.1/accounts",
    ]


async def test_refresh_failure_raises_without_token_material(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/1.1/oauth/token":
            return httpx.Response(401)
        return httpx.Response(401)

    session = make_session(tmp_path, handler)
    with pytest.raises(ToolError, match="mcp-timely auth") as exc_info:
        await session.get("/1.1/accounts")
    message = str(exc_info.value)
    assert "old-access" not in message
    assert "old-refresh" not in message


async def test_429_surfaces_as_tool_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "3600"})

    session = make_session(tmp_path, handler)
    with pytest.raises(ToolError, match="rate limit"):
        await session.get("/1.1/accounts")


async def test_exchange_code_writes_token_file(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/1.1/oauth/token"
        body = request.content.decode()
        assert "grant_type=authorization_code" in body
        assert "code=oob-code" in body
        return httpx.Response(200, json=NEW)

    session = make_session(tmp_path, handler, tokens=None)
    await session.exchange_code("oob-code")
    assert json.loads((tmp_path / "tokens.json").read_text()) == NEW
