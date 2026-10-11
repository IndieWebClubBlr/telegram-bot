from __future__ import annotations

import asyncio
import functools
import html
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Coroutine, MutableMapping
from contextlib import suppress
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any

from markdown_it import MarkdownIt
from telegram import Message, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from todoist_api_python.models import Project

from .config import Config
from .state import (
    ListItem,
    ListState,
    active_states,
    all_mine_states,
    any_done,
    displayed_matches,
    get_chat_state,
    resolve_ids,
    set_chat_state,
    set_mine_state,
    sync_item,
    take_mine,
    task_suffixes,
    todoist_name_for_user,
)
from .todoist_client import DueTask, TodoistClient, TodoistError, effective_day

logger = logging.getLogger(__name__)

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, None]]

_MESSAGE_LIMIT = 4000
_TELEGRAM_LIMIT = 4096
_DISPLAY_LIMIT = 500
_BODY_LIMIT = 3500
_TYPING_INTERVAL = 4.0
_NBSP = "\N{NO-BREAK SPACE}"
_LIST_CMDS = "/today, /tomorrow or /week"
_MORNING_TITLE = "<b>Daily Task Reminder</b>"
_MINE_TITLE = "<b>Your tasks</b>"
_MINE_LIMIT = 10

# Every read-modify-write of a chat's list state holds this chat's lock:
# updates are processed sequentially, so the only concurrent writer is
# the morning-announce loop.  Locks bind to the running event loop on
# first use; the bot runs a single loop for its whole lifetime.
_chat_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def _chat_lock(chat_id: int) -> asyncio.Lock:
    return _chat_locks[chat_id]


_md = MarkdownIt("gfm-like", {"html": False}).disable("image")
# Telegram only accepts http(s) and tg links; markdown-it's default
# validator would also emit mailto:, data: and relative hrefs.
_md.validateLink = lambda url: url.startswith(("http://", "https://", "tg://"))


def _next_announce(at: time, tz: tzinfo, now: datetime | None = None) -> datetime:
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


_HELP = """\
Commands (project: {project}):

/add <text> — add a task, quick-add syntax: "buy milk tomorrow p1 @chore /regular +Abhinav"
/today — tasks due today or overdue, each with a short id
/tomorrow — tasks due by end of tomorrow
/week — tasks due by end of the next 6 days
/mine — your tasks (due, then upcoming, then undated; max 10)
/done <id> — complete the task with that id (from the last shown list)
/undo <id> — reopen a task completed via /done (not recurring)
/edit <id> <new text> — edit the task with that id
/project — link to this project in Todoist
/start or /help — this message

- ids are shown in lowercase and matched case-insensitively
- {list_cmds} or /mine show lists that /done, /undo and /edit act on;
  edits appear in those messages only — refresh to re-fetch
- /done, /undo and /edit sync across your /mine list and the chat list
- /add refreshes the shown list when the new task falls inside its range
""".replace("{list_cmds}", _LIST_CMDS)


def guarded(handler: Handler, *, typing: bool = False) -> Handler:
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        ticker: asyncio.Task[None] | None = None
        try:
            if typing:
                ticker = await _start_typing(update, context)
            await handler(update, context)
        except TodoistError as exc:
            logger.warning("%s failed: %s", handler.__name__, exc)
            await _reply_safely(update, f"⚠ {exc}")
        except TelegramError:
            logger.exception("Telegram error in %s", handler.__name__)
            await _reply_safely(
                update,
                f"⚠ Telegram send failed — the command may have "
                f"completed; check with {_LIST_CMDS} or /mine",
            )
        except Exception:
            logger.exception("Unhandled error in %s", handler.__name__)
            await _reply_safely(update, "Unexpected error")
        finally:
            await _stop_typing(ticker)

    return wrapper


