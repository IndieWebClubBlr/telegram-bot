from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from typing import TypeVar

import requests
from todoist_api_python.api_async import TodoistAPIAsync
from todoist_api_python.models import Deadline, Due, Project, Task

from .config import ConfigError

logger = logging.getLogger(__name__)

T = TypeVar("T")


class TodoistError(Exception):
    """Todoist API failure or an unresolvable project configuration."""


class TodoistAPIError(TodoistError):
    """Todoist API returned a non-success HTTP status."""

    def __init__(self, status: object) -> None:
        super().__init__(f"Todoist API error (HTTP {status})")
        self.status = status


@dataclass(frozen=True)
class DueTask:
    task: Task
    # None when the task has neither a due date nor a deadline (/mine's
    # "No date" section); tasks_due_by only yields dated tasks.
    due: date | None


def _day(value: date | datetime, tz: tzinfo) -> date:
    """Calendar day of a due/deadline value, honoring configured tz.

    A value that carries a timezone is an absolute instant: convert it
    into the configured zone and take that wall-clock day.  A naive
    datetime is already local (Todoist stores account-local wall
    clocks): assume it is in the configured zone and take the date
    as-is.  Plain dates pass through unchanged.
    """
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(tz)
        return value.date()
    return value


def effective_day(
    due: Due | None, deadline: Deadline | None, tz: tzinfo
) -> date | None:
    """The day a task counts for /today: the earlier of due date and deadline.

    Deadlines are date-only hard limits that never recur; a task can have
    both a due date and a deadline, and a passed deadline must not hide
    behind a later due date, so the earlier of the two wins.  None only
    when the task has neither.
    """
    days: list[date] = []
    if due is not None:
        days.append(_day(due.date, tz))
    if deadline is not None:
        days.append(_day(deadline.date, tz))
    return min(days) if days else None


