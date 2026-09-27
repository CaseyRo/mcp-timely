"""Tool tests via FastMCP's in-memory client, upstream mocked at the session."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastmcp.client.client import Client

import mcp_timely.server as srv


def _payload(result):
    """Pull the serialised dict out of a CallToolResult (fleet pattern)."""
    if getattr(result, "structured_content", None):
        return result.structured_content
    if hasattr(result, "data") and result.data is not None:
        data = result.data
        if hasattr(data, "model_dump"):
            return data.model_dump(by_alias=True, exclude_none=True)
        return data
    raise AssertionError(f"Unexpected result shape: {result!r}")


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


DUR = lambda h, f: {"total_hours": h, "formatted": f, "total_seconds": int(h * 3600)}  # noqa: E731

PROJECTS = [
    {
        "id": 1,
        "name": "Site relaunch",
        "active": True,
        "billable": True,
        "client": {"name": "ACME"},
        "duration": DUR(6.5, "06:30"),
        "unbilled_duration": DUR(6.5, "06:30"),
        "unbilled_cost": {"amount": 585.0, "formatted": "€585,00"},
        "budget": 100,
        "budget_type": "H",
        "budget_percent": 42.0,
        "hour_rate": 90,
    },
    {
        "id": 2,
        "name": "Old thing",
        "active": False,
        "billable": False,
        "client": None,
        "duration": DUR(1.0, "01:00"),
    },
]

REPORT_CLIENTS = [
    {
        "id": 9,
        "name": "ACME",
        "duration": DUR(4.0, "04:00"),
        "billable_duration": DUR(3.0, "03:00"),
        "non_billable_duration": DUR(1.0, "01:00"),
        "projects": [
            {
                "id": 1,
                "name": "Site relaunch",
                "duration": DUR(3.0, "03:00"),
                "billable_duration": DUR(3.0, "03:00"),
                "non_billable_duration": DUR(0.0, "00:00"),
            },
            {
                "id": 3,
                "name": "Support",
                "duration": DUR(1.0, "01:00"),
                "billable_duration": DUR(0.0, "00:00"),
                "non_billable_duration": DUR(1.0, "01:00"),
            },
        ],
    }
]

EVENTS = [
    {
        "id": 11,
        "day": "2026-07-16",
        "duration": DUR(0.25, "00:15"),
        "note": "Refined terminal setup",
        "project": {"name": "Personal toolkit"},
        "billable": False,
        "billed": False,
        "timer_state": "default",
    },
    {
        "id": 10,
        "day": "2026-07-15",
        "duration": DUR(2.0, "02:00"),
        "note": "",
        "project": {"name": "Site relaunch"},
        "billable": True,
        "billed": False,
        "timer_state": "start",
    },
]


@pytest.fixture
def scoped(monkeypatch):
    monkeypatch.setattr(srv.session, "account_id", AsyncMock(return_value=1141447))
    monkeypatch.setattr(srv.session, "user_id", AsyncMock(return_value=42))


@pytest.fixture
async def client():
    async with Client(srv.mcp) as c:
        yield c


async def test_tool_surface_is_exactly_three_read_only_tools(client):
    tools = await client.list_tools()
    assert sorted(t.name for t in tools) == [
        "projects_overview",
        "time_spent",
        "work_log",
    ]
    for tool in tools:
        assert tool.annotations.readOnlyHint is True


async def test_projects_overview_filters_inactive_and_maps_fields(
    client, scoped, monkeypatch
):
    monkeypatch.setattr(srv.session, "get", AsyncMock(return_value=_Resp(PROJECTS)))
    data = _payload(await client.call_tool("projects_overview", {}))
    assert data["count"] == 1
    project = data["projects"][0]
    assert project["client"] == "ACME"
    assert project["hours_logged"] == 6.5
    assert project["budget_percent"] == 42.0
    assert project["unbilled_cost"] == "€585,00"
    assert "1 projects" in data["summary"]


async def test_projects_overview_include_inactive(client, scoped, monkeypatch):
    monkeypatch.setattr(srv.session, "get", AsyncMock(return_value=_Resp(PROJECTS)))
    data = _payload(
        await client.call_tool("projects_overview", {"include_inactive": True})
    )
    assert data["count"] == 2


async def test_time_spent_by_project_flattens_nested_projects(
    client, scoped, monkeypatch
):
    get = AsyncMock(return_value=_Resp(REPORT_CLIENTS))
    monkeypatch.setattr(srv.session, "get", get)
    data = _payload(
        await client.call_tool(
            "time_spent", {"since": "2026-07-01", "until": "2026-07-19"}
        )
    )
    assert get.call_args.args[0] == "/1.1/1141447/reports"
    assert [g["name"] for g in data["groups"]] == ["Site relaunch", "Support"]
    assert data["total_hours"] == 4.0
    assert data["billable_hours"] == 3.0
    assert data["total_formatted"] == "04:00"


async def test_time_spent_by_client_uses_top_level(client, scoped, monkeypatch):
    monkeypatch.setattr(
        srv.session, "get", AsyncMock(return_value=_Resp(REPORT_CLIENTS))
    )
    data = _payload(
        await client.call_tool(
            "time_spent",
            {"since": "2026-07-01", "until": "2026-07-19", "group_by": "client"},
        )
    )
    assert [g["name"] for g in data["groups"]] == ["ACME"]
    assert data["groups"][0]["hours"] == 4.0


async def test_time_spent_by_day_posts_filter_with_me_scope(
    client, scoped, monkeypatch
):
    post = AsyncMock(
        return_value=_Resp(
            {
                "days": [{"day": "2026-07-01", "duration": DUR(2.5, "02:30")}],
                "totals": {},
            }
        )
    )
    monkeypatch.setattr(srv.session, "post", post)
    data = _payload(
        await client.call_tool(
            "time_spent",
            {"since": "2026-07-01", "until": "2026-07-19", "group_by": "day"},
        )
    )
    body = post.call_args.kwargs["json_body"]
    assert body["group_by"] == ["days"]
    assert body["user_ids"] == [42]
    assert data["groups"][0]["name"] == "2026-07-01"
    assert data["groups"][0]["hours"] == 2.5


async def test_time_spent_empty_period_returns_zero_envelope(
    client, scoped, monkeypatch
):
    monkeypatch.setattr(srv.session, "get", AsyncMock(return_value=_Resp([])))
    data = _payload(
        await client.call_tool(
            "time_spent", {"since": "2026-01-01", "until": "2026-01-02"}
        )
    )
    assert data["total_hours"] == 0.0
    assert data["groups"] == []


async def test_work_log_sorts_by_day_and_maps_entries(client, scoped, monkeypatch):
    get = AsyncMock(return_value=_Resp(EVENTS))
    monkeypatch.setattr(srv.session, "get", get)
    data = _payload(
        await client.call_tool(
            "work_log", {"since": "2026-07-13", "upto": "2026-07-19"}
        )
    )
    assert get.call_args.args[0] == "/1.1/1141447/users/42/events"
    assert [e["day"] for e in data["entries"]] == ["2026-07-15", "2026-07-16"]
    first = data["entries"][0]
    assert first["project"] == "Site relaunch"
    assert first["timer_running"] is True
    assert "note" not in first or first["note"] is None  # empty note normalised
    assert data["total_hours"] == 2.25


async def test_tool_call_writes_one_usage_line(capsys, client, scoped, monkeypatch):
    monkeypatch.setattr(srv.session, "get", AsyncMock(return_value=_Resp(PROJECTS)))
    await client.call_tool("projects_overview", {})
    lines = [ln for ln in capsys.readouterr().err.splitlines() if '"mcp_usage"' in ln]
    assert len(lines) == 1
    assert '"server": "timely"' in lines[0]
    assert '"tool": "projects_overview"' in lines[0]
    assert '"outcome": "ok"' in lines[0]