async def _start_typing(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> asyncio.Task[None] | None:
    """Keep Telegram's "typing…" indicator alive while a command runs.

    Telegram drops the action after ~5 seconds, so it must be re-sent;
    failures (channels, rate limits) are logged and never disturb the
    command itself.  In forum groups the action is pinned to the topic the
    command came from so the status shows where the reply will land.
    """
    chat = update.effective_chat
    if chat is None:
        return None
    message = update.effective_message
    thread_id: int | None = None
    if chat.is_forum:
        # General-topic messages arrive without message_thread_id; Telegram
        # uses 1 internally for the General topic.
        thread_id = (message.message_thread_id if message else None) or 1

    async def ticker() -> None:
        while True:
            try:
                await context.bot.send_chat_action(
                    chat_id=chat.id,
                    action=ChatAction.TYPING,
                    message_thread_id=thread_id,
                )
            except TelegramError as exc:
                logger.debug("chat action failed: %s", exc)
            await asyncio.sleep(_TYPING_INTERVAL)

    # application.create_task (not asyncio.create_task) so shutdown
    # waits for the ticker; the name aids debugging in task dumps.
    return context.application.create_task(ticker(), name=f"typing:{chat.id}")


async def _stop_typing(ticker: asyncio.Task[None] | None) -> None:
    if ticker is None:
        return
    ticker.cancel()
    with suppress(asyncio.CancelledError):
        await ticker


async def _reply(update: Update, text: str) -> None:
    message = update.effective_message
    if message is not None:
        await message.reply_text(text)


async def _reply_safely(update: Update, text: str = "Unexpected error") -> None:
    try:
        await _reply(update, text)
    except TelegramError:
        logger.exception("could not send reply")


async def _reply_html(update: Update, text: str) -> None:
    message = update.effective_message
    if message is not None:
        await message.reply_text(text, parse_mode=ParseMode.HTML)


def _md_html(text: str) -> str:
    # Collapsing whitespace first kills markdown hard breaks (`<br />`,
    # which Telegram rejects) and keeps output on one line.
    return _md.renderInline(" ".join(text.split()))


def _render_text(raw: str) -> str:
    """Markdown → Telegram HTML, capped so the message stays sendable.

    Percent-encoding of URLs inside links can inflate a 500-char title
    ~12x; plain html.escape is bounded at 6x, so fall back to it when
    the rendered form grows too large.
    """
    clean = _one_line(raw)
    out = _md_html(clean)
    if len(out) > _BODY_LIMIT:
        return html.escape(clean)
    return out


def _fmt_due(day: date, reference: date) -> str:
    """`Tue 9 Jun`, or `Tue 9 Jun 25` when the year differs from the
    reference (usually the day the list was built for). Near-today lists
    stay compact; old overdue items and year-boundary ranges disambiguate.
    """
    formatted = f"{day.strftime('%a')} {day.day} {day.strftime('%b')}"
    if day.year != reference.year:
        formatted += f" {day.strftime('%y')}"
    return formatted


def _one_line(text: str, limit: int = _DISPLAY_LIMIT) -> str:
    """Collapse to a single line and cap the length.

    Single-line input guarantees every rendered line is a self-contained,
    balanced HTML fragment, so chunking on newlines can never split a tag.
    The cap keeps the worst case (escaped entities) under the chunk limit.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) > limit:
        return collapsed[: limit - 1] + "…"
    return collapsed


def _is_divider_line(line: str) -> bool:
    """A rendered /mine section header (`<b>…</b>` on its own line)."""
    return line.startswith("<b>") and line.endswith("</b>")


def _chunk(text: str) -> list[str]:
    if len(text) <= _MESSAGE_LIMIT:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if current and len(current) + 1 + len(line) > _MESSAGE_LIMIT:
            # A section divider as the last line of a message would
            # label the *next* message's tasks — pull it down with the
            # first line of its section when that still fits.
            last = current.rsplit("\n", 1)[-1]
            head = current[: current.rfind("\n")] if "\n" in current else ""
            if (
                head
                and _is_divider_line(last)
                and len(last) + 1 + len(line) <= _MESSAGE_LIMIT
            ):
                chunks.append(head)
                current = f"{last}\n{line}"
            else:
                chunks.append(current)
                current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    # _one_line keeps every line well under the limit; the assert guards
    # against future render changes reintroducing oversized chunks.
    assert all(len(chunk) <= _MESSAGE_LIMIT for chunk in chunks)
    return chunks


def _mentions_lookup(cfg: Config) -> dict[str, str]:
    """Casefolded Todoist-name → Telegram mention target mapping."""
    return {name.casefold(): target for name, target in cfg.assignee_mentions.items()}


def _assignee_suffix(name: str | None, mentions: dict[str, str]) -> str:
    """Line suffix for an assignee: mention, handle, or plain name.

    mentions maps casefolded Todoist collaborator names to a Telegram
    @handle (auto-linked by Telegram) or a numeric user id (rendered as
    a clickable tg:// mention).  Unmapped names render as escaped plain
    text so ownership stays visible; unassigned tasks get nothing.
    """
    if name is None:
        return ""
    target = mentions.get(name.casefold())
    if target is None:
        return f" — {html.escape(name)}"
    if target.startswith("@"):
        return f" — {target}"
    return f' — <a href="tg://user?id={target}">{html.escape(name)}</a>'


def _render_list(
    items: list[ListItem],
    today: date,
    *,
    show_dates: bool,
    mentions: dict[str, str] | None = None,
    dividers: dict[int, str] | None = None,
) -> str:
    """Render the task list body (no headline).

    With show_dates (used by /tomorrow, /week and /mine) every line
    carries its due date so multi-day lists are readable; /today only
    dates overdue lines.  Done items keep their date in multi-day lists
    too — the strike-through already marks them.  Assignees (resolved
    at fetch time) render between the task text and the due suffix.
    dividers maps an item index to a bold section header printed above
    that line (/mine's Due/Upcoming/No date sections).
    """
    if mentions is None:
        mentions = {}
    suffixes = task_suffixes([item.task_id for item in items])
    width = max((len(suffix) for suffix in suffixes), default=0)
    lines: list[str] = []
    for index, (suffix, item) in enumerate(zip(suffixes, items, strict=True)):
        if dividers and index in dividers:
            lines.append(f"<b>{dividers[index]}</b>")
        overdue = item.due is not None and item.due < today and not item.done
        marker = "⚠ " if overdue else ""
        body = _render_text(item.text)
        if item.done:
            body = f"<s>{body}</s>"
        padded = suffix.ljust(width, _NBSP)
        line = (
            f"<code>[{padded}]</code>  {marker}{body}"
            f"{_assignee_suffix(item.assignee, mentions)}"
        )
        if item.due is not None and (show_dates or overdue):
            line += f" — due {_fmt_due(item.due, today)}"
        lines.append(line)
    return "\n".join(lines)


async def _build_items(
    tasks: list[DueTask], todoist: TodoistClient, project_id: str
) -> list[ListItem]:
    """State items for fetched tasks, resolving assignee display names.

    Assignee resolution goes through the client's cached collaborator
    map (fetched once, refreshed on unknown ids) and never raises — a
    failed lookup leaves the assignee blank.
    """
    items: list[ListItem] = []
    for task in tasks:
        assignee: str | None = None
        if task.task.assignee_id:
            assignee = await todoist.collaborator_name(
                project_id, task.task.assignee_id
            )
        items.append(
            ListItem(
                task_id=task.task.id,
                text=task.task.content,
                due=task.due,
                recurring=bool(task.task.due and task.task.due.is_recurring),
                assignee=assignee,
            )
        )
    return items


def _render_list_message(
    items: list[ListItem],
    today: date,
    mentions: dict[str, str],
    *,
    show_dates: bool,
    cutoff: date | None = None,
    headline: str | None = None,
    dividers: dict[int, str] | None = None,
) -> list[str]:
    """Render state items into sendable chunks.

    cutoff (when given) prefixes the "Due by <date>:" header; /mine
    passes None and uses dividers for its sections instead.  Returns []
    for an empty list — the caller decides the empty reply.
    """
    if not items:
        return []
    body = _render_list(
        items, today, show_dates=show_dates, mentions=mentions, dividers=dividers
    )
    if cutoff is not None:
        body = f"<b>Due by {_fmt_due(cutoff, today)}:</b>\n{body}"
    if headline is not None:
        body = f"{headline}\n\n{body}"
    return _chunk(body)


async def _send_chunks(
    send: Callable[[str], Awaitable[Message]], chunks: list[str]
) -> int | None:
    """Send rendered chunks via `send`; message id when single-chunk.

    Single-chunk lists get an editable message id; multi-chunk lists
    stay un-edited (the first message alone would lose its siblings).
    """
    first = await send(chunks[0])
    for chunk in chunks[1:]:
        await send(chunk)
    return first.message_id if len(chunks) == 1 else None


async def _send_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    tasks: list[DueTask],
    today: date,
    cutoff: date,
    mentions: dict[str, str],
    todoist: TodoistClient,
    project_id: str,
    headline: str | None = None,
) -> None:
    """Send the rendered list, optionally under an HTML headline line.

    Stores the list state so /done, /undo and /edit act on it.
    """
    message = update.effective_message
    if message is None:
        return
    show_dates = cutoff > today
    items = await _build_items(tasks, todoist, project_id)
    chunks = _render_list_message(
        items, today, mentions, show_dates=show_dates, cutoff=cutoff, headline=headline
    )
    if not chunks:
        set_chat_state(context.chat_data, None)
        if headline is not None:
            await _reply_html(update, headline)
        elif cutoff > today:
            await _reply(update, f"Nothing due by {_fmt_due(cutoff, today)} 🎉")
        else:
            await _reply(update, "Nothing due today 🎉")
        return
    message_id = await _send_chunks(
        lambda text: message.reply_text(text, parse_mode=ParseMode.HTML), chunks
    )
    set_chat_state(
        context.chat_data,
        ListState(
            message_id=message_id,
            day=today,
            cutoff=cutoff,
            items=items,
            headline=headline,
            show_dates=show_dates,
        ),
    )


async def _edit_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: ListState,
    mentions: dict[str, str],
) -> None:
    """Re-render the stored list message from its state.

    Renders with state.day — not the current clock — so a strike applied
    just after midnight still draws against the day the list was built
    for, and the message the user is looking at stays consistent.
    """
    if state.message_id is None:
        return
    chat = update.effective_chat
    if chat is None:
        return
    today = state.day
    rendered = _render_list(
        state.items,
        today,
        show_dates=state.show_dates or state.cutoff > today,
        mentions=mentions,
        dividers=state.dividers,
    )
    if state.cutoff > today:
        rendered = f"<b>Due by {_fmt_due(state.cutoff, today)}:</b>\n{rendered}"
    if state.headline is not None:
        rendered = f"{state.headline}\n\n{rendered}"
    if len(rendered) > _TELEGRAM_LIMIT:
        logger.warning("rendered list is %d chars, skipping edit", len(rendered))
        return
    try:
        await context.bot.edit_message_text(
            chat_id=chat.id,
            message_id=state.message_id,
            text=rendered,
            parse_mode=ParseMode.HTML,
        )
    except BadRequest as exc:
        if "not modified" in str(exc):
            logger.debug("list message already up to date")
        elif "message to edit not found" in str(exc).casefold():
            # User deleted the list message; stop editing it and stop
            # spamming exception logs on every strike.
            logger.debug("list message deleted; clearing edit target")
            state.message_id = None
        else:
            logger.exception("could not edit list message")
    except TelegramError:
        logger.exception("could not edit list message")


async def _edit_states(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    states: list[ListState],
    mentions: dict[str, str],
) -> None:
    """Re-edit every message backing `states` (chat list + /mine lists)."""
    for state in states:
        await _edit_list(update, context, state, mentions)


async def _send_morning_list(
    application: Application,
    chat_id: int,
    tasks: list[DueTask],
    today: date,
    cutoff: date,
    mentions: dict[str, str],
    todoist: TodoistClient,
    project_id: str,
) -> None:
    """Send the morning list to one chat and store it as that chat's state.

    Same rendering as /today under a "Daily Task Reminder" title; the
    message becomes the last shown list, so /done, /undo and /edit work
    on it immediately (the title survives strikes via the stored
    headline).  Sent to the chat root (no message_thread_id → the
    General topic in forum groups).  Caller holds the chat lock.
    """
    chat_data = application.chat_data[chat_id]
    items = await _build_items(tasks, todoist, project_id)
    chunks = _render_list_message(
        items,
        today,
        mentions,
        show_dates=cutoff > today,
        cutoff=cutoff,
        headline=_MORNING_TITLE,
    )
    if not chunks:
        set_chat_state(chat_data, None)
        await application.bot.send_message(
            chat_id=chat_id,
            text=f"{_MORNING_TITLE}\n\nNothing due today 🎉",
            parse_mode=ParseMode.HTML,
        )
        return
    message_id = await _send_chunks(
        lambda text: application.bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML
        ),
        chunks,
    )
    set_chat_state(
        chat_data,
        ListState(
            message_id=message_id,
            day=today,
            cutoff=cutoff,
            items=items,
            headline=_MORNING_TITLE,
            show_dates=cutoff > today,
        ),
    )


async def _morning_loop(
    cfg: Config, project: Project, todoist: TodoistClient, application: Application
) -> None:
    """Send the day's list to every allowed chat at the configured time.

    Started by post_init only when morning_announce_time is set; the
    task is cancelled by post_stop.  Each cycle sleeps until the next
    wall-clock target (timestamp math — DST-correct) and then fires
    unconditionally: asyncio.sleep lands at or after the deadline, so
    re-deriving at wake would always roll to tomorrow and never send.
    The Todoist fetch is chat-independent and runs once per cycle,
    outside any chat lock; only the send and state write are locked.
    An unexpected error in one cycle is logged and the schedule
    continues; a fetch failure notifies each chat so silence is never
    mistaken for "nothing due".
    """
    at = cfg.morning_announce_time
    if at is None:
        return
    tz = cfg.resolved_timezone
    mentions = _mentions_lookup(cfg)
    logger.info("morning list scheduled daily at %s (%s)", at.strftime("%H:%M"), tz)
    while True:
        target = _next_announce(at, tz)
        await asyncio.sleep(max(0.0, target.timestamp() - datetime.now(tz).timestamp()))
        try:
            today = cfg.today()
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
                async with _chat_lock(chat_id):
                    try:
                        if tasks is None:
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
                            )
                    except TelegramError:
                        logger.warning("morning send failed for chat %s", chat_id)
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
    mentions = _mentions_lookup(cfg)

    async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _reply(update, _HELP.format(project=project.name))

    async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if chat is None:
            return
        text = " ".join(context.args or []).strip()
        if not text:
            await _reply(
                update,
                "Usage: /add <task text> — e.g. /add buy milk tomorrow p1",
            )
            return
        if len(text) > 500:
            await _reply(update, "Task text too long — max 500 characters")
            return
        async with _chat_lock(chat.id):
            task = await todoist.add_task_in_project(text, project)
            today = cfg.today()
            day = effective_day(task.due, task.deadline, cfg.resolved_timezone)
            detail = f" (due {_fmt_due(day, today)})" if day is not None else ""
            headline = f"✅ Added: {_render_text(task.content)}{detail}"
            state = get_chat_state(context.chat_data)
            if state is not None and state.day != today:
                state = None
            cutoff = state.cutoff if state is not None else today
            if day is None or day > cutoff:
                await _reply_html(update, headline)
                return
            try:
                tasks = await todoist.tasks_due_by(project.id, cutoff)
            except TodoistError:
                # The add itself succeeded — report it; only the list
                # refresh failed.
                await _reply_html(
                    update,
                    f"{headline}\n\n⚠ couldn't refresh the list — {_LIST_CMDS}",
                )
                return
            await _send_list(
                update,
                context,
                tasks,
                today,
                cutoff,
                mentions,
                todoist,
                project.id,
                headline=headline,
            )

    def _cmd_list(days: int, name: str) -> Handler:
        async def cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            chat = update.effective_chat
            if chat is None:
                return
            async with _chat_lock(chat.id):
                today = cfg.today()
                cutoff = today + timedelta(days=days)
                tasks = await todoist.tasks_due_by(project.id, cutoff)
                await _send_list(
                    update,
                    context,
                    tasks,
                    today,
                    cutoff,
                    mentions,
                    todoist,
                    project.id,
                )

        cmd.__name__ = name
        return guarded(cmd, typing=True)

    async def _require_states(
        update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int
    ) -> list[ListState] | None:
        """Today's lists for this user (their /mine + the chat list).

        Callers hold the chat lock.  Returns None after replying why
        there is nothing to act on.
        """
        today = cfg.today()
        states = active_states(context.chat_data, user_id, today)
        if states:
            return states
        if get_chat_state(context.chat_data) is not None:
            # active_states pruned stale /mine lists; a stale chat list
            # is cleared here so the message explains the expiry once.
            set_chat_state(context.chat_data, None)
            await _reply(
                update,
                f"The last list is from a previous day — send {_LIST_CMDS} " "or /mine",
            )
        else:
            await _reply(update, f"Send {_LIST_CMDS} or /mine first")
        return None

    def _union_ids(states: list[ListState]) -> list[str]:
        """Task ids across all states, deduplicated (first wins)."""
        return list(
            dict.fromkeys(item.task_id for state in states for item in state.items)
        )

    def _resolve_query(states: list[ListState], folded: str) -> list[str]:
        """Resolve a typed id against the sender's lists.

        Prefers an exact match of a *displayed* short suffix (the label
        the user copied from a message); a shown label can otherwise
        collide with a never-displayed tail in the other list and be
        rejected as ambiguous.  Falls back to exact/tail matching over
        the union of both lists.
        """
        displayed = displayed_matches(states, folded)
        if len(displayed) == 1:
            return displayed
        return resolve_ids(_union_ids(states), folded)

    def _mutation_states(
        context: ContextTypes.DEFAULT_TYPE, states: list[ListState]
    ) -> list[ListState]:
        """Resolution states plus every user's /mine, identity-deduped.

        IDs resolve against the sender's lists only (active_states), but
        the mutation must land on every stored copy: an unstruck /mine
        copy in another user's list would pass the already-done guard
        and complete a recurring task a second time.
        """
        merged = [*states, *all_mine_states(context.chat_data, cfg.today())]
        seen: set[int] = set()
        unique: list[ListState] = []
        for state in merged:
            if id(state) not in seen:
                seen.add(id(state))
                unique.append(state)
        return unique

    def _find_item(states: list[ListState], task_id: str) -> ListItem | None:
        return next(
            (
                item
                for state in states
                for item in state.items
                if item.task_id == task_id
            ),
            None,
        )

    async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        user = update.effective_user
        if chat is None or user is None:
            return
        query = " ".join(context.args or []).strip()
        if not query:
            await _reply(
                update, f"Usage: /done <id> — id shown by {_LIST_CMDS} or /mine"
            )
            return
        async with _chat_lock(chat.id):
            states = await _require_states(update, context, user.id)
            if states is None:
                return
            folded = query.casefold()
            resolved = _resolve_query(states, folded)
            if not resolved:
                await _reply(update, "No match in the last shown list")
                return
            if len(resolved) > 1:
                await _reply(
                    update,
                    f"Ambiguous — matches {len(resolved)} tasks, be more specific",
                )
                return
            task_id = resolved[0]
            item = _find_item(states, task_id)
            assert item is not None  # resolved from the same union
            # The guard must see every stored copy, not just the
            # sender's lists: another user's refreshed /mine can show a
            # fresh un-struck line while their older struck copy (or the
            # chat list) still has it — completing again would close a
            # recurring task's *next* occurrence.  Same scope as the
            # sync below.
            mutation = _mutation_states(context, states)
            if any_done(mutation, task_id):
                # A struck copy in *any* stored list blocks this — most
                # often the chat list, which a /mine refresh does not
                # replace, so point at the chat list refresh.
                await _reply(
                    update, f"Already completed — send {_LIST_CMDS} to refresh"
                )
                return
            try:
                await todoist.complete_task(task_id)
            except TodoistError as exc:
                await _reply(update, f"⚠ {exc} — send {_LIST_CMDS} or /mine to refresh")
                return
            next_due: date | None = None
            if item.recurring:
                try:
                    rolled = await todoist.get_task(task_id)
                    if rolled.due is not None:
                        day = rolled.due.date
                        next_due = day.date() if isinstance(day, datetime) else day
                except TodoistError:
                    logger.warning("could not read next occurrence of %s", task_id)
            # Strike the copy in every stored list (chat + any /mine),
            # showing the rolled occurrence's date for recurring tasks.
            touched = sync_item(mutation, task_id, done=True, due=next_due)
            await _edit_states(update, context, touched, mentions)
            if item.recurring and next_due is not None:
                await _reply_html(
                    update,
                    f"✅ {_render_text(item.text)} — "
                    f"next occurrence: {_fmt_due(next_due, cfg.today())}",
                )
            else:
                await _reply_html(update, f"✅ {_render_text(item.text)}")

    async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        user = update.effective_user
        if chat is None or user is None:
            return
        query = " ".join(context.args or []).strip()
        if not query:
            await _reply(
                update, f"Usage: /undo <id> — id shown by {_LIST_CMDS} or /mine"
            )
            return
        async with _chat_lock(chat.id):
            states = await _require_states(update, context, user.id)
            if states is None:
                return
            folded = query.casefold()
            resolved = _resolve_query(states, folded)
            if len(resolved) > 1:
                await _reply(
                    update,
                    f"Ambiguous — matches {len(resolved)} tasks, be more specific",
                )
                return
            if not resolved:
                await _reply(update, "No match in the last shown list")
                return
            task_id = resolved[0]
            item = _find_item(states, task_id)
            assert item is not None  # resolved from the same union
            if not any_done(states, task_id):
                # No copy struck — including the case where one list was
                # refreshed and lost the strike while another still has it
                # (any_done above covers the reverse divergence).
                await _reply(update, "Not completed — nothing to undo")
                return
            if item.recurring:
                # Blanket refusal: undoing a rolled completion is unsafe (the
                # series already advanced); a series that *ended* on the last
                # occurrence could be undone, but distinguishing the two
                # needs extra state — accepted false negative.
                await _reply(update, "⚠ can't undo a recurring completion")
                return
            try:
                await todoist.uncomplete_task(task_id)
            except TodoistError as exc:
                await _reply(update, f"⚠ {exc} — send {_LIST_CMDS} or /mine to refresh")
                return
            touched = sync_item(_mutation_states(context, states), task_id, done=False)
            await _edit_states(update, context, touched, mentions)
            await _reply_html(update, f"↩️ {_render_text(item.text)}")

    async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        user = update.effective_user
        if chat is None or user is None:
            return
        raw = " ".join(context.args or []).strip()
        query, separator, new_text = raw.partition(" ")
        if not separator or not new_text.strip():
            await _reply(
                update,
                "Usage: /edit <id> <new text> — e.g. /edit 5c buy milk",
            )
            return
        new_text = new_text.strip()
        if len(new_text) > 500:
            await _reply(update, "Task text too long — max 500 characters")
            return
        async with _chat_lock(chat.id):
            states = await _require_states(update, context, user.id)
            if states is None:
                return
            folded = query.casefold()
            resolved = _resolve_query(states, folded)
            if not resolved:
                await _reply(update, "No match in the last shown list")
                return
            if len(resolved) > 1:
                await _reply(
                    update,
                    f"Ambiguous — matches {len(resolved)} tasks, be more specific",
                )
                return
            task_id = resolved[0]
            item = _find_item(states, task_id)
            assert item is not None  # resolved from the same union
            if any_done(states, task_id):
                await _reply(update, "Already completed — can't edit")
                return
            try:
                await todoist.update_task_content(task_id, new_text)
            except TodoistError as exc:
                await _reply(update, f"⚠ {exc} — send {_LIST_CMDS} or /mine to refresh")
                return
            touched = sync_item(
                _mutation_states(context, states), task_id, text=new_text
            )
            await _edit_states(update, context, touched, mentions)
            await _reply_html(update, f"✏️ {_render_text(new_text)}")

    async def cmd_mine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """The sender's own tasks: due, then upcoming, then undated."""
        chat = update.effective_chat
        user = update.effective_user
        message = update.effective_message
        if chat is None or user is None or message is None:
            return
        async with _chat_lock(chat.id):
            assignee_name = todoist_name_for_user(
                user.id, user.username, cfg.assignee_mentions
            )
            if assignee_name is None:
                await _reply(
                    update,
                    "I can't match your Telegram account to a Todoist "
                    "collaborator — set ASSIGNEE_MENTIONS "
                    "(Todoist Name=@you or Name=<numeric id>) and retry",
                )
                return
            assignee_id = await todoist.collaborator_id_for_name(
                project.id, assignee_name
            )
            if assignee_id is None:
                await _reply(
                    update,
                    f"Todoist collaborator {assignee_name!r} not found on this "
                    "project (or the lookup failed) — check ASSIGNEE_MENTIONS "
                    "and retry /mine",
                )
                return
            today = cfg.today()
            due, upcoming, undated = await todoist.tasks_for_assignee(
                project.id, assignee_id, today
            )
            selected, dividers = take_mine(due, upcoming, undated, _MINE_LIMIT)
            if not selected:
                set_mine_state(context.chat_data, user.id, None)
                await _reply(update, "Nothing assigned to you 🎉")
                return
            items = await _build_items(selected, todoist, project.id)
            chunks = _render_list_message(
                items,
                today,
                mentions,
                show_dates=True,
                headline=_MINE_TITLE,
                dividers=dividers,
            )
            message_id = await _send_chunks(
                lambda text: message.reply_text(text, parse_mode=ParseMode.HTML),
                chunks,
            )
            set_mine_state(
                context.chat_data,
                user.id,
                ListState(
                    message_id=message_id,
                    day=today,
                    cutoff=today,
                    items=items,
                    headline=_MINE_TITLE,
                    show_dates=True,
                    dividers=dividers,
                ),
            )

    async def cmd_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None:
            return
        await message.reply_text(
            f'📂 <a href="{html.escape(project.url, quote=True)}">'
            f"{html.escape(project.name)}</a> — open in Todoist",
            parse_mode=ParseMode.HTML,
        )

    async def on_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        text = message.text if message is not None else None
        first = (text or "").split(maxsplit=1)[0] if text else ""
        if "@" in first:
            # /cmd@SomeOtherBot — another bot's command, stay quiet.
            addressed = first.partition("@")[2].casefold()
            if addressed != context.bot.username.casefold():
                return
        await _reply(update, "Unknown command — try /help")

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
            await _reply_safely(update)

    application = (
        Application.builder()
        .token(cfg.telegram_token)
        .post_init(post_init)
        .post_stop(post_stop)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(
        CommandHandler(["help", "start"], guarded(cmd_help), filters=auth)
    )
    application.add_handler(
        CommandHandler("add", guarded(cmd_add, typing=True), filters=auth)
    )
    application.add_handler(
        CommandHandler("today", _cmd_list(0, "cmd_today"), filters=auth)
    )
    application.add_handler(
        CommandHandler("tomorrow", _cmd_list(1, "cmd_tomorrow"), filters=auth)
    )
    application.add_handler(
        CommandHandler("week", _cmd_list(6, "cmd_week"), filters=auth)
    )
    application.add_handler(
        CommandHandler("mine", guarded(cmd_mine, typing=True), filters=auth)
    )
    application.add_handler(
        CommandHandler("done", guarded(cmd_done, typing=True), filters=auth)
    )
    application.add_handler(
        CommandHandler("undo", guarded(cmd_undo, typing=True), filters=auth)
    )
    application.add_handler(
        CommandHandler("edit", guarded(cmd_edit, typing=True), filters=auth)
    )
    application.add_handler(
        CommandHandler("project", guarded(cmd_project), filters=auth)
    )
    application.add_handler(MessageHandler(filters.COMMAND & auth, guarded(on_unknown)))
    application.add_error_handler(on_error)
    return application
