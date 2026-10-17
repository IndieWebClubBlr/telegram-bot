"""Shared fakes and builders for command tests (imported by test files).

Two seams make command handlers testable offline:

- TelegramBot (messaging.TelegramBot) — FakeTelegram is bound to a real
  Update/Message via set_bot(); Message.reply_text then routes to
  fake.send_message.  The real respond/send_list/reply stack runs
  unmodified.
- TodoistClient (todoist_client.TodoistClient) — FakeTodoist is injected
  as the (cfg, todoist, project) constructor arg.

The autouse no-network fixture lives in conftest.py; this module holds
only the fakes and builders so test files import them explicitly rather
than relying on conftest's import path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

from telegram import Chat, Message, Update, User
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes
from todoist_api_python.models import Project
from todoist_api_python.models import Task as SdkTask

from iwcb_bot.config import Config
from iwcb_bot.messaging import TelegramBot
from iwcb_bot.state import Task, TaskList, put_tasks, set_chat_list
from iwcb_bot.todoist_client import DueTask, TodoistClient

# A plausible-looking but fake token (matches Telegram's digits:chars
# shape so Application.builder().token() accepts it offline).
DUMMY_TG_TOKEN = "1000000000:" + "A" * 35

PROJECT = cast(
    Project, SimpleNamespace(id="p1", name="IWCB", url="https://todoist.com/p1")
)


def make_config(**over: Any) -> Config:
    """A fully-invalid-but-well-shaped config; override per test.

    The default ASSIGNEE_MENTIONS is a *valid* mapping (unique targets).
    Tests that need a duplicate target (which Config.from_env rejects)
    must build that config inline — see the assign-ambiguity test.
    """
    base: dict[str, Any] = dict(
        telegram_token=DUMMY_TG_TOKEN,
        todoist_token="invalid-todoist-token-for-tests",
        project_id="p1",
        allowed_chat_ids=frozenset({1}),
        timezone=timezone.utc,
        morning_announce_time=None,
        assignee_mentions={"Priya Singh": "@priya"},
        log_level="ERROR",
        morning_announce_when_empty=False,
        reaction_replies=False,
    )
    base.update(over)
    return Config(**base)


class FakeTelegram(TelegramBot):
    """Records every send-side call; never touches the network."""

    def __init__(self, username: str = "testbot") -> None:
        self._username = username
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._fail_exc: TelegramError | None = None
        self._fail_remaining: int = 0
        self._next_message_id = 1000
        # Only successfully-delivered payloads (a call that raised is
        # recorded in `calls` but not here).
        self._delivered_texts: list[str] = []
        self._delivered_edits: list[str] = []
        self._delivered_reactions: list[Any] = []

    @property
    def username(self) -> str:
        return self._username

    def fail_calls(self, exc: TelegramError, times: int = 1) -> None:
        """Fail the next `times` send-side calls with `exc`, then succeed.

        Lets a test exercise command_callback's TelegramError path (the
        first send fails → "⚠ Telegram send failed") and respond_safely's
        own swallow (every send, including the fallback, fails).
        """
        self._fail_exc = exc
        self._fail_remaining = times

    def _record(self, method: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((method, kwargs))
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            exc = self._fail_exc
            if self._fail_remaining == 0:
                self._fail_exc = None
            if exc is not None:
                raise exc

    def sent_texts(self) -> list[str]:
        return list(self._delivered_texts)

    def reactions(self) -> list[Any]:
        return list(self._delivered_reactions)

    def edits(self) -> list[str]:
        return list(self._delivered_edits)

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Message:
        self._record("send_message", {"chat_id": chat_id, "text": text, **kwargs})
        self._delivered_texts.append(text)
        self._next_message_id += 1
        return Message(
            message_id=self._next_message_id,
            date=datetime.now(timezone.utc),
            chat=Chat(id=chat_id, type=ChatType.SUPERGROUP),
        )

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, **kwargs: Any
    ) -> None:
        self._record(
            "edit_message_text",
            {"chat_id": chat_id, "message_id": message_id, "text": text, **kwargs},
        )
        self._delivered_edits.append(text)

    async def set_message_reaction(
        self, chat_id: int, message_id: int, reaction: Any, **kwargs: Any
    ) -> bool:
        self._record(
            "set_message_reaction",
            {"chat_id": chat_id, "message_id": message_id, "reaction": reaction},
        )
        self._delivered_reactions.append(reaction)
        return True

    async def send_chat_action(self, chat_id: int, action: str, **kwargs: Any) -> bool:
        self._record("send_chat_action", {"chat_id": chat_id, "action": action})
        return True


class FakeTodoist(TodoistClient):
    """Records every Todoist call; canned returns, no network."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.raise_on: dict[str, Exception] = {}
        self.add_task_result: Any = None
        self.due_by_result: list[DueTask] = []
        self.assignee_result: tuple[list[DueTask], list[DueTask], list[DueTask]] = (
            [],
            [],
            [],
        )
        self.collab_ids: dict[str, str] = {}
        self.collab_names: dict[str, str] = {}
        self.get_task_result: Any = None
        self.update_task_due_result: Any = None
        self.resolve_project_result: Any = PROJECT

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, *args))
        if name in self.raise_on:
            raise self.raise_on[name]

    def method_calls(self, name: str) -> list[tuple[Any, ...]]:
        return [c for c in self.calls if c[0] == name]

    async def close(self) -> None:
        self._record("close")

    async def warm_collaborators(self, project_id: str) -> None:
        self._record("warm_collaborators", project_id)

    async def collaborator_name(self, project_id: str, assignee_id: str) -> str | None:
        self._record("collaborator_name", project_id, assignee_id)
        return self.collab_names.get(assignee_id)

    async def collaborator_id_for_name(self, project_id: str, name: str) -> str | None:
        self._record("collaborator_id_for_name", project_id, name)
        return self.collab_ids.get(name)

    async def resolve_project(self, project_id: str) -> Project:
        self._record("resolve_project", project_id)
        return cast(Project, self.resolve_project_result)

    async def tasks_due_by(self, project_id: str, cutoff: Any) -> list[DueTask]:
        self._record("tasks_due_by", project_id, str(cutoff))
        return list(self.due_by_result)

    async def tasks_for_assignee(
        self, project_id: str, assignee_id: str, today: Any
    ) -> tuple[list[DueTask], list[DueTask], list[DueTask]]:
        self._record("tasks_for_assignee", project_id, assignee_id, str(today))
        return self.assignee_result

    async def add_task_in_project(self, text: str, project: Project) -> Any:
        self._record("add_task_in_project", text, project.id)
        return self.add_task_result

    async def get_task(self, task_id: str) -> Any:
        self._record("get_task", task_id)
        return self.get_task_result

    async def update_task_content(self, task_id: str, content: str) -> Any:
        self._record("update_task_content", task_id, content)
        return None

    async def update_task_assignee(self, task_id: str, assignee_id: str) -> Any:
        self._record("update_task_assignee", task_id, assignee_id)
        return None

    async def update_task_due(self, task_id: str, due_string: str) -> Any:
        self._record("update_task_due", task_id, due_string)
        return self.update_task_due_result

    async def complete_task(self, task_id: str) -> None:
        self._record("complete_task", task_id)

    async def uncomplete_task(self, task_id: str) -> None:
        self._record("uncomplete_task", task_id)


