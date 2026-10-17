"""Application wiring: lifecycle hooks, morning announce loop, and the
command registrations.  Command behaviour lives in commands.py; Telegram
output and list rendering live in messaging.py.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import date, datetime, time, timedelta, tzinfo

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)
from todoist_api_python.models import Project

from .commands import (
    AddCommand,
    AssignCommand,
    DoneCommand,
    DueCommand,
    EditCommand,
    HelpCommand,
    ListCommand,
    MineCommand,
    ProjectCommand,
    UnknownCommand,
    UndoCommand,
    command_callback,
    command_handler,
)
from .config import Config
from .messaging import (
    build_entities,
    chat_lock,
    is_group_chat_type,
    mentions_lookup,
    render_list_message,
    respond_safely,
    send_chunks,
)
from .state import TaskList, drop_locally_done, put_tasks, set_chat_list
from .todoist_client import DueTask, TodoistClient, TodoistError

logger = logging.getLogger(__name__)

_MORNING_TITLE = "<b>Daily Task Reminder</b>"


def next_announce(at: time, tz: tzinfo, now: datetime | None = None) -> datetime:
    """The next `at` wall-clock occurrence in `tz`, strictly after `now`.

    Returns the target instant (not a delta): the caller sleeps the
    timestamp difference (absolute time — DST-correct) and fires
    unconditionally on wake, because asyncio.sleep lands at or after
    the deadline and a re-derivation at the target would always roll
    to tomorrow.  At or after the moment the next occurrence is
    tomorrow (no catch-up — a bot that was down at the scheduled time
    stays quiet until the next morning).  `now` is injectable for
    tests; it must be aware.
    """
    if now is None:
        now = datetime.now(tz)
    else:
        now = now.astimezone(tz)
    target = datetime.combine(now.date(), at, tzinfo=tz)
    if target <= now:
        target += timedelta(days=1)
    return target


async def _send_morning_list(
    application: Application,
    chat_id: int,
    tasks: list[DueTask],
    today: date,
    cutoff: date,
    mentions: dict[str, str],
    todoist: TodoistClient,
    project_id: str,
    *,
    send_when_empty: bool,
) -> None:
    """Send the morning list to one chat and store it as that chat's list.

    Same rendering as /today under a "Daily Task Reminder" title; the
    message becomes the last shown list, so /done, /undo and /edit work
    on it immediately (the title survives strikes via the stored
    headline).  Sent to the chat root (no message_thread_id → the
    General topic in forum groups).  When nothing is due and
    send_when_empty is false (MORNING_ANNOUNCE_WHEN_EMPTY=false), no
    message is sent, but the chat's stored list is still cleared so
    yesterday's message stops updating.  Caller holds the chat lock.
    """
    chat_data = application.chat_data[chat_id]
    entities = await build_entities(tasks, todoist, project_id)
    # The fetch ran outside this lock (once per cycle, chat-independent);
    # a /done that landed since then wins — neither revert its record
    # nor re-render it as active in this message.
    fresh = drop_locally_done(chat_data, entities.values())
    entities = {task.task_id: task for task in fresh}
    put_tasks(chat_data, entities.values())
    chunks = render_list_message(
        list(entities),
        entities,
        today,
        mentions,
        show_dates=cutoff > today,
        cutoff=cutoff,
        headline=_MORNING_TITLE,
    )
    if not chunks:
        set_chat_list(chat_data, None)
        if send_when_empty:
            await application.bot.send_message(
                chat_id=chat_id,
                text=f"{_MORNING_TITLE}\n\nNothing due today 🎉",
                parse_mode=ParseMode.HTML,
            )
        return
    message_id = await send_chunks(
        lambda text: application.bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML
        ),
        chunks,
    )
    set_chat_list(
        chat_data,
        TaskList(
            message_id=message_id,
            day=today,
            cutoff=cutoff,
            task_ids=list(entities),
            headline=_MORNING_TITLE,
            show_dates=cutoff > today,
        ),
    )


async def _morning_loop(
    cfg: Config, project: Project, todoist: TodoistClient, application: Application
) -> None:
    """Send the day's list to every allowed group chat at the configured time.

    Started by post_init only when morning_announce_time is set; the
    task is cancelled by post_stop.  Each cycle re-derives the
    timezone (so a DST shift cannot freeze a stale fixed offset when
    TIMEZONE is unset) and sleeps until the next wall-clock target
    (timestamp math — DST-correct) and then fires
    unconditionally: asyncio.sleep lands at or after the deadline, so
    re-deriving at wake would always roll to tomorrow and never send.
    The Todoist fetch is chat-independent and runs once per cycle,
    outside any chat lock; only the send and state write are locked.
    An unexpected error in one cycle is logged and the schedule
    continues; a fetch failure notifies each chat so silence is never
    mistaken for "nothing due".  Chats whose type cannot be resolved
    are skipped (group chats only), and resolved types are cached for
    the process lifetime so get_chat runs once per chat.
    """
    at = cfg.morning_announce_time
    if at is None:
        return
    mentions = mentions_lookup(cfg)
    chat_types: dict[int, str] = {}
    logger.info(
        "morning list scheduled daily at %s (%s)",
        at.strftime("%H:%M"),
        cfg.resolved_timezone,
    )
    while True:
        # Re-derive each cycle: when TIMEZONE is unset,
        # resolved_timezone returns the server's *current* fixed
        # offset — captured once, it would keep the old offset across
        # a DST transition while cfg.today() moved on, drifting the
        # announce time by an hour until restart.
        tz = cfg.resolved_timezone
        target = next_announce(at, tz)
        await asyncio.sleep(max(0.0, target.timestamp() - datetime.now(tz).timestamp()))
        try:
            today = cfg.today()
            logger.debug("morning cycle start, %d chat(s)", len(cfg.allowed_chat_ids))
            try:
                tasks = await todoist.tasks_due_by(project.id, cutoff=today)
            except TodoistError:
                logger.warning("morning fetch failed")
                tasks = None
            # Chat-independent: warm the assignee-name cache once per
            # cycle, outside any chat lock (only the send and state
            # write are locked).  Failures degrade to blank assignees.
            await todoist.warm_collaborators(project.id)
            for chat_id in sorted(cfg.allowed_chat_ids):
                kind = chat_types.get(chat_id)
                if kind is None:
                    try:
                        resolved = await application.bot.get_chat(chat_id)
                    except TelegramError:
                        # Fail closed: "group chats only" also holds
                        # when the type cannot be determined.  Not
                        # cached — retried next cycle.
                        logger.warning(
                            "morning chat=%s: could not resolve chat type, " "skipping",
                            chat_id,
                        )
                        continue
                    kind = resolved.type.value
                    chat_types[chat_id] = kind
                if not is_group_chat_type(kind):
                    continue
                async with chat_lock(chat_id):
                    try:
                        if tasks is None:
                            logger.debug(
                                "morning chat=%s: sending fetch-failed notice", chat_id
                            )
                            with suppress(TelegramError):
                                await application.bot.send_message(
                                    chat_id=chat_id,
                                    text="⚠ couldn't fetch today's tasks — try /today",
                                )
                        else:
                            await _send_morning_list(
                                application,
                                chat_id,
                                tasks,
                                today,
                                today,
                                mentions,
                                todoist,
                                project.id,
                                send_when_empty=cfg.morning_announce_when_empty,
                            )
                            logger.debug("morning chat=%s: sent", chat_id)
                    except Exception:
                        # Contain one chat's failure so the remaining
                        # chats still get today's list.
                        logger.exception("morning send failed for chat %s", chat_id)
        except Exception:
            # Never let an unexpected error (bug, closed client at
            # shutdown) kill the schedule; CancelledError is a
            # BaseException and propagates for clean shutdown.
            logger.exception("morning announce cycle failed")


def build_application(
    cfg: Config, project: Project, todoist: TodoistClient
) -> Application:
    # NOTE: the Application is built without concurrent_updates, so PTB
    # processes updates strictly one at a time (SimpleUpdateProcessor,
    # max_concurrent_updates=1).  The list state is additionally guarded
    # by a per-chat asyncio.Lock because the morning-announce loop is a
    # genuinely concurrent writer; enabling concurrent_updates on top of
    # that is still untested — don't.
    auth = (
        filters.Chat(chat_id=sorted(cfg.allowed_chat_ids)) & ~filters.UpdateType.EDITED
    )
    morning_task: asyncio.Task[None] | None = None

    async def post_init(application: Application) -> None:
        nonlocal morning_task
        if cfg.morning_announce_time is not None:
            # Plain asyncio.create_task: Application.create_task before
            # start() warns and would register an infinite task the
            # application tried to await at stop(); we cancel it
            # ourselves in post_stop.
            morning_task = asyncio.create_task(
                _morning_loop(cfg, project, todoist, application),
                name="morning-announce",
            )

    async def post_stop(application: Application) -> None:
        # post_stop runs after stop() but before shutdown() closes the
        # bot's HTTP client, so a mid-send cancellation here cannot
        # turn into a RuntimeError from a closed client.
        if morning_task is not None:
            morning_task.cancel()
            try:
                await morning_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("morning task raised during shutdown")

    async def post_shutdown(application: Application) -> None:
        await todoist.close()

    async def on_error(
        update: object | None, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        logger.error("Unhandled exception", exc_info=context.error)
        if isinstance(update, Update):
            await respond_safely(cfg, update, "hard", "Unexpected error")

    application = (
        Application.builder()
        .token(cfg.telegram_token)
        .post_init(post_init)
        .post_stop(post_stop)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(
        command_handler(["help", "start"], HelpCommand(cfg, todoist, project), auth)
    )
    application.add_handler(
        command_handler("add", AddCommand(cfg, todoist, project), auth)
    )
    application.add_handler(
        command_handler(
            "today", ListCommand(cfg, todoist, project, 0, "cmd_today"), auth
        )
    )
    application.add_handler(
        command_handler(
            "tomorrow", ListCommand(cfg, todoist, project, 1, "cmd_tomorrow"), auth
        )
    )
    application.add_handler(
        command_handler("week", ListCommand(cfg, todoist, project, 6, "cmd_week"), auth)
    )
    application.add_handler(
        command_handler("mine", MineCommand(cfg, todoist, project), auth)
    )
    application.add_handler(
        command_handler("done", DoneCommand(cfg, todoist, project), auth)
    )
    application.add_handler(
        command_handler("undo", UndoCommand(cfg, todoist, project), auth)
    )
    application.add_handler(
        command_handler("edit", EditCommand(cfg, todoist, project), auth)
    )
    application.add_handler(
        command_handler("assign", AssignCommand(cfg, todoist, project), auth)
    )
    application.add_handler(
        command_handler("due", DueCommand(cfg, todoist, project), auth)
    )
    application.add_handler(
        command_handler("project", ProjectCommand(cfg, todoist, project), auth)
    )
    application.add_handler(
        MessageHandler(
            filters.COMMAND & auth,
            command_callback(UnknownCommand(cfg, todoist, project)),
        )
    )
    application.add_error_handler(on_error)
    return application
