"""Tests for the update_entry / delete_entry write paths (CDI-1974).

Same harness as test_create_entry: in-memory client, upstream mocked at the
session. Reads go through ``session.get``; the PUT/DELETE go through
``session.request``, so a refused call is proven by ``request`` never firing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastmcp.client.client import Client
from fastmcp.exceptions import ToolError

import mcp_timely.server as srv


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _payload(result):
    if getattr(result, "structured_content", None):
        return result.structured_content
    if hasattr(result, "data") and result.data is not None:
        data = result.data
        if hasattr(data, "model_dump"):
            return data.model_dump(by_alias=True, exclude_none=True)
        return data
    raise AssertionError(f"Unexpected result shape: {result!r}")


ME = 42
OCTOBER = {"id": 5683637, "name": "StoryKeep Retainer (October)"}
SEPTEMBER = {"id": 5590403, "name": "StoryKeep Retainer (September)"}
PROJECTS = [{**OCTOBER, "active": True}, {**SEPTEMBER, "active": True}]

ENTRY = {
    "id": 700,
    "user": {"id": ME},
    "project": SEPTEMBER,
    "day": "2026-10-02",
    "duration": {"total_hours": 1.5, "formatted": "01:30"},
    "note": "CDI-1957 session",
    "label_ids": [],
    "billable": True,
    "billed": False,
    "locked": False,
    "invoice_id": None,
    "timer_state": "default",
}


def _route_get(entry, projects=PROJECTS, labels=None):
    async def _get(path, params=None):
        if path.endswith(f"/hours/{entry['id']}"):
            return _Resp(entry)
        if "/projects" in path:
            return _Resp(projects)
        if "/labels" in path:
            return _Resp(labels or [])
        raise AssertionError(f"unexpected GET {path}")

    return _get


@pytest.fixture
def scoped(monkeypatch):
    monkeypatch.setattr(srv.session, "account_id", AsyncMock(return_value=1141447))
    monkeypatch.setattr(srv.session, "user_id", AsyncMock(return_value=ME))


@pytest.fixture
async def client():
    async with Client(srv.mcp) as c:
        yield c


async def test_move_to_another_project_by_name(client, scoped, monkeypatch):
    moved = {**ENTRY, "project": OCTOBER}
    monkeypatch.setattr(srv.session, "get", _route_get(ENTRY))
    request = AsyncMock(return_value=_Resp(moved))
    monkeypatch.setattr(srv.session, "request", request)

    data = _payload(
        await client.call_tool(
            "update_entry",
            {"id": 700, "project": "StoryKeep Retainer (October)"},
        )
    )

    assert request.call_args.args == ("PUT", "/1.1/1141447/hours/700")
    # Only the passed field is sent; nothing else is overwritten.
    assert request.call_args.kwargs["json_body"] == {"event": {"project_id": 5683637}}
    assert data["before"]["project_id"] == 5590403  # enough to move it back
    assert data["after"]["project"] == "StoryKeep Retainer (October)"
    assert "September" in data["summary"] and "October" in data["summary"]


async def test_update_sends_minutes_note_day_and_labels(client, scoped, monkeypatch):
    labels = [{"id": 8, "name": "Tasks", "children": [{"id": 9, "name": "claude"}]}]
    monkeypatch.setattr(srv.session, "get", _route_get(ENTRY, labels=labels))
    request = AsyncMock(return_value=_Resp({**ENTRY, "label_ids": [9]}))
    monkeypatch.setattr(srv.session, "request", request)
    data = _payload(
        await client.call_tool(
            "update_entry",
            {
                "id": 700,
                "minutes": 95,
                "note": " fixed note ",
                "day": "2026-10-03",
                "labels": ["claude"],
            },
        )
    )
    assert request.call_args.kwargs["json_body"]["event"] == {
        "hours": 1,
        "minutes": 35,
        "note": "fixed note",
        "day": "2026-10-03",
        "label_ids": [9],
    }
    assert data["after"]["labels"] == ["claude"]


@pytest.mark.parametrize(
    "patch,match",
    [
        ({"billed": True}, "is billed"),
        ({"invoice_id": 31}, "is billed"),
        ({"locked": True, "locked_reason": "period closed"}, "locked"),
        ({"user": {"id": 99}}, "another user"),
        ({"timer_state": "start"}, "running timer"),
    ],
)
async def test_guarded_entries_are_refused_before_any_write(
    client, scoped, monkeypatch, patch, match
):
    entry = {**ENTRY, **patch}
    monkeypatch.setattr(srv.session, "get", _route_get(entry))
    request = AsyncMock()
    monkeypatch.setattr(srv.session, "request", request)
    for tool, args in (
        ("update_entry", {"id": 700, "note": "move it"}),
        ("delete_entry", {"id": 700, "confirm": True}),
    ):
        with pytest.raises(ToolError, match=match):
            await client.call_tool(tool, args)
    request.assert_not_called()


async def test_unknown_project_refused(client, scoped, monkeypatch):
    monkeypatch.setattr(srv.session, "get", _route_get(ENTRY))
    request = AsyncMock()
    monkeypatch.setattr(srv.session, "request", request)
    with pytest.raises(ToolError, match="No Timely project named 'Nope'"):
        await client.call_tool("update_entry", {"id": 700, "project": "Nope"})
    request.assert_not_called()


@pytest.mark.parametrize(
    "bad,match",
    [
        ({"day": "02-10-2026"}, "YYYY-MM-DD"),
        ({"minutes": 0}, "must be positive"),
        ({"minutes": 1441}, "implausible"),
        ({"note": "  "}, "note is required"),
        ({}, "Nothing to change"),
    ],
)
async def test_update_validation_runs_before_any_call(
    client, scoped, monkeypatch, bad, match
):
    get = AsyncMock()
    monkeypatch.setattr(srv.session, "get", get)
    with pytest.raises(ToolError, match=match):
        await client.call_tool("update_entry", {"id": 700, **bad})
    get.assert_not_called()


async def test_delete_without_confirm_refused(client, scoped, monkeypatch):
    get, request = AsyncMock(), AsyncMock()
    monkeypatch.setattr(srv.session, "get", get)
    monkeypatch.setattr(srv.session, "request", request)
    with pytest.raises(ToolError, match="confirm=true"):
        await client.call_tool("delete_entry", {"id": 700})
    get.assert_not_called()
    request.assert_not_called()


async def test_delete_with_confirm(client, scoped, monkeypatch):
    monkeypatch.setattr(srv.session, "get", _route_get(ENTRY))
    request = AsyncMock(return_value=_Resp({}))
    monkeypatch.setattr(srv.session, "request", request)
    data = _payload(
        await client.call_tool("delete_entry", {"id": 700, "confirm": True})
    )
    assert request.call_args.args == ("DELETE", "/1.1/1141447/hours/700")
    assert data["before"]["id"] == 700
    assert data["before"]["hours_formatted"] == "01:30"
    assert "after" not in data
    assert "Deleted entry 700" in data["summary"]