class TodoistClient:
    def __init__(self, token: str, timezone: tzinfo) -> None:
        self._api = TodoistAPIAsync(token)
        self._timezone = timezone
        # Project collaborator id → name, fetched once on first need and
        # cached.  _collaborator_misses remembers ids that were looked
        # for in a *successful* refresh and still not found (e.g. a
        # removed collaborator), so a stale id cannot trigger a refetch
        # loop.  _collaborator_lock makes the fetch single-flight: the
        # morning-announce task can race a handler, and both must not
        # fetch the same list concurrently.
        self._collaborators: dict[str, str] | None = None
        self._collaborator_misses: set[str] = set()
        # Same one-shot-refresh bookkeeping for the reverse (name) lookup.
        self._collaborator_name_misses: set[str] = set()
        self._collaborator_lock = asyncio.Lock()

    async def close(self) -> None:
        # The async wrapper's __aexit__ is a docstring-only no-op; the
        # sync client's __exit__ actually closes the requests session
        # (idempotent via weakref.finalize). Guarded so a future SDK
        # rename cannot crash shutdown.
        closer = getattr(getattr(self._api, "_api", None), "__exit__", None)
        if closer is not None:
            closer(None, None, None)
        else:
            self._api.__aexit__(None, None, None)

    async def warm_collaborators(self, project_id: str) -> None:
        """Fetch the collaborator list now, unless it is already cached.

        Lets callers (the morning loop) do the chat-independent network
        work once per cycle outside any per-chat lock; later
        collaborator_name calls then hit the cache.
        """
        async with self._collaborator_lock:
            if self._collaborators is None:
                await self._refresh_collaborators(project_id)

    async def collaborator_name(self, project_id: str, assignee_id: str) -> str | None:
        """Display name for a Todoist assignee id, or None.

        The collaborator list is fetched once and cached; an id not in
        the cache triggers one refresh (a new collaborator), after which
        an id that is still missing is treated as unresolvable.  API
        failures are logged and degrade to None — assignee display is
        cosmetic and must never break list rendering — and the failed
        attempt is not recorded, so the next call retries.
        """
        async with self._collaborator_lock:
            if self._collaborators is None:
                await self._refresh_collaborators(project_id)
            if self._collaborators is None:
                # First fetch failed (offline, 403, rate limit, …) —
                # degrade to None, never crash the caller; the next
                # call retries.
                logger.debug("collaborator_name id=%s: cache unavailable", assignee_id)
                return None
            name = self._collaborators.get(assignee_id)
            if name is not None or assignee_id in self._collaborator_misses:
                logger.debug(
                    "collaborator_name id=%s: %s",
                    assignee_id,
                    "hit" if name is not None else "known-miss",
                )
                return name
            refreshed = await self._refresh_collaborators(project_id)
            name = self._collaborators.get(assignee_id)
            # Only a *successful* refresh that lacks the id retires it;
            # a failed refresh leaves the old cache in place, so the
            # "is not None" test would wrongly record the miss — check
            # the refresh result instead and retry on the next call.
            if refreshed:
                self._collaborator_misses.add(assignee_id)
            return name

    async def collaborator_id_for_name(self, project_id: str, name: str) -> str | None:
        """Collaborator id whose name casefold-matches `name`, or None.

        Shares the cached collaborator map with collaborator_name: a
        name not in the cache triggers one refresh (a collaborator
        added after the cache was built), after which a name still
        missing is treated as unresolvable — recorded only after a
        successful refresh, so an API failure retries on the next call.
        None also covers a failed initial fetch; the caller surfaces a
        "check ASSIGNEE_MENTIONS and retry" message either way.
        """
        async with self._collaborator_lock:
            if self._collaborators is None:
                await self._refresh_collaborators(project_id)
            if self._collaborators is None:
                logger.debug("collaborator_id_for_name: cache unavailable")
                return None
            folded = name.casefold()
            collaborator_id = self._id_by_folded_name(folded)
            if collaborator_id is not None or folded in self._collaborator_name_misses:
                logger.debug(
                    "collaborator_id_for_name: %s",
                    "hit" if collaborator_id is not None else "known-miss",
                )
                return collaborator_id
            refreshed = await self._refresh_collaborators(project_id)
            collaborator_id = (
                self._id_by_folded_name(folded)
                if self._collaborators is not None
                else None
            )
            # A failed refresh keeps the old cache, so the name may in
            # fact exist — only retire it after a refresh that really
            # succeeded and still lacks the name.
            if refreshed:
                self._collaborator_name_misses.add(folded)
            return collaborator_id

    def _id_by_folded_name(self, folded: str) -> str | None:
        assert self._collaborators is not None
        for collaborator_id, collaborator_name in self._collaborators.items():
            if collaborator_name.casefold() == folded:
                return collaborator_id
        return None

    async def _refresh_collaborators(self, project_id: str) -> bool:
        """Re-fetch the collaborator cache; True when it succeeded.

        On failure the previous cache (or None) is kept and the miss
        bookkeeping must not record anything — the next call retries.
        """
        try:
            collaborators = await self._pages(
                "get_collaborators", self._api.get_collaborators(project_id)
            )
        except Exception:
            # Catches everything the SDK or pagination can raise
            # (TodoistError, HTTPError, TypeError on an unexpected
            # response shape, …) — a cosmetic feature must never break
            # list rendering or the morning cycle.
            logger.exception("could not load project collaborators")
            return False
        self._collaborators = {c.id: c.name for c in collaborators}
        return True

    async def resolve_project(self, project_id: str) -> Project:
        try:
            return await self._run("get_project", self._api.get_project(project_id))
        except TodoistAPIError as exc:
            if exc.status == 404:
                raise ConfigError(
                    f"project {project_id!r} not found — check " "TODOIST_PROJECT_ID"
                ) from exc
            raise TodoistError(f"could not load project {project_id!r}: {exc}") from exc
        except TodoistError as exc:
            raise TodoistError(f"could not load project {project_id!r}: {exc}") from exc

    async def tasks_due_by(self, project_id: str, cutoff: date) -> list[DueTask]:
        tasks = await self._pages(
            "get_tasks", self._api.get_tasks(project_id=project_id, limit=200)
        )
        due_tasks: list[DueTask] = []
        for task in tasks:
            day = effective_day(task.due, task.deadline, self._timezone)
            if day is None or day > cutoff:
                continue
            due_tasks.append(DueTask(task=task, due=day))
        due_tasks.sort(key=lambda item: (item.due, item.task.content.casefold()))
        return due_tasks

    async def tasks_for_assignee(
        self, project_id: str, assignee_id: str, today: date
    ) -> tuple[list[DueTask], list[DueTask], list[DueTask]]:
        """All of an assignee's tasks: (due by today, future, undated).

        Due and upcoming are sorted by day then content; undated by
        content.  day is the same earlier-of-due-and-deadline value
        /today uses.
        """
        tasks = await self._pages(
            "get_tasks", self._api.get_tasks(project_id=project_id, limit=200)
        )
        due: list[DueTask] = []
        upcoming: list[DueTask] = []
        undated: list[DueTask] = []
        for task in tasks:
            if task.assignee_id != assignee_id:
                continue
            day = effective_day(task.due, task.deadline, self._timezone)
            item = DueTask(task=task, due=day)
            if day is None:
                undated.append(item)
            elif day <= today:
                due.append(item)
            else:
                upcoming.append(item)
        by_day = lambda item: (item.due or date.max, item.task.content.casefold())
        due.sort(key=by_day)
        upcoming.sort(key=by_day)
        undated.sort(key=lambda item: item.task.content.casefold())
        return due, upcoming, undated

    async def add_task_in_project(self, text: str, project: Project) -> Task:
        # Quick-add only resolves /Section and +Assignee when the text
        # itself names a project, so prepend the configured project
        # token (before any trailing // description in the text).
        quick_text = f"{_project_token(project.name)} {text}"
        task = await self._run("add_task_quick", self._api.add_task_quick(quick_text))
        if task.project_id == project.id:
            return task
        try:
            moved = await self._run(
                "move_task", self._api.move_task(task.id, project_id=project.id)
            )
        except TodoistError as exc:
            # Don't leave a stray Inbox task behind a reported failure:
            # roll back best-effort, then surface the original error.
            try:
                removed = await self._run("delete_task", self._api.delete_task(task.id))
            except TodoistError:
                removed = False
            if not removed:
                logger.warning(
                    "task %s was created in Inbox and could not be moved "
                    "or removed (move error: %s)",
                    task.id,
                    exc,
                )
                raise TodoistError(
                    f"task was created in Inbox but could not be moved or "
                    f"removed — move it manually (Todoist id {task.id})"
                ) from exc
            raise
        if not moved:
            raise TodoistError("Todoist did not move the task into the project")
        # move_task returns only a success flag; re-fetch so callers see
        # the task in its real (moved) project.
        return await self._run("get_task", self._api.get_task(task.id))

    async def get_task(self, task_id: str) -> Task:
        return await self._run("get_task", self._api.get_task(task_id))

    async def update_task_content(self, task_id: str, content: str) -> Task:
        return await self._run(
            "update_task", self._api.update_task(task_id, content=content)
        )

    async def complete_task(self, task_id: str) -> None:
        closed = await self._run("complete_task", self._api.complete_task(task_id))
        if not closed:
            raise TodoistError("Todoist did not close the task")

    async def uncomplete_task(self, task_id: str) -> None:
        reopened = await self._run(
            "uncomplete_task", self._api.uncomplete_task(task_id)
        )
        if not reopened:
            raise TodoistError("Todoist did not reopen the task")

    async def _run(self, label: str, awaitable: Awaitable[T]) -> T:
        """Await one SDK call, timing it for DEBUG.

        `label` is a fixed method name — never arguments, task text or
        collaborator names; ids are ok (correlation keys).
        """
        started = time.monotonic()
        try:
            result = await awaitable
        except requests.exceptions.HTTPError as exc:
            logger.debug(
                "todoist %s failed in %.0fms: HTTP %s",
                label,
                (time.monotonic() - started) * 1000,
                _status_of(exc),
            )
            raise _api_error(exc) from exc
        except requests.exceptions.RequestException as exc:
            logger.debug(
                "todoist %s failed in %.0fms: network",
                label,
                (time.monotonic() - started) * 1000,
            )
            raise TodoistError("Could not reach the Todoist API") from exc
        logger.debug(
            "todoist %s ok in %.0fms", label, (time.monotonic() - started) * 1000
        )
        return result

    async def _pages(
        self, label: str, awaitable: Awaitable[AsyncIterator[list[T]]]
    ) -> list[T]:
        started = time.monotonic()
        try:
            stream = await awaitable
            items = [item async for page in stream for item in page]
        except requests.exceptions.HTTPError as exc:
            logger.debug(
                "todoist %s failed in %.0fms: HTTP %s",
                label,
                (time.monotonic() - started) * 1000,
                _status_of(exc),
            )
            raise _api_error(exc) from exc
        except requests.exceptions.RequestException as exc:
            logger.debug(
                "todoist %s failed in %.0fms: network",
                label,
                (time.monotonic() - started) * 1000,
            )
            raise TodoistError("Could not reach the Todoist API") from exc
        logger.debug(
            "todoist %s ok in %.0fms (%d item(s))",
            label,
            (time.monotonic() - started) * 1000,
            len(items),
        )
        return items


def _project_token(name: str) -> str:
    """Quick-add project token for a project name, e.g. #My\\ Project."""
    return "#" + name.replace("\\", "\\\\").replace(" ", "\\ ")


def _status_of(exc: requests.exceptions.HTTPError) -> object:
    response = exc.response
    return response.status_code if response is not None else "?"


def _api_error(exc: requests.exceptions.HTTPError) -> TodoistAPIError:
    return TodoistAPIError(_status_of(exc))
