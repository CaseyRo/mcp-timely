"""The MCP surface over the wire, via an in-memory ``fastmcp.Client``.

test_server.py asserts on tool payloads. These tests check what a connector sees:
the server stands up, every tool is registered, the hints survive serialisation
(CI sets FASTMCP_MCP_CAMELCASE_COMPAT=false, so snake_case is what the client reads),
a read-only call round-trips with the upstream mocked at the session boundary, and
the call emits exactly one telemetry line on stderr.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastmcp import Client

import mcp_timely.server as srv

EXPECTED_TOOLS = {
    "projects_overview", "time_spent", "work_log",
    "create_entry", "update_entry", "delete_entry", "mark_billed",
}
ACCOUNT = 1141447


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def upstream(monkeypatch):
    monkeypatch.setattr(srv.session, "account_id", AsyncMock(return_value=ACCOUNT))
    get = AsyncMock(return_value=_Resp([]))
    monkeypatch.setattr(srv.session, "get", get)
    return get


async def test_server_registers_its_tools():
    async with Client(srv.mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert EXPECTED_TOOLS <= names, f"missing: {EXPECTED_TOOLS - names}"


async def test_annotations_survive_the_wire():
    async with Client(srv.mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert tools["projects_overview"].annotations.read_only_hint is True
    assert tools["delete_entry"].annotations.destructive_hint is True


async def test_read_only_call_round_trips(upstream):
    async with Client(srv.mcp) as client:
        result = await client.call_tool("projects_overview", {})
    upstream.assert_awaited_once()
    assert upstream.await_args.args[0] == f"/1.1/{ACCOUNT}/projects"
    assert result.structured_content["count"] == 0


async def test_a_tool_call_emits_one_usage_line(upstream, capfd):
    capfd.readouterr()
    async with Client(srv.mcp) as client:
        await client.call_tool("projects_overview", {})
    lines = [ln for ln in capfd.readouterr().err.splitlines() if '"mcp_usage"' in ln]
    assert len(lines) == 1
    assert '"server": "timely"' in lines[0]
    assert '"tool": "projects_overview"' in lines[0]
    assert '"outcome": "ok"' in lines[0]
