"""Telegram output, feedback and list rendering shared by bot and commands.

This module is the effect layer: every function here talks to Telegram
or renders state for display.  Command classes (commands.py) call these
with their injected cfg; the morning loop and on_error in bot.py use
them directly.  Keeping the effects in one place is the seam a future
pure-interpreter redesign would sit under.
"""

from __future__ import annotations

import asyncio
import html
import logging
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import suppress
from datetime import date
from typing import Any, Literal, cast

from markdown_it import MarkdownIt
from telegram import Message, ReactionTypeEmoji, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from .config import Config
from .state import (
    Task,
    TaskList,
    put_tasks,
    set_chat_list,
    task_suffixes,
)
from .todoist_client import DueTask, TodoistClient

logger = logging.getLogger(__name__)

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, None]]


_MESSAGE_LIMIT = 4000
_TELEGRAM_LIMIT = 4096
_DISPLAY_LIMIT = 500
BODY_LIMIT = 3500
_TYPING_INTERVAL = 4.0
NBSP = "\N{NO-BREAK SPACE}"
LIST_CMDS = "/today, /tomorrow or /week"
MINE_TITLE = "<b>Your tasks</b>"
MINE_LIMIT = 10


class TelegramBot(ABC):
    """The send-side Telegram surface the bot actually uses.

    The library's Bot implements this; bot_for_update() casts the bot
    bound to an Update so tests can substitute a fake while the real
    reply/render stack (Message.reply_text → bot.send_message) runs
    unmodified.
    """

    @property
    @abstractmethod
    def username(self) -> str:
        """The bot's own username, without the @."""

    @abstractmethod
    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Message:
        """Send a message; the posted Message on success."""

    @abstractmethod
    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str, **kwargs: Any
    ) -> Any:
        """Edit a previously sent message in place."""

    @abstractmethod
    async def set_message_reaction(
        self, chat_id: int, message_id: int, reaction: Any, **kwargs: Any
    ) -> bool:
        """React to the command message with an emoji reaction."""

    @abstractmethod
    async def send_chat_action(self, chat_id: int, action: str, **kwargs: Any) -> bool:
        """Show a chat action such as "typing…"."""


def bot_for_update(update: Update) -> TelegramBot:
    """The bot bound to this update — the library Bot in production, a
    fake in tests.  The single extraction point: command code must never
    reach for context.bot or construct a Bot, so binding a fake to the
    update makes every send go to the fake.
    """
    return cast(TelegramBot, update.get_bot())


# Every read-modify-write of a chat's list state holds this chat's lock:
# updates are processed sequentially, so the only concurrent writer is
# the morning-announce loop.  Locks bind to the running event loop on
# first use; the bot runs a single loop for its whole lifetime.
_chat_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def chat_lock(chat_id: int) -> asyncio.Lock:
    return _chat_locks[chat_id]


_md = MarkdownIt("gfm-like", {"html": False}).disable("image")
# Telegram only accepts http(s) and tg links; markdown-it's default
# validator would also emit mailto:, data: and relative hrefs.
_md.validateLink = lambda url: url.startswith(("http://", "https://", "tg://"))


