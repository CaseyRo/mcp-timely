"""Tests for the mark_billed write path (CDI-2075).

Same harness as test_edit_entries: in-memory client, upstream mocked at the
session. The listing goes through ``session.get``; each PUT through
``session.request``, so a dry run is proven by ``request`` never firing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastmcp.client.client import Client
from fastmcp.exceptions import ToolError

import mcp_timely.server as srv

ME = 42
ROOM = {"id": 5701, "name": "Exhibition room"}


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _entry(id, hours=2.0, **kw):
    return {
        "id": id,
        "user": {"id": ME},
        "project": ROOM,
        "day": "2026-10-20",
        "duration": {"total_hours": hours},
        "billable": True,
        "billed": False,
        "invoice_id": None,
        "timer_state": "default",
        **kw,
    }


HOURS = [
    _entry(1, 3.0),
    _entry(2, 1.5),
    _entry(3, billable=False),  # not billed hours stay as they are
    _entry(4, billed=True),  # already billed: skipped
    _entry(5, timer_state="start"),  # running timer
    _entry(6, user={"id": 7}),  # someone else's
]


@pytest.fixture
def upstream(monkeypatch):
    monkeypatch.setattr(srv.session, "account_id", AsyncMock(return_value=1141447))
    monkeypatch.setattr(srv.session, "user_id", AsyncMock(return_value=ME))
    seen = {}

    async def _get(path, params=None):
        if path.endswith("/projects"):
            return _Resp([{**ROOM, "active": True}])
        if path.endswith("/hours"):
            seen["params"] = params
            return _Resp(HOURS)
        raise AssertionError(f"unexpected GET {path}")

    monkeypatch.setattr(srv.session, "get", _get)
    request = AsyncMock(
        side_effect=lambda m, path, json_body=None: _Resp(
            {"id": int(path.rsplit("/", 1)[1]), "billed": True}
        )
    )
    monkeypatch.setattr(srv.session, "request", request)
    return seen, request


@pytest.fixture
async def client():
    async with Client(srv.mcp) as c:
        yield c


ARGS = {"project": "Exhibition room", "since": "2026-10-01", "upto": "2026-10-23"}


async def test_dry_run_lists_only_eligible_entries_and_writes_nothing(client, upstream):
    seen, request = upstream
    data = (await client.call_tool("mark_billed", ARGS)).structured_content
    assert data["marked"] is False
    assert data["entry_ids"] == [1, 2]
    assert data["hours_formatted"] == "04:30"
    assert "Dry run" in data["summary"]
    request.assert_not_called()
    assert seen["params"]["project_id"] == ROOM["id"]
    assert seen["params"]["user_id"] == ME


async def test_confirm_puts_billed_true_per_entry(client, upstream):
    _, request = upstream
    data = (
        await client.call_tool("mark_billed", {**ARGS, "confirm": True})
    ).structured_content
    assert data["marked"] is True
    assert data["failed_ids"] == []
    assert [c.args[1] for c in request.call_args_list] == [
        "/1.1/1141447/hours/1",
        "/1.1/1141447/hours/2",
    ]
    assert request.call_args.kwargs["json_body"] == {"event": {"billed": True}}


async def test_entry_not_coming_back_billed_is_reported(client, upstream):
    _, request = upstream
    request.side_effect = lambda m, path, json_body=None: _Resp({"billed": False})
    data = (
        await client.call_tool("mark_billed", {**ARGS, "confirm": True})
    ).structured_content
    assert data["failed_ids"] == [1, 2]
    assert "did not come back billed" in data["summary"]


async def test_bad_range_is_refused_before_any_call(client, upstream):
    _, request = upstream
    with pytest.raises(ToolError, match="after upto"):
        await client.call_tool(
            "mark_billed", {**ARGS, "since": "2026-10-24", "upto": "2026-10-01"}
        )
    request.assert_not_called()
