"""MCP server exposing question-shaped read tools for Timely.

Three tools, each ≤2 upstream calls: ``projects_overview``, ``time_spent``,
``work_log``. Read-only and scoped to the authorized user by design.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse

from .auth import BearerTokenVerifier
from .config import settings
from .session import TimelySession
from .usage import UsageMiddleware

try:
    # Releases are git tags; the image carries the tag as APP_VERSION.
    __version__ = os.environ.get("APP_VERSION") or version("mcp-timely")
except PackageNotFoundError:  # running from a source tree
    __version__ = "dev"

_start_time = datetime.now(timezone.utc)

session = TimelySession()

_auth = None
if settings.mcp_api_key.get_secret_value():
    _auth = BearerTokenVerifier(settings.mcp_api_key.get_secret_value())

mcp = FastMCP("mcp-timely", auth=_auth)
mcp.add_middleware(UsageMiddleware("timely"))


# -- envelopes ----------------------------------------------------------------


class ProjectSummary(BaseModel):
    id: int
    name: str
    client: str | None = None
    active: bool = True
    billable: bool = False
    hours_logged: float = 0.0
    hours_formatted: str = "00:00"
    budget: float | None = None
    budget_type: str | None = None  # "H" = hours, "M" = money
    budget_percent: float | None = None
    unbilled_hours: float = 0.0
    unbilled_cost: str | None = None  # pre-formatted by Timely, e.g. "€1.234,00"
    hour_rate: float | None = None


class ProjectsOverviewResult(BaseModel):
    summary: str
    count: int
    projects: list[ProjectSummary]


class GroupSlice(BaseModel):
    name: str
    hours: float
    hours_formatted: str
    billable_hours: float = 0.0
    non_billable_hours: float = 0.0


class TimeSpentResult(BaseModel):
    summary: str
    since: str
    until: str
    group_by: str
    total_hours: float
    total_formatted: str
    billable_hours: float
    non_billable_hours: float
    groups: list[GroupSlice]


class WorkLogEntry(BaseModel):
    day: str
    hours: float
    hours_formatted: str
    note: str | None = None
    project: str | None = None
    labels: list[str] = []
    billable: bool = False
    billed: bool = False
    timer_running: bool = False


class WorkLogResult(BaseModel):
    summary: str
    since: str
    upto: str
    total_hours: float
    entries: list[WorkLogEntry]


class CreateEntryResult(BaseModel):
    summary: str
    created: bool  # False when an entry with this external_id already existed
    entry_id: int | None = None
    project_id: int
    project: str | None = None
    day: str
    hours: float
    hours_formatted: str
    label: str | None = None
    note: str | None = None
    external_id: str | None = None


# -- upstream value parsing ---------------------------------------------------
# Timely durations are objects like {"total_hours": 6.5, "formatted": "06:30"},
# money values like {"amount": 0.0, "formatted": "€0,00"}. Parse defensively.


def _hours(value: Any) -> float:
    if isinstance(value, dict):
        return float(value.get("total_hours") or 0.0)
    return float(value or 0.0)


def _formatted(value: Any) -> str:
    if isinstance(value, dict) and value.get("formatted"):
        return str(value["formatted"])
    return "00:00"


def _money(value: Any) -> str | None:
    if isinstance(value, dict) and value.get("formatted"):
        return str(value["formatted"])
    return None


def _fmt_hours(total_hours: float) -> str:
    whole = int(total_hours)
    minutes = int(round((total_hours - whole) * 60))
    return f"{whole:02d}:{minutes:02d}"


def _slice(entry: dict[str, Any]) -> GroupSlice:
    name = str(entry.get("name") or entry.get("day") or entry.get("id") or "?")
    return GroupSlice(
        name=name,
        hours=_hours(entry.get("duration")),
        hours_formatted=_formatted(entry.get("duration")),
        billable_hours=_hours(entry.get("billable_duration")),
        non_billable_hours=_hours(entry.get("non_billable_duration")),
    )


# -- write-path resolution ----------------------------------------------------
# Callers know names ("StoryKeep Retainer (October)", "claude"), not numeric ids,
# and the retainer project is renamed every month. Resolve here so the caller
# never needs a lookup round-trip of its own.

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_MINUTES = 24 * 60


def _flatten_labels(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Timely labels nest via ``children``; match against parents and children."""
    out: list[dict[str, Any]] = []
    for label in raw:
        out.append(label)
        out.extend(_flatten_labels(label.get("children") or []))
    return out