async def start_typing(
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
                await bot_for_update(update).send_chat_action(
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


async def stop_typing(ticker: asyncio.Task[None] | None) -> None:
    if ticker is None:
        return
    ticker.cancel()
    with suppress(asyncio.CancelledError):
        await ticker


async def reply(update: Update, text: str) -> None:
    message = update.effective_message
    if message is not None:
        await message.reply_text(text)


Status = Literal["ok", "soft", "hard"]

_STATUS_EMOJI: dict[Status, str] = {"ok": "👍", "soft": "🤨", "hard": "👎"}


def status_emoji(status: Status) -> str:
    """Reaction emoji for a response status — from Telegram's fixed
    reaction set (REACTION_REPLIES): 👍 ok, 🤨 soft refusal, 👎 failure.
    """
    return _STATUS_EMOJI[status]


async def react(update: Update, status: Status) -> None:
    """React on the command message; failures log and stay silent.

    Reaction mode's contract (REACTION_REPLIES): no text fallback —
    a chat that rejects the reaction gets no feedback at all.
    """
    message = update.effective_message
    if message is None:
        return
    try:
        await bot_for_update(update).set_message_reaction(
            chat_id=message.chat_id,
            message_id=message.id,
            reaction=ReactionTypeEmoji(status_emoji(status)),
        )
    except TelegramError as exc:
        logger.debug("reaction failed chat=%s: %s", message.chat_id, exc)


async def reply_html(update: Update, text: str) -> None:
    message = update.effective_message
    if message is not None:
        await message.reply_text(text, parse_mode=ParseMode.HTML)


def md_html(text: str) -> str:
    # Collapsing whitespace first kills markdown hard breaks (`<br />`,
    # which Telegram rejects) and keeps output on one line.
    return _md.renderInline(" ".join(text.split()))


def render_text(raw: str) -> str:
    """Markdown → Telegram HTML, capped so the message stays sendable.

    Percent-encoding of URLs inside links can inflate a 500-char title
    ~12x; plain html.escape is bounded at 6x, so fall back to it when
    the rendered form grows too large.
    """
    clean = one_line(raw)
    out = md_html(clean)
    if len(out) > BODY_LIMIT:
        return html.escape(clean)
    return out


def fmt_due(day: date, reference: date) -> str:
    """`Tue 9 Jun`, or `Tue 9 Jun 25` when the year differs from the
    reference (usually the day the list was built for). Near-today lists
    stay compact; old overdue items and year-boundary ranges disambiguate.
    """
    formatted = f"{day.strftime('%a')} {day.day} {day.strftime('%b')}"
    if day.year != reference.year:
        formatted += f" {day.strftime('%y')}"
    return formatted


def one_line(text: str, limit: int = _DISPLAY_LIMIT) -> str:
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


def chunk(text: str) -> list[str]:
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
    # one_line keeps every line well under the limit; the assert guards
    # against future render changes reintroducing oversized chunks.
    assert all(len(chunk) <= _MESSAGE_LIMIT for chunk in chunks)
    return chunks


def mentions_lookup(cfg: Config) -> dict[str, str]:
    """Casefolded Todoist-name → Telegram mention target mapping."""
    return {name.casefold(): target for name, target in cfg.assignee_mentions.items()}


def assignee_suffix(name: str | None, mentions: dict[str, str]) -> str:
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


def render_list(
    items: list[Task],
    today: date,
    *,
    show_dates: bool,
    mentions: dict[str, str] | None = None,
    dividers: dict[int, str] | None = None,
) -> str:
    """Render the task list body (no headline).

    Pure over resolved entities: callers resolve list ids through the
    chat's task map first (_resolve_entities), so this never sees the
    storage.  With show_dates (used by /tomorrow, /week and /mine) every
    line carries its due date so multi-day lists are readable; /today
    only dates overdue lines.  Done items keep their date in multi-day
    lists too — the strike-through already marks them.  Assignees
    (resolved at fetch time) render between the task text and the due
    suffix.  dividers maps an item index to a bold section header
    printed above that line (/mine's Due/Upcoming/No date sections).
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
        body = render_text(item.text)
        if item.done:
            body = f"<s>{body}</s>"
        padded = suffix.ljust(width, NBSP)
        line = (
            f"<code>[{padded}]</code>  {marker}{body}"
            f"{assignee_suffix(item.assignee, mentions)}"
        )
        if item.due is not None and (show_dates or overdue):
            line += f" — due {fmt_due(item.due, today)}"
        lines.append(line)
    return "\n".join(lines)


def _resolve_entities(task_ids: list[str], tasks: dict[str, Task]) -> list[Task]:
    """Entities for the list's id order (invariant: every id has one)."""
    resolved: list[Task] = []
    for task_id in task_ids:
        entity = tasks.get(task_id)
        assert entity is not None, f"list id {task_id} has no entity in the map"
        resolved.append(entity)
    return resolved


async def build_entities(
    tasks: list[DueTask], todoist: TodoistClient, project_id: str
) -> dict[str, Task]:
    """Map of fetched task id → entity, resolving assignee display names.

    Insertion order = fetch order, so list(dict) gives the id order to
    store.  Assignee resolution goes through the client's cached
    collaborator map (fetched once, refreshed on unknown ids) and never
    raises — a failed lookup leaves the assignee blank.
    """
    entities: dict[str, Task] = {}
    for task in tasks:
        assignee: str | None = None
        if task.task.assignee_id:
            assignee = await todoist.collaborator_name(
                project_id, task.task.assignee_id
            )
        entities[task.task.id] = Task(
            task_id=task.task.id,
            text=task.task.content,
            due=task.due,
            recurring=bool(task.task.due and task.task.due.is_recurring),
            assignee=assignee,
        )
    return entities


def render_list_message(
    task_ids: list[str],
    tasks: dict[str, Task],
    today: date,
    mentions: dict[str, str],
    *,
    show_dates: bool,
    cutoff: date | None = None,
    headline: str | None = None,
    dividers: dict[int, str] | None = None,
) -> list[str]:
    """Resolve the list's ids and render into sendable chunks.

    cutoff (when given) prefixes the "Due by <date>:" header; /mine
    passes None and uses dividers for its sections instead.  Returns []
    for an empty list — the caller decides the empty reply.
    """
    if not task_ids:
        return []
    body = render_list(
        _resolve_entities(task_ids, tasks),
        today,
        show_dates=show_dates,
        mentions=mentions,
        dividers=dividers,
    )
    if cutoff is not None:
        body = f"<b>Due by {fmt_due(cutoff, today)}:</b>\n{body}"
    if headline is not None:
        body = f"{headline}\n\n{body}"
    return chunk(body)


async def send_chunks(
    send: Callable[[str], Awaitable[Message]], chunks: list[str]
) -> int | None:
    """Send rendered chunks via `send`; message id when single-chunk.

    Single-chunk lists get an editable message id; multi-chunk lists
    stay un-edited (the first message alone would lose its siblings).
    """
    first = await send(chunks[0])
    for chunk in chunks[1:]:
        await send(chunk)
    logger.debug("sent %d chunk(s), editable=%s", len(chunks), len(chunks) == 1)
    return first.message_id if len(chunks) == 1 else None


async def send_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    tasks: list[DueTask],
    today: date,
    cutoff: date,
    mentions: dict[str, str],
    todoist: TodoistClient,
    project_id: str,
    headline: str | None = None,
    *,
    already_confirmed: bool = False,
) -> None:
    """Send the rendered list, optionally under an HTML headline line.

    Stores the list so /done, /undo and /edit act on it: entities go
    into the chat's task map first, then the id order points at them.
    `already_confirmed` means the caller delivered the status line
    another way (REACTION_REPLIES reaction) — skip the headline echo.
    """
    message = update.effective_message
    if message is None:
        return
    show_dates = cutoff > today
    entities = await build_entities(tasks, todoist, project_id)
    put_tasks(context.chat_data, entities.values())
    chunks = render_list_message(
        list(entities),
        entities,
        today,
        mentions,
        show_dates=show_dates,
        cutoff=cutoff,
        headline=headline,
    )
    if not chunks:
        set_chat_list(context.chat_data, None)
        if headline is not None:
            # Reaction mode already got its status line from cmd_add;
            # don't also echo the headline.
            if not already_confirmed:
                await reply_html(update, headline)
        elif cutoff > today:
            await reply(update, f"Nothing due by {fmt_due(cutoff, today)} 🎉")
        else:
            await reply(update, "Nothing due today 🎉")
        return
    message_id = await send_chunks(
        lambda text: message.reply_text(text, parse_mode=ParseMode.HTML), chunks
    )
    set_chat_list(
        context.chat_data,
        TaskList(
            message_id=message_id,
            day=today,
            cutoff=cutoff,
            task_ids=list(entities),
            headline=headline,
            show_dates=show_dates,
        ),
    )


async def _edit_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    task_list: TaskList,
    mentions: dict[str, str],
    tasks: dict[str, Task],
) -> None:
    """Re-render the stored list message from its state.

    Renders with task_list.day — not the current clock — so a strike
    applied just after midnight still draws against the day the list
    was built for, and the message the user is looking at stays
    consistent.  `tasks` is the chat's map; ids resolve through it.
    """
    if task_list.message_id is None:
        return
    chat = update.effective_chat
    if chat is None:
        return
    today = task_list.day
    rendered = render_list(
        _resolve_entities(task_list.task_ids, tasks),
        today,
        show_dates=task_list.show_dates or task_list.cutoff > today,
        mentions=mentions,
        dividers=task_list.dividers,
    )
    if task_list.cutoff > today:
        rendered = f"<b>Due by {fmt_due(task_list.cutoff, today)}:</b>\n{rendered}"
    if task_list.headline is not None:
        rendered = f"{task_list.headline}\n\n{rendered}"
    if len(rendered) > _TELEGRAM_LIMIT:
        logger.warning("rendered list is %d chars, skipping edit", len(rendered))
        return
    try:
        await bot_for_update(update).edit_message_text(
            chat_id=chat.id,
            message_id=task_list.message_id,
            text=rendered,
            parse_mode=ParseMode.HTML,
        )
        logger.debug("edited list message chat=%s (%d chars)", chat.id, len(rendered))
    except BadRequest as exc:
        if "not modified" in str(exc):
            logger.debug("list message already up to date")
        elif "message to edit not found" in str(exc).casefold():
            # User deleted the list message; stop editing it and stop
            # spamming exception logs on every strike.
            logger.debug("list message deleted; clearing edit target")
            task_list.message_id = None
        else:
            logger.exception("could not edit list message")
    except TelegramError:
        logger.exception("could not edit list message")