class Fakes:
    """A wired update/context pair with their bound FakeTelegram."""

    def __init__(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, bot: FakeTelegram
    ) -> None:
        self.update = update
        self.context = context
        self.bot = bot


def _close_and_return_future(coro: Coroutine[Any, Any, Any], name: str = "") -> Any:
    """Stand-in for Application.create_task: swallow the typing ticker so
    no send_chat_action noise is recorded and nothing is scheduled."""
    coro.close()
    fut = asyncio.get_event_loop().create_future()
    fut.set_result(None)
    return fut


def make_update(
    text: str = "/cmd",
    *,
    chat_id: int = 1,
    chat_type: str = "supergroup",
    user_id: int | None = 42,
    username: str | None = None,
    bot: FakeTelegram | None = None,
    no_message: bool = False,
) -> tuple[Update, FakeTelegram]:
    bot = bot or FakeTelegram()
    chat = Chat(id=chat_id, type=cast(Any, chat_type))
    from_user = (
        None
        if user_id is None
        else User(id=user_id, is_bot=False, first_name="Tester", username=username)
    )
    message = (
        None
        if no_message
        else Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=chat,
            from_user=from_user,
            text=text,
        )
    )
    update = Update(update_id=1, message=message)
    update.set_bot(cast(Any, bot))
    if message is not None:
        message.set_bot(cast(Any, bot))
    return update, bot