async def _resolve_label(acc: int, name: str) -> tuple[int, str]:
    # Deliberately uncached: the usual failure is "label not created yet", and a
    # cache would keep serving that miss after the user creates it.
    raw = (await session.get(f"/1.1/{acc}/labels")).json()
    wanted = name.strip().casefold()
    for label in _flatten_labels(raw):
        if str(label.get("name") or "").strip().casefold() == wanted:
            return int(label["id"]), str(label["name"])
    raise ToolError(
        f"No Timely label named {name!r}. Create it once in Timely, then retry."
    )


async def _resolve_project(acc: int, project: int | str) -> tuple[int, str | None]:
    if isinstance(project, int) or str(project).strip().isdigit():
        return int(project), None  # trusted as-is; Timely rejects a bad id
    raw = (await session.get(f"/1.1/{acc}/projects", params={"per_page": 500})).json()
    wanted = str(project).strip().casefold()
    hits = [p for p in raw if str(p.get("name") or "").strip().casefold() == wanted]
    if not hits:
        active = sorted(str(p.get("name") or "") for p in raw if p.get("active", True))
        raise ToolError(
            f"No Timely project named {project!r}. Active projects: "
            + ", ".join(active[:20])
        )
    if len(hits) > 1:
        ids = ", ".join(str(p["id"]) for p in hits)
        raise ToolError(
            f"{len(hits)} Timely projects are named {project!r} (ids: {ids}); "
            "pass the id instead."
        )
    return int(hits[0]["id"]), str(hits[0].get("name") or "")


async def _entry_with_external_id(
    acc: int, me: int, day: str, external_id: str
) -> int | None:
    raw = (
        await session.get(
            f"/1.1/{acc}/users/{me}/events",
            params={"since": day, "upto": day, "per_page": 250},
        )
    ).json()
    for entry in raw:
        if str(entry.get("external_id") or "") == external_id:
            return int(entry["id"])
    return None


# -- tools --------------------------------------------------------------------

_READ_ONLY = dict(read_only_hint=True, idempotent_hint=True, open_world_hint=True)


@mcp.tool(
    tags={"time-tracking"},
    annotations=ToolAnnotations(title="Projects overview", **_READ_ONLY),
)
async def projects_overview(include_inactive: bool = False) -> ProjectsOverviewResult:
    """[timely] What's on my plate? Every project with client, logged hours,
    budget burn, and unbilled amounts, sorted by hours logged."""
    acc = await session.account_id()
    # ponytail: no pagination — fine up to 500 projects; page when someone has more
    raw = (await session.get(f"/1.1/{acc}/projects", params={"per_page": 500})).json()
    projects = []
    for p in raw:
        if not include_inactive and not p.get("active", True):
            continue
        projects.append(
            ProjectSummary(
                id=int(p["id"]),
                name=str(p.get("name") or ""),
                client=(p.get("client") or {}).get("name"),
                active=bool(p.get("active", True)),
                billable=bool(p.get("billable", False)),
                hours_logged=_hours(p.get("duration")),
                hours_formatted=_formatted(p.get("duration")),
                budget=float(p["budget"]) if p.get("budget") else None,
                budget_type=p.get("budget_type") or None,
                budget_percent=p.get("budget_percent"),
                unbilled_hours=_hours(p.get("unbilled_duration")),
                unbilled_cost=_money(p.get("unbilled_cost")),
                hour_rate=float(p["hour_rate"]) if p.get("hour_rate") else None,
            )
        )
    projects.sort(key=lambda p: p.hours_logged, reverse=True)
    total = sum(p.hours_logged for p in projects)
    scope = "incl. inactive" if include_inactive else "active only"
    summary = f"{len(projects)} projects ({scope}), {total:.1f}h logged overall."
    return ProjectsOverviewResult(
        summary=summary, count=len(projects), projects=projects
    )


_POST_GROUP_KEYS = {"label": "labels", "day": "days"}


