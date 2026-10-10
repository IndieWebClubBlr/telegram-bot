from __future__ import annotations

import asyncio
import functools
import html
import logging
from collections.abc import Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from markdown_it import MarkdownIt
from telegram import Update
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

_md = MarkdownIt("gfm-like", {"html": False}).disable("image")
# Telegram only accepts http(s) and tg links; markdown-it's default
# validator would also emit mailto:, data: and relative hrefs.
_md.validateLink = lambda url: url.startswith(("http://", "https://", "tg://"))

_HELP = """\
Commands (project: {project}):

/add <text> — add a task, quick-add syntax: "buy milk tomorrow p1 @chore /regular +Abhinav"
/today — tasks due today or overdue, each with a short id
/tomorrow — tasks due by end of tomorrow
/week — tasks due by end of the next 6 days
/done <id> — complete the task with that id (from the last shown list)
/undo <id> — reopen a task completed via /done (not recurring)
/edit <id> <new text> — edit the task with that id
/project — link to this project in Todoist
/start or /help — this message

- ids are shown in lowercase and matched case-insensitively
- {list_cmds} show the list that /done, /undo and /edit act on;
  edits appear in that message only — refresh with {list_cmds} to re-fetch
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
                f"completed; check with {_LIST_CMDS}",
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


def _fmt_due(day: date) -> str:
    return f"{day.strftime('%a')} {day.day} {day.strftime('%b')}"


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


def _chunk(text: str) -> list[str]:
    if len(text) <= _MESSAGE_LIMIT:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if current and len(current) + 1 + len(line) > _MESSAGE_LIMIT:
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


def _resolve_ids(task_ids: list[str], folded: str) -> list[str]:
    """Match by exact casefolded id first, then by casefolded tail."""
    exact = [task_id for task_id in task_ids if task_id.casefold() == folded]
    if exact:
        return exact
    return [task_id for task_id in task_ids if task_id.casefold().endswith(folded)]


@dataclass
class _ListItem:
    task_id: str
    text: str
    due: date
    done: bool = False
    recurring: bool = False


@dataclass
class _ListState:
    message_id: int | None
    day: date
    cutoff: date
    items: list[_ListItem]
    headline: str | None = None


def _task_suffixes(task_ids: list[str]) -> list[str]:
    """Shortest case-insensitive unique tail of each id, minimum 2 chars.

    Uniqueness and display are lowercase so two labels can never differ
    only by case, and typing any case resolves to the same task.
    """
    folded_ids = [task_id.casefold() for task_id in task_ids]
    suffixes: list[str] = []
    for task_id in task_ids:
        size = 2
        while size <= len(task_id):
            probe = task_id[-size:].casefold()
            if sum(1 for other in folded_ids if other.endswith(probe)) == 1:
                break
            size += 1
        suffixes.append(task_id[-size:].casefold())
    return suffixes


def _render_list(items: list[_ListItem], today: date, *, show_dates: bool) -> str:
    """Render the task list body (no headline).

    With show_dates (used by /tomorrow and /week) every line carries its
    due date so multi-day lists are readable; /today only dates overdue
    lines.  Done items keep their date in multi-day lists too — the
    strike-through already marks them.
    """
    suffixes = _task_suffixes([item.task_id for item in items])
    width = max((len(suffix) for suffix in suffixes), default=0)
    lines: list[str] = []
    for suffix, item in zip(suffixes, items, strict=True):
        marker = "⚠ " if item.due < today and not item.done else ""
        body = _render_text(item.text)
        if item.done:
            body = f"<s>{body}</s>"
        padded = suffix.ljust(width, _NBSP)
        line = f"<code>[{padded}]</code>  {marker}{body}"
        if show_dates or (item.due < today and not item.done):
            line += f" — due {_fmt_due(item.due)}"
        lines.append(line)
    return "\n".join(lines)


async def _send_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    tasks: list[DueTask],
    today: date,
    cutoff: date,
    headline: str | None = None,
) -> None:
    """Send the rendered list, optionally under an HTML headline line.

    Stores the list state so /done, /undo and /edit act on it.
    """
    message = update.effective_message
    if message is None:
        return
    if not tasks:
        _set_state(context, None)
        if headline is not None:
            await _reply_html(update, headline)
        elif cutoff > today:
            await _reply(update, f"Nothing due by {_fmt_due(cutoff)} 🎉")
        else:
            await _reply(update, "Nothing due today 🎉")
        return
    items = [
        _ListItem(
            task_id=task.task.id,
            text=task.task.content,
            due=task.due,
            recurring=bool(task.task.due and task.task.due.is_recurring),
        )
        for task in tasks
    ]
    body = _render_list(items, today, show_dates=cutoff > today)
    if cutoff > today:
        body = f"<b>Due by {_fmt_due(cutoff)}:</b>\n{body}"
    if headline is not None:
        body = f"{headline}\n\n{body}"
    chunks = _chunk(body)
    first = await message.reply_text(chunks[0], parse_mode=ParseMode.HTML)
    message_id = first.message_id if len(chunks) == 1 else None
    for chunk in chunks[1:]:
        await message.reply_text(chunk, parse_mode=ParseMode.HTML)
    _set_state(
        context,
        _ListState(
            message_id=message_id,
            day=today,
            cutoff=cutoff,
            items=items,
            headline=headline,
        ),
    )


def _get_state(context: ContextTypes.DEFAULT_TYPE) -> _ListState | None:
    chat_data = context.chat_data
    if chat_data is None:
        return None
    state = chat_data.get("list")
    return state if isinstance(state, _ListState) else None


def _set_state(context: ContextTypes.DEFAULT_TYPE, state: _ListState | None) -> None:
    chat_data = context.chat_data
    if chat_data is None:
        return
    if state is None:
        chat_data.pop("list", None)
    else:
        chat_data["list"] = state


async def _edit_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: _ListState,
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
    rendered = _render_list(state.items, today, show_dates=state.cutoff > today)
    if state.cutoff > today:
        rendered = f"<b>Due by {_fmt_due(state.cutoff)}:</b>\n{rendered}"
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


def build_application(
    cfg: Config, project: Project, todoist: TodoistClient
) -> Application:
    # NOTE: the Application is built without concurrent_updates, so PTB
    # processes updates strictly one at a time (SimpleUpdateProcessor,
    # max_concurrent_updates=1).  The list handlers rely on this: each
    # is a read-modify-write of chat_data["list"] across awaits with no
    # lock.  Enabling concurrent_updates would interleave those at await
    # points — add a per-chat asyncio.Lock before ever setting it.
    auth = (
        filters.Chat(chat_id=sorted(cfg.allowed_chat_ids)) & ~filters.UpdateType.EDITED
    )

    async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _reply(update, _HELP.format(project=project.name))

    async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        task = await todoist.add_task_in_project(text, project)
        today = cfg.today()
        day = effective_day(task.due, task.deadline, cfg.resolved_timezone)
        detail = f" (due {_fmt_due(day)})" if day is not None else ""
        headline = f"✅ Added: {_render_text(task.content)}{detail}"
        state = _get_state(context)
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
                update, f"{headline}\n\n⚠ couldn't refresh the list — {_LIST_CMDS}"
            )
            return
        await _send_list(update, context, tasks, today, cutoff, headline=headline)

    def _cmd_list(days: int, name: str) -> Handler:
        async def cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            today = cfg.today()
            cutoff = today + timedelta(days=days)
            tasks = await todoist.tasks_due_by(project.id, cutoff)
            await _send_list(update, context, tasks, today, cutoff)

        cmd.__name__ = name
        return guarded(cmd, typing=True)

    async def _require_state(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> _ListState | None:
        """State of today's last shown list, or a reply explaining why not."""
        today = cfg.today()
        state = _get_state(context)
        if state is None:
            await _reply(update, f"Send {_LIST_CMDS} first")
            return None
        if state.day != today:
            _set_state(context, None)
            await _reply(
                update,
                f"The last list is from a previous day — send {_LIST_CMDS}",
            )
            return None
        return state

    async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = " ".join(context.args or []).strip()
        if not query:
            await _reply(update, f"Usage: /done <id> — id shown by {_LIST_CMDS}")
            return
        state = await _require_state(update, context)
        if state is None:
            return
        folded = query.casefold()
        resolved = _resolve_ids([item.task_id for item in state.items], folded)
        if not resolved:
            await _reply(update, "No match in the last shown list")
            return
        if len(resolved) > 1:
            await _reply(
                update,
                f"Ambiguous — matches {len(resolved)} tasks, be more specific",
            )
            return
        item = next(item for item in state.items if item.task_id == resolved[0])
        if item.done:
            # Never call the API again: for a recurring task a second
            # complete would close the *next* occurrence.
            await _reply(update, f"Already completed — send {_LIST_CMDS} to refresh")
            return
        try:
            await todoist.complete_task(item.task_id)
        except TodoistError as exc:
            await _reply(update, f"⚠ {exc} — send {_LIST_CMDS} to refresh")
            return
        next_due: date | None = None
        if item.recurring:
            try:
                rolled = await todoist.get_task(item.task_id)
                if rolled.due is not None:
                    day = rolled.due.date
                    next_due = day.date() if isinstance(day, datetime) else day
            except TodoistError:
                logger.warning("could not read next occurrence of %s", item.task_id)
        item.done = True
        if next_due is not None:
            # Show the rolled occurrence's date instead of the stale one
            # the user just completed.
            item.due = next_due
        await _edit_list(update, context, state)
        if item.recurring and next_due is not None:
            await _reply_html(
                update,
                f"✅ {_render_text(item.text)} — "
                f"next occurrence: {_fmt_due(next_due)}",
            )
        else:
            await _reply_html(update, f"✅ {_render_text(item.text)}")

    async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = " ".join(context.args or []).strip()
        if not query:
            await _reply(update, f"Usage: /undo <id> — id shown by {_LIST_CMDS}")
            return
        state = await _require_state(update, context)
        if state is None:
            return
        folded = query.casefold()
        resolved = _resolve_ids([item.task_id for item in state.items], folded)
        if len(resolved) > 1:
            await _reply(
                update,
                f"Ambiguous — matches {len(resolved)} tasks, be more specific",
            )
            return
        if not resolved:
            await _reply(update, "No match in the last shown list")
            return
        item = next(item for item in state.items if item.task_id == resolved[0])
        if not item.done:
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
            await todoist.uncomplete_task(item.task_id)
        except TodoistError as exc:
            await _reply(update, f"⚠ {exc} — send {_LIST_CMDS} to refresh")
            return
        item.done = False
        await _edit_list(update, context, state)
        await _reply_html(update, f"↩️ {_render_text(item.text)}")

    async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        state = await _require_state(update, context)
        if state is None:
            return
        folded = query.casefold()
        resolved = _resolve_ids([item.task_id for item in state.items], folded)
        if not resolved:
            await _reply(update, "No match in the last shown list")
            return
        if len(resolved) > 1:
            await _reply(
                update,
                f"Ambiguous — matches {len(resolved)} tasks, be more specific",
            )
            return
        item = next(item for item in state.items if item.task_id == resolved[0])
        if item.done:
            await _reply(update, "Already completed — can't edit")
            return
        try:
            await todoist.update_task_content(item.task_id, new_text)
        except TodoistError as exc:
            await _reply(update, f"⚠ {exc} — send {_LIST_CMDS} to refresh")
            return
        item.text = new_text
        await _edit_list(update, context, state)
        await _reply_html(update, f"✏️ {_render_text(new_text)}")

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
