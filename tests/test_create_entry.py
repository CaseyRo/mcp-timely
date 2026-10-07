"""Tests for the create_entry write path (CDI-1956).

Kept in its own module: these are the first tests in the suite that exercise a
write, and the money path (entries price at a real hourly rate on a client
retainer) deserves its guards tested explicitly rather than folded into the
read-tool file.
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


RETAINER = [
    {"id": 5683637, "name": "StoryKeep Retainer (October)", "active": True},
    {"id": 5590403, "name": "StoryKeep Retainer (September)", "active": True},
]

# Timely nests labels one level deep; "claude" sits under a parent on purpose,
# so the flattening walk is exercised rather than assumed.
LABELS = [
    {"id": 7, "name": "Administrative", "children": []},
    {"id": 8, "name": "Tasks", "children": [{"id": 9, "name": "claude"}]},
]

CREATED = {"id": 500, "project": {"name": "StoryKeep Retainer (October)"}}


def _route_get(projects=None, labels=None, events=None):
    """create_entry issues several GETs; dispatch them by path."""

    async def _get(path, params=None):
        if "/labels" in path:
            return _Resp(labels if labels is not None else [])
        if "/events" in path:
            return _Resp(events if events is not None else [])
        if "/projects" in path:
            return _Resp(projects if projects is not None else [])
        raise AssertionError(f"unexpected GET {path}")

    return _get


@pytest.fixture
def scoped(monkeypatch):
    monkeypatch.setattr(srv.session, "account_id", AsyncMock(return_value=1141447))
    monkeypatch.setattr(srv.session, "user_id", AsyncMock(return_value=42))


@pytest.fixture
async def client():
    async with Client(srv.mcp) as c:
        yield c


async def test_resolves_project_and_label_by_name_and_posts_event(
    client, scoped, monkeypatch
):
    monkeypatch.setattr(
        srv.session, "get", _route_get(projects=RETAINER, labels=LABELS)
    )
    post = AsyncMock(return_value=_Resp(CREATED))
    monkeypatch.setattr(srv.session, "post", post)

    data = _payload(
        await client.call_tool(
            "create_entry",
            {
                "project": "StoryKeep Retainer (October)",
                "day": "2026-10-06",
                "minutes": 95,
                "note": "CDI-1957 — wired the session hook",
                "label": "claude",
            },
        )
    )

    assert post.call_args.args[0] == "/1.1/1141447/hours"
    event = post.call_args.kwargs["json_body"]["event"]
    # The exact-name match must pick October, not the September sibling.
    assert event["project_id"] == 5683637
    assert event["user_id"] == 42
    assert (event["hours"], event["minutes"]) == (1, 35)  # 95 min, not 1.58h
    assert event["label_ids"] == [9]  # nested child label resolved
    assert event["note"] == "CDI-1957 — wired the session hook"
    assert "billable" not in event  # omitted => project's own setting
    assert data["created"] is True
    assert data["entry_id"] == 500
    assert data["hours_formatted"] == "01:35"


@pytest.mark.parametrize("flag", [True, False])
async def test_billable_is_forwarded_verbatim_when_set(
    client, scoped, monkeypatch, flag
):
    """Explicit billable must reach Timely unchanged.

    The SessionEnd hook sends billable=True on purpose so agent hours count
    against the StoryKeep retainer rather than inheriting whatever the month's
    project happens to be set to.
    """
    monkeypatch.setattr(srv.session, "get", _route_get(projects=RETAINER))
    post = AsyncMock(return_value=_Resp(CREATED))
    monkeypatch.setattr(srv.session, "post", post)
    await client.call_tool(
        "create_entry",
        {
            "project": 5683637,
            "day": "2026-10-07",
            "minutes": 60,
            "note": "billable flag",
            "billable": flag,
        },
    )
    assert post.call_args.kwargs["json_body"]["event"]["billable"] is flag


async def test_numeric_project_skips_the_lookup(client, scoped, monkeypatch):
    async def _get(path, params=None):
        if "/labels" in path:
            return _Resp(LABELS)
        raise AssertionError(f"should not GET {path} for a numeric project id")

    monkeypatch.setattr(srv.session, "get", _get)
    monkeypatch.setattr(srv.session, "post", AsyncMock(return_value=_Resp(CREATED)))
    data = _payload(
        await client.call_tool(
            "create_entry",
            {
                "project": 5683637,
                "day": "2026-10-06",
                "minutes": 30,
                "note": "direct id",
            },
        )
    )
    assert data["project_id"] == 5683637


async def test_external_id_already_present_creates_nothing(client, scoped, monkeypatch):
    """The hook can fire twice; double-logged hours reach a client invoice."""
    existing = [{"id": 411, "external_id": "sess-abc", "day": "2026-10-06"}]
    monkeypatch.setattr(
        srv.session, "get", _route_get(projects=RETAINER, events=existing)
    )
    post = AsyncMock()
    monkeypatch.setattr(srv.session, "post", post)

    data = _payload(
        await client.call_tool(
            "create_entry",
            {
                "project": 5683637,
                "day": "2026-10-06",
                "minutes": 60,
                "note": "retry of the same session",
                "external_id": "sess-abc",
            },
        )
    )
    post.assert_not_called()
    assert data["created"] is False
    assert data["entry_id"] == 411


async def test_missing_label_refuses_rather_than_creating_one(
    client, scoped, monkeypatch
):
    monkeypatch.setattr(srv.session, "get", _route_get(projects=RETAINER, labels=[]))
    post = AsyncMock()
    monkeypatch.setattr(srv.session, "post", post)
    with pytest.raises(ToolError, match="No Timely label named 'claude'"):
        await client.call_tool(
            "create_entry",
            {
                "project": 5683637,
                "day": "2026-10-06",
                "minutes": 60,
                "note": "x",
                "label": "claude",
            },
        )
    post.assert_not_called()


async def test_ambiguous_project_name_refuses(client, scoped, monkeypatch):
    twins = [
        {"id": 1, "name": "Retainer", "active": True},
        {"id": 2, "name": "retainer", "active": True},
    ]
    monkeypatch.setattr(srv.session, "get", _route_get(projects=twins))
    with pytest.raises(ToolError, match="pass the id instead"):
        await client.call_tool(
            "create_entry",
            {"project": "Retainer", "day": "2026-10-06", "minutes": 60, "note": "x"},
        )


@pytest.mark.parametrize(
    "bad,match",
    [
        ({"day": "06-10-2026"}, "YYYY-MM-DD"),
        ({"minutes": 0}, "must be positive"),
        ({"minutes": 1441}, "implausible"),
        ({"note": "   "}, "note is required"),
    ],
)
async def test_guards_reject_bad_input_before_any_write(
    client, scoped, monkeypatch, bad, match
):
    """A caller bug must not become an invoice line."""
    monkeypatch.setattr(srv.session, "get", _route_get(projects=RETAINER))
    post = AsyncMock()
    monkeypatch.setattr(srv.session, "post", post)
    args = {
        "project": 5683637,
        "day": "2026-10-06",
        "minutes": 60,
        "note": "fine",
        **bad,
    }
    with pytest.raises(ToolError, match=match):
        await client.call_tool("create_entry", args)
    post.assert_not_called()