def make_context(
    args: list[str] | None = None,
    *,
    chat_data: dict[str, Any] | None = None,
    bot: FakeTelegram | None = None,
) -> ContextTypes.DEFAULT_TYPE:
    bot = bot or FakeTelegram()
    ctx = SimpleNamespace(
        args=list(args or []),
        chat_data=chat_data if chat_data is not None else {},
        bot=bot,
        application=SimpleNamespace(create_task=_close_and_return_future),
    )
    return cast(ContextTypes.DEFAULT_TYPE, ctx)


def build_fakes(
    *,
    args: list[str] | None = None,
    text: str = "/cmd",
    chat_data: dict[str, Any] | None = None,
    chat_id: int = 1,
    chat_type: str = "supergroup",
    user_id: int | None = 42,
    username: str | None = None,
    no_message: bool = False,
    bot: FakeTelegram | None = None,
) -> Fakes:
    bot = bot or FakeTelegram()
    update, bot = make_update(
        text,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=user_id,
        username=username,
        bot=bot,
        no_message=no_message,
    )
    context = make_context(args, chat_data=chat_data, bot=bot)
    return Fakes(update, context, bot)


def seed_chat_list(
    chat_data: dict[str, Any],
    tasks: dict[str, Task],
    *,
    day: Any = None,
    cutoff: Any = None,
    message_id: int = 7,
) -> None:
    """Put tasks in the chat map and register today's chat-level list."""
    today = day or make_config().today()
    put_tasks(chat_data, tasks.values())
    set_chat_list(
        chat_data,
        TaskList(
            message_id=message_id,
            day=today,
            cutoff=cutoff or today,
            task_ids=list(tasks),
        ),
    )


def make_state_task(
    task_id: str = "5c",
    text: str = "buy milk",
    *,
    due: Any = None,
    done: bool = False,
    recurring: bool = False,
    assignee: str | None = None,
) -> Task:
    return Task(
        task_id=task_id,
        text=text,
        due=due,
        done=done,
        recurring=recurring,
        assignee=assignee,
    )


def due_task(
    task_id: str = "t1",
    content: str = "buy milk",
    *,
    due_date: date | None = None,
    is_recurring: bool = False,
    assignee_id: str | None = None,
    deadline: date | None = None,
) -> DueTask:
    """A DueTask wrapping an SDK-shaped stub (what send_list expects)."""
    due = (
        None
        if due_date is None
        else SimpleNamespace(date=due_date, is_recurring=is_recurring)
    )
    sdk = SimpleNamespace(
        id=task_id,
        content=content,
        due=due,
        deadline=deadline,
        assignee_id=assignee_id,
        project_id="p1",
    )
    return DueTask(task=cast(SdkTask, sdk), due=due_date)


def sdk_task(
    task_id: str = "t1",
    content: str = "buy milk",
    *,
    due_date: date | None = None,
    is_recurring: bool = False,
    assignee_id: str | None = None,
    deadline: date | None = None,
) -> Any:
    """An SDK-shaped task stub (id/content/due/deadline/assignee_id)."""
    due = (
        None
        if due_date is None
        else SimpleNamespace(date=due_date, is_recurring=is_recurring)
    )
    return SimpleNamespace(
        id=task_id,
        content=content,
        due=due,
        deadline=deadline,
        assignee_id=assignee_id,
        project_id="p1",
    )


def run(cmd: Any, fakes: Fakes) -> None:
    """Drive a command through its guarded callback in a fresh loop."""
    from iwcb_bot.commands import command_callback

    callback = command_callback(cmd)
    asyncio.run(callback(fakes.update, fakes.context))