async def edit_lists(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    task_lists: list[TaskList],
    mentions: dict[str, str],
    tasks: dict[str, Task],
) -> None:
    """Re-edit every message backing `task_lists` (chat list + /mine lists)."""
    for task_list in task_lists:
        await _edit_list(update, context, task_list, mentions, tasks)


def is_group_chat_type(kind: str) -> bool:
    """True for the Telegram chat types the morning list targets.

    Telegram reports "private", "group", "supergroup" or "channel"
    (`ChatType.value`); only group chats get the daily list — private
    chats and channels do not.
    """
    return kind in {"group", "supergroup"}


def reaction_mode_applies(reaction_replies: bool, chat_type: str | None) -> bool:
    """Whether REACTION_REPLIES answers this chat with a reaction.

    Reaction mode is a group-chat feature: private chats, channels and
    chats without a resolvable type always get the text reply.
    `chat_type` is `Chat.type` — annotated `str`, actually ChatType (a
    str-enum), which compares equal to "group"/"supergroup".
    """
    return bool(
        reaction_replies and chat_type is not None and is_group_chat_type(chat_type)
    )


async def respond(
    cfg: Config,
    update: Update,
    status: Status,
    text: str,
    *,
    html: bool = False,
) -> None:
    """Action-command feedback: status reaction or the text reply.

    With reaction mode the command message gets 👍/🤨/👎 (see
    status_emoji) and nothing is posted; a failed reaction logs at
    DEBUG and stays silent — no text fallback.  Otherwise the
    reply strings are byte-identical to pre-reaction mode.
    """
    if reaction_mode_applies(
        cfg.reaction_replies,
        update.effective_chat.type if update.effective_chat is not None else None,
    ):
        await react(update, status)
        return
    if html:
        await reply_html(update, text)
    else:
        await reply(update, text)


async def respond_safely(
    cfg: Config, update: Update, status: Status, text: str
) -> None:
    try:
        await respond(cfg, update, status, text)
    except TelegramError:
        logger.exception("could not send reply")