@mcp.tool(
    tags={"time-tracking"},
    annotations=ToolAnnotations(title="Time spent", **_READ_ONLY),
)
async def time_spent(
    since: str,
    until: str,
    group_by: Literal["project", "client", "label", "day"] = "project",
) -> TimeSpentResult:
    """[timely] Where did my time go? Server-side rollups for a date range
    (YYYY-MM-DD), grouped by project, client, label, or day, with billable
    split. Totals are computed by Timely, not client-side. Day buckets follow
    the Timely account's timezone (a day's bucket equals the sum of that
    day's entries)."""
    acc = await session.account_id()
    if group_by in ("project", "client"):
        raw = (
            await session.get(
                f"/1.1/{acc}/reports", params={"since": since, "until": until}
            )
        ).json()
        if group_by == "client":
            slices = [_slice(e) for e in raw]
        else:
            slices = [_slice(p) for e in raw for p in (e.get("projects") or [])]
    else:
        me = await session.user_id()
        key = _POST_GROUP_KEYS[group_by]
        raw = (
            await session.post(
                f"/1.1/{acc}/reports/filter",
                json_body={
                    "since": since,
                    "until": until,
                    "group_by": [key],
                    "user_ids": [me],
                },
            )
        ).json()
        slices = [_slice(e) for e in (raw.get(key) or [])]
    slices = [s for s in slices if s.hours > 0]
    slices.sort(key=lambda s: s.hours, reverse=True)
    total = sum(s.hours for s in slices)
    billable = sum(s.billable_hours for s in slices)
    non_billable = sum(s.non_billable_hours for s in slices)
    top = f", top: {slices[0].name} ({slices[0].hours:.1f}h)" if slices else ""
    summary = (
        f"{total:.1f}h between {since} and {until} "
        f"({billable:.1f}h billable), grouped by {group_by}{top}."
    )
    return TimeSpentResult(
        summary=summary,
        since=since,
        until=until,
        group_by=group_by,
        total_hours=total,
        total_formatted=_fmt_hours(total),
        billable_hours=billable,
        non_billable_hours=non_billable,
        groups=slices,
    )


@mcp.tool(
    tags={"time-tracking"},
    annotations=ToolAnnotations(title="Work log", **_READ_ONLY),
)
async def work_log(since: str, upto: str) -> WorkLogResult:
    """[timely] What did I actually do? Individual entries with notes and
    labels for a date range (YYYY-MM-DD) — standup, diary, and invoicing raw
    material."""
    acc = await session.account_id()
    me = await session.user_id()
    # ponytail: no pagination — 250 entries covers weeks; page when a range overflows
    raw = (
        await session.get(
            f"/1.1/{acc}/users/{me}/events",
            params={"since": since, "upto": upto, "per_page": 250},
        )
    ).json()
    # Events carry label ids only; one lookup names them, skipped when unlabelled.
    names: dict[int, str] = {}
    if any(e.get("label_ids") for e in raw):
        labels = (await session.get(f"/1.1/{acc}/labels")).json()
        names = {
            int(lb["id"]): str(lb.get("name") or "") for lb in _flatten_labels(labels)
        }
    entries = [
        WorkLogEntry(
            day=str(e.get("day") or ""),
            hours=_hours(e.get("duration")),
            hours_formatted=_formatted(e.get("duration")),
            note=(e.get("note") or None),
            project=(e.get("project") or {}).get("name"),
            labels=[names.get(int(i), str(i)) for i in (e.get("label_ids") or [])],
            billable=bool(e.get("billable", False)),
            billed=bool(e.get("billed", False)),
            timer_running=str(e.get("timer_state") or "")
            in {"start", "started", "running"},
        )
        for e in raw
    ]
    entries.sort(key=lambda e: e.day)
    total = sum(e.hours for e in entries)
    summary = f"{len(entries)} entries, {total:.1f}h between {since} and {upto}."
    return WorkLogResult(
        summary=summary, since=since, upto=upto, total_hours=total, entries=entries
    )


