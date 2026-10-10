from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass
from datetime import date, datetime
from typing import TypeVar

import requests
from todoist_api_python.api_async import TodoistAPIAsync
from todoist_api_python.models import Deadline, Due, Project, Task

from .config import ConfigError

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
    due: date


def _day(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


def effective_day(due: Due | None, deadline: Deadline | None) -> date | None:
    """The day a task counts for /today: the earlier of due date and deadline.

    Deadlines are date-only hard limits that never recur; a task can have
    both a due date and a deadline, and a passed deadline must not hide
    behind a later due date, so the earlier of the two wins.  None only
    when the task has neither.
    """
    days: list[date] = []
    if due is not None:
        days.append(_day(due.date))
    if deadline is not None:
        days.append(_day(deadline.date))
    return min(days) if days else None


class TodoistClient:
    def __init__(self, token: str) -> None:
        self._api = TodoistAPIAsync(token)

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

    async def resolve_project(self, project_id: str) -> Project:
        try:
            return await self._run(self._api.get_project(project_id))
        except TodoistAPIError as exc:
            if exc.status == 404:
                raise ConfigError(
                    f"project {project_id!r} not found — check " "TODOIST_PROJECT_ID"
                ) from exc
            raise TodoistError(f"could not load project {project_id!r}: {exc}") from exc
        except TodoistError as exc:
            raise TodoistError(f"could not load project {project_id!r}: {exc}") from exc

    async def tasks_due_by(self, project_id: str, cutoff: date) -> list[DueTask]:
        tasks = await self._pages(self._api.get_tasks(project_id=project_id, limit=200))
        due_tasks: list[DueTask] = []
        for task in tasks:
            day = effective_day(task.due, task.deadline)
            if day is None or day > cutoff:
                continue
            due_tasks.append(DueTask(task=task, due=day))
        due_tasks.sort(key=lambda item: (item.due, item.task.content.casefold()))
        return due_tasks

    async def add_task_in_project(self, text: str, project: Project) -> Task:
        # Quick-add only resolves /Section and +Assignee when the text
        # itself names a project, so prepend the configured project
        # token (before any trailing // description in the text).
        quick_text = f"{_project_token(project.name)} {text}"
        task = await self._run(self._api.add_task_quick(quick_text))
        if task.project_id == project.id:
            return task
        try:
            await self._run(self._api.move_task(task.id, project_id=project.id))
        except TodoistError:
            # Don't leave a stray Inbox task behind a reported failure:
            # roll back best-effort, then surface the original error.
            try:
                await self._run(self._api.delete_task(task.id))
            except TodoistError:
                pass
            raise
        return task

    async def get_task(self, task_id: str) -> Task:
        return await self._run(self._api.get_task(task_id))

    async def update_task_content(self, task_id: str, content: str) -> Task:
        return await self._run(self._api.update_task(task_id, content=content))

    async def complete_task(self, task_id: str) -> None:
        closed = await self._run(self._api.complete_task(task_id))
        if not closed:
            raise TodoistError("Todoist did not close the task")

    async def uncomplete_task(self, task_id: str) -> None:
        reopened = await self._run(self._api.uncomplete_task(task_id))
        if not reopened:
            raise TodoistError("Todoist did not reopen the task")

    async def _run(self, awaitable: Awaitable[T]) -> T:
        try:
            return await awaitable
        except requests.exceptions.HTTPError as exc:
            raise _api_error(exc) from exc
        except requests.exceptions.RequestException as exc:
            raise TodoistError("Could not reach the Todoist API") from exc

    async def _pages(self, awaitable: Awaitable[AsyncIterator[list[T]]]) -> list[T]:
        try:
            stream = await awaitable
            return [item async for page in stream for item in page]
        except requests.exceptions.HTTPError as exc:
            raise _api_error(exc) from exc
        except requests.exceptions.RequestException as exc:
            raise TodoistError("Could not reach the Todoist API") from exc


def _project_token(name: str) -> str:
    """Quick-add project token for a project name, e.g. #My\\ Project."""
    return "#" + name.replace("\\", "\\\\").replace(" ", "\\ ")


def _api_error(exc: requests.exceptions.HTTPError) -> TodoistAPIError:
    response = exc.response
    status = response.status_code if response is not None else "?"
    return TodoistAPIError(status)