@mcp.tool(
    tags={"time-tracking"},
    annotations=ToolAnnotations(
        title="Create time entry",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,  # repeat-safe when external_id is supplied
        open_world_hint=True,
    ),
)
async def create_entry(
    project: int | str,
    day: str,
    minutes: int,
    note: str,
    label: str | None = None,
    external_id: str | None = None,
    billable: bool | None = None,
) -> CreateEntryResult:
    """[timely] Log time I already spent. Creates one entry for the authorized
    user against `project` (a numeric id, or an exact project name) on `day`
    (YYYY-MM-DD), lasting `minutes`, with a `note` saying what the time went on.

    Pass `external_id` to make the call safe to repeat: if an entry on that day
    already carries it, nothing is created and `created` comes back false.
    `label` is the name of a label that already exists in Timely. `billable`
    defaults to the project's own setting.
    """
    # Validation is deliberate, not defensive padding: these entries price at a
    # real hourly rate on a client retainer, so a caller bug must not become an
    # invoice line.
    day = day.strip()
    if not _DAY_RE.match(day):
        raise ToolError(f"day must be YYYY-MM-DD, got {day!r}.")
    if minutes <= 0:
        raise ToolError(f"minutes must be positive, got {minutes}.")
    if minutes > _MAX_MINUTES:
        raise ToolError(
            f"minutes must be at most {_MAX_MINUTES} (24h) for one day, got "
            f"{minutes} — refusing to log an implausible duration."
        )
    note = note.strip()
    if not note:
        raise ToolError("note is required: a logged hour must say what it was for.")

    acc = await session.account_id()
    me = await session.user_id()
    project_id, project_name = await _resolve_project(acc, project)

    label_id: int | None = None
    label_name: str | None = None
    if label:
        label_id, label_name = await _resolve_label(acc, label)

    total_hours = minutes / 60
    formatted = _fmt_hours(total_hours)

    if external_id:
        already = await _entry_with_external_id(acc, me, day, external_id)
        if already is not None:
            return CreateEntryResult(
                summary=(
                    f"Nothing created: entry {already} on {day} already carries "
                    f"external_id {external_id!r}."
                ),
                created=False,
                entry_id=already,
                project_id=project_id,
                project=project_name,
                day=day,
                hours=total_hours,
                hours_formatted=formatted,
                label=label_name,
                note=note,
                external_id=external_id,
            )

    hours, mins = divmod(int(minutes), 60)
    event: dict[str, Any] = {
        "project_id": project_id,
        "user_id": me,
        "day": day,
        "hours": hours,
        "minutes": mins,
        "note": note,
    }
    if label_id is not None:
        event["label_ids"] = [label_id]
    if external_id:
        event["external_id"] = external_id
    if billable is not None:
        event["billable"] = billable

    raw = (await session.post(f"/1.1/{acc}/hours", json_body={"event": event})).json()
    where = project_name or (raw.get("project") or {}).get("name")
    tag = f", label {label_name}" if label_name else ""
    return CreateEntryResult(
        summary=f"Logged {formatted} to {where or f'project {project_id}'} "
        f"on {day}{tag}.",
        created=True,
        entry_id=int(raw["id"]) if raw.get("id") else None,
        project_id=project_id,
        project=where,
        day=day,
        hours=total_hours,
        hours_formatted=formatted,
        label=label_name,
        note=note,
        external_id=external_id,
    )


# -- health -------------------------------------------------------------------


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Public health endpoint. Does not call Timely — process liveness only."""
    return JSONResponse(
        {
            "status": "healthy",
            "service": "mcp-timely",
            "version": __version__,
            "uptime_seconds": int(
                (datetime.now(timezone.utc) - _start_time).total_seconds()
            ),
        }
    )


@mcp.custom_route("/healthz", methods=["GET"])
async def health_check_z(request: Request) -> JSONResponse:
    return await health_check(request)


# -- entry points -------------------------------------------------------------


def _bootstrap_auth() -> None:
    """One-time OOB authorization: prints the URL, takes the code, writes tokens."""
    import asyncio

    if (
        not settings.timely_client_id
        or not settings.timely_client_secret.get_secret_value()
    ):
        sys.exit(
            "Set TIMELY_CLIENT_ID and TIMELY_CLIENT_SECRET first (see .env.example)."
        )
    print("Open this URL, authorize, and paste the code Timely shows you:\n")
    print(f"  {session.authorize_url()}\n")
    code = input("Code: ").strip()
    asyncio.run(session.exchange_code(code))
    print(
        f"Tokens saved to {settings.timely_token_file}. No further interaction needed."
    )


def main() -> None:
    """Entry point for the mcp-timely server (or `mcp-timely auth` bootstrap)."""
    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        _bootstrap_auth()
        return
    if settings.transport == "http":
        mcp.run(
            transport="streamable-http",
            host=settings.host,
            port=settings.port,
            stateless_http=True,
        )
    else:
        mcp.run()


if __name__ == "__main__":
    main()
