"""Command classes for the IWCB Telegram bot.

Every Telegram command is a _Command (or _ListAction) built with the
same constructor triple — (cfg, todoist, project) — and registered via
command_handler.  Effects (sending messages, reactions, list edits)
live in messaging.py; list state transitions live in state.py.  The
future direction: run() returns effect descriptions and a pure
interpreter executes them — keep run bodies linear awaits so that
reification stays mechanical.
"""

from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import ClassVar

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import CommandHandler, ContextTypes
from telegram.ext.filters import BaseFilter
from todoist_api_python.models import Project

from .config import Config
from .messaging import (
    Handler,
    Status,
    LIST_CMDS,
    MINE_LIMIT,
    MINE_TITLE,
    assignee_suffix,
    bot_for_update,
    build_entities,
    chat_lock,
    edit_lists,
    fmt_due,
    mentions_lookup,
    react,
    reaction_mode_applies,
    render_list_message,
    render_text,
    reply,
    respond,
    respond_safely,
    send_chunks,
    send_list,
    start_typing,
    stop_typing,
)
from .state import (
    UNSET,
    Ambiguous,
    Applied,
    Assign,
    Complete,
    Edit,
    Event,
    Permitted,
    Rejected,
    Reopen,
    Reschedule,
    TaskList,
    Unresolved,
    active_lists,
    assignee_name_candidates,
    begin,
    commit,
    get_chat_list,
    get_tasks,
    put_tasks,
    set_chat_list,
    set_mine_list,
    take_mine,
    todoist_name_for_user,
)
from .todoist_client import TodoistClient, TodoistError, effective_day

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Outcome:
    """perform()'s result: the state event to commit and the ok reply.

    `log_extra` feeds command-specific bits in log_applied overrides
    (e.g. /assign's collaborator id).
    """

    event: Event
    success: str
    log_extra: str | None = None


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
/assign <id> <assignee> — assign to a Telegram @handle or Todoist name
/due <id> <when> — change when a task is due ("tomorrow 5pm", "no date")
/project — link to this project in Todoist
/start or /help — this message

- ids are shown in lowercase and matched case-insensitively
- {list_cmds} or /mine show lists that /done, /undo, /edit, /assign and /due act on; edits appear in those messages only — refresh to re-fetch
- /done, /undo, /edit, /assign and /due sync across every user's /mine list and the chat list
- /add refreshes the shown list when the new task falls inside its range
""".replace("{list_cmds}", LIST_CMDS)


class _Command:
    """Uniform shell for command handlers: guards, optional chat
    lock, and registration.  Subclasses implement run(); the
    ClassVars below pick the preamble:

    - require_message / require_user: early-return unless present
    - use_lock: wrap run() in the per-chat lock (commands that
      need checks outside the lock set this False and take it
      themselves inside run)
    - typing: whether the typing indicator runs while the command does

    Every command is constructed identically as
    Command(cfg, todoist, project); cheap derivations (the reverse
    assignee-mention lookup) happen once in __init__.  Self helpers
    (_should_react, _respond) are thin wrappers over messaging.py's
    react/reply layer with self.cfg — the effect layer a future pure
    interpreter would replace.  Keep run bodies a linear sequence of
    awaits so they can later be reified as effect descriptions.
    """

    log_name: str
    require_message: ClassVar[bool] = False
    require_user: ClassVar[bool] = False
    use_lock: ClassVar[bool] = True
    typing: ClassVar[bool] = True

    def __init__(self, cfg: Config, todoist: TodoistClient, project: Project) -> None:
        self.cfg = cfg
        self.todoist = todoist
        self.project = project
        self.mentions = mentions_lookup(cfg)

    def _should_react(self, update: Update) -> bool:
        chat = update.effective_chat
        return reaction_mode_applies(
            self.cfg.reaction_replies, chat.type if chat is not None else None
        )

    async def _respond(
        self, update: Update, status: Status, text: str, *, html: bool = False
    ) -> None:
        await respond(self.cfg, update, status, text, html=html)

    async def run(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        raise NotImplementedError


def command_callback(command: _Command) -> Handler:
    """Guarded callback for a _Command instance: id-only debug
    logs, optional typing indicator, guards, optional chat lock, and
    the shared failure replies (log shapes preserved from the old
    guarded wrapper).
    """

    async def cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        started = asyncio.get_running_loop().time()
        chat = update.effective_chat
        user = update.effective_user
        # Ids only — never usernames, chat titles, message text or
        # command arguments.
        logger.debug(
            "%s chat=%s user=%s",
            command.log_name,
            chat.id if chat else None,
            user.id if user else None,
        )
        ticker: asyncio.Task[None] | None = None
        try:
            if command.typing:
                ticker = await start_typing(update, context)
            if command.require_message and update.effective_message is None:
                return
            if command.require_user and user is None:
                return
            if command.use_lock:
                if chat is None:
                    return
                async with chat_lock(chat.id):
                    await command.run(update, context)
            else:
                await command.run(update, context)
            logger.debug(
                "%s done in %.0fms",
                command.log_name,
                (asyncio.get_running_loop().time() - started) * 1000,
            )
        except TodoistError as exc:
            logger.warning("%s failed: %s", command.log_name, exc)
            await respond_safely(command.cfg, update, "hard", f"⚠ {exc}")
        except TelegramError:
            logger.exception("Telegram error in %s", command.log_name)
            await respond_safely(
                command.cfg,
                update,
                "hard",
                f"⚠ Telegram send failed — the command may have "
                f"completed; check with {LIST_CMDS} or /mine",
            )
        except Exception:
            logger.exception("Unhandled error in %s", command.log_name)
            await respond_safely(command.cfg, update, "hard", "Unexpected error")
        finally:
            await stop_typing(ticker)

    cmd.__name__ = command.log_name
    return cmd


def command_handler(
    commands: str | list[str], command: _Command, filters: BaseFilter | None = None
) -> CommandHandler:
    """PTB CommandHandler for a command spec and its _Command instance.

    `commands` is the slash-command name(s) without the slash (e.g.
    "done" or ["help", "start"]); `filters` is the registration-time
    auth filter.  The callback is command_callback()'s guarded shell.
    """
    return CommandHandler(commands, command_callback(command), filters=filters)


class HelpCommand(_Command):
    """The /help and /start reply — no lock, no typing indicator."""

    log_name = "cmd_help"
    require_message = True
    use_lock = False
    typing = False

    async def run(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await reply(update, _HELP.format(project=self.project.name))


class AddCommand(_Command):
    """Quick-add: validates, then locks, adds, and confirms.

    use_lock is False so the usage/too-long replies happen outside
    the chat lock, as they always have.
    """

    log_name = "cmd_add"
    require_message = False
    use_lock = False

    async def run(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if chat is None:
            return
        text = " ".join(context.args or []).strip()
        if not text:
            await self._respond(
                update,
                "soft",
                "Usage: /add <task text> — e.g. /add buy milk tomorrow p1",
            )
            return
        if len(text) > 500:
            await self._respond(
                update, "soft", "Task text too long — max 500 characters"
            )
            return
        async with chat_lock(chat.id):
            task = await self.todoist.add_task_in_project(text, self.project)
            today = self.cfg.today()
            day = effective_day(task.due, task.deadline, self.cfg.resolved_timezone)
            detail = f" (due {fmt_due(day, today)})" if day is not None else ""
            headline = f"✅ Added: {render_text(task.content)}{detail}"
            task_list = get_chat_list(context.chat_data)
            if task_list is not None and task_list.day != today:
                task_list = None
            cutoff = task_list.cutoff if task_list is not None else today
            if day is None or day > cutoff:
                await self._respond(update, "ok", headline, html=True)
                return
            try:
                tasks = await self.todoist.tasks_due_by(self.project.id, cutoff)
            except TodoistError:
                # The add itself succeeded — report it; only the list
                # refresh failed.
                await self._respond(
                    update,
                    "ok",
                    f"{headline}\n\n⚠ couldn't refresh the list — {LIST_CMDS}",
                    html=True,
                )
                return
            # "Will react", not "did react": if Telegram then rejects
            # the reaction the headline echo stays suppressed too —
            # zero status is the mandated no-fallback contract.
            confirmed = self._should_react(update)
            await send_list(
                update,
                context,
                tasks,
                today,
                cutoff,
                self.mentions,
                self.todoist,
                self.project.id,
                headline=headline,
                already_confirmed=confirmed,
            )
            if confirmed:
                # The list above is content and still goes out; the
                # reaction is the action's status line.
                await react(update, "ok")


class ListCommand(_Command):
    """One of /today, /tomorrow, /week — fetch and show a list."""

    def __init__(
        self,
        cfg: Config,
        todoist: TodoistClient,
        project: Project,
        days: int,
        name: str,
    ) -> None:
        super().__init__(cfg, todoist, project)
        self.days = days
        self.log_name = name

    async def run(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        today = self.cfg.today()
        cutoff = today + timedelta(days=self.days)
        tasks = await self.todoist.tasks_due_by(self.project.id, cutoff)
        await send_list(
            update,
            context,
            tasks,
            today,
            cutoff,
            self.mentions,
            self.todoist,
            self.project.id,
        )


class _ListAction(_Command):
    """A list-mutating command backed by the begin/commit FSM.

    run() owns the shared FSM shell — arg split, chat lock, require
    lists, begin, perform, commit, list re-edit, ok reply — so a
    subclass supplies only its distinct pieces: usage text, the
    begin event, the rejected reply, and the perform() mutation.
    The optional hooks (split, validate,
    rejected_debug, applied_verb, log_applied) have working
    defaults; add a mutating command by subclassing here — set
    log_name, implement usage/begin_event/rejected_reply/perform —
    and registering command_handler(MyCommand(cfg, todoist, project)).

    use_lock is False on purpose: the shell takes the chat lock
    itself, after the pre-lock split/validate replies.
    """

    require_user = True
    use_lock = False

    async def _require_lists(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int
    ) -> list[TaskList] | None:
        """Today's lists for this user (their /mine + the chat list).

        Callers hold the chat lock.  Returns None after replying why
        there is nothing to act on.
        """
        today = self.cfg.today()
        lists = active_lists(context.chat_data, user_id, today)
        if lists:
            return lists
        if get_chat_list(context.chat_data) is not None:
            # active_lists pruned stale /mine lists; a stale chat list
            # is cleared here so the message explains the expiry once.
            set_chat_list(context.chat_data, None)
            await self._respond(
                update,
                "soft",
                f"The last list is from a previous day — send {LIST_CMDS} " "or /mine",
            )
        else:
            await self._respond(update, "soft", f"Send {LIST_CMDS} or /mine first")
        return None

    async def _reply_begin_failure(
        self, update: Update, outcome: Unresolved | Ambiguous
    ) -> None:
        """Reply for begin's two resolve failures (verbatim log shapes
        and reply strings).

        The decision itself is state.begin (pure, tested) — this adds only
        the log line and the reply.
        """
        if isinstance(outcome, Unresolved):
            logger.debug(
                "%s: no match (%d state(s))", self.log_name, outcome.list_count
            )
            await self._respond(update, "soft", "No match in the last shown list")
            return
        logger.debug("%s: ambiguous, %d matches", self.log_name, outcome.count)
        await self._respond(
            update,
            "soft",
            f"Ambiguous — matches {outcome.count} tasks, be more specific",
        )

    def usage(self) -> str:
        raise NotImplementedError

    def begin_event(self, rest: str) -> Event:
        raise NotImplementedError

    def rejected_reply(self, rej: Rejected) -> str:
        raise NotImplementedError

    async def perform(
        self, update: Update, ok: Permitted, rest: str
    ) -> _Outcome | None:
        """Mutate Todoist and build the commit event + ok reply.

        Return None only after replying the abort yourself (the
        shell stops without committing).  TodoistError propagates:
        the shell replies the shared hard-failure line.
        """
        raise NotImplementedError

    def split(self, raw: str) -> tuple[str, str] | None:
        """(query, rest) for begin/perform — base splits `<id> <rest>`."""
        query, separator, rest = raw.partition(" ")
        if not separator or not rest.strip():
            return None
        return query, rest.strip()

    def validate(self, rest: str) -> str | None:
        """Pre-lock check; return a soft-reply text to abort with."""
        return None

    def rejected_debug(self, rej: Rejected) -> str | None:
        return None

    def applied_verb(self) -> str:
        return "applied"

    def log_applied(self, ok: Permitted, applied: Applied, outcome: _Outcome) -> None:
        logger.debug(
            "%s task=%s: %s, updated %d displaying list(s)",
            self.log_name,
            ok.task_id,
            self.applied_verb(),
            len(applied.touched),
        )

    async def run(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Shared shell for _ListAction commands — see the class doc."""
        chat = update.effective_chat
        user = update.effective_user
        if chat is None or user is None:
            return
        parts = self.split(" ".join(context.args or []).strip())
        if parts is None:
            await self._respond(update, "soft", self.usage())
            return
        query, rest = parts
        too_long = self.validate(rest)
        if too_long is not None:
            await self._respond(update, "soft", too_long)
            return
        async with chat_lock(chat.id):
            if await self._require_lists(update, context, user.id) is None:
                return
            outcome = begin(
                context.chat_data,
                user.id,
                self.cfg.today(),
                query.casefold(),
                self.begin_event(rest),
            )
            match outcome:
                case Unresolved() | Ambiguous() as fail:
                    await self._reply_begin_failure(update, fail)
                    return
                case Rejected() as rej:
                    debug = self.rejected_debug(rej)
                    if debug is not None:
                        logger.debug(
                            "%s task=%s: %s", self.log_name, rej.task_id, debug
                        )
                    await self._respond(update, "soft", self.rejected_reply(rej))
                    return
                case Permitted() as ok:
                    pass
            try:
                result = await self.perform(update, ok, rest)
            except TodoistError as exc:
                await self._respond(
                    update,
                    "hard",
                    f"⚠ {exc} — send {LIST_CMDS} or /mine to refresh",
                )
                return
            if result is None:
                return
            applied = commit(
                context.chat_data, user.id, self.cfg.today(), ok.task_id, result.event
            )
            # Unreachable in practice: sequential updates under the
            # chat lock keep the phase stable between begin and commit.
            assert isinstance(applied, Applied)
            self.log_applied(ok, applied, result)
            await edit_lists(
                update,
                context,
                applied.touched,
                self.mentions,
                get_tasks(context.chat_data),
            )
            await self._respond(update, "ok", result.success, html=True)


class DoneCommand(_ListAction):
    log_name = "cmd_done"

    def usage(self) -> str:
        return f"Usage: /done <id> — id shown by {LIST_CMDS} or /mine"

    def split(self, raw: str) -> tuple[str, str] | None:
        return (raw, "") if raw else None

    def begin_event(self, rest: str) -> Complete:
        return Complete()

    def rejected_reply(self, rej: Rejected) -> str:
        return f"Already completed — send {LIST_CMDS} to refresh"

    def rejected_debug(self, rej: Rejected) -> str | None:
        return "already done"

    async def perform(self, update: Update, ok: Permitted, rest: str) -> _Outcome:
        await self.todoist.complete_task(ok.task_id)
        next_due: date | None = None
        if ok.item.recurring:
            try:
                rolled = await self.todoist.get_task(ok.task_id)
                if rolled.due is not None:
                    day = rolled.due.date
                    next_due = day.date() if isinstance(day, datetime) else day
            except TodoistError:
                logger.warning("could not read next occurrence of %s", ok.task_id)
        # Commit the strike once (done + the rolled occurrence's
        # date for recurring tasks), then re-edit every list
        # displaying it.  Complete's due sentinel: UNSET leaves the
        # date alone — a bare done must not clear the line — while a
        # rolled recurring date replaces it.
        event = Complete(due=next_due if next_due is not None else UNSET)
        if ok.item.recurring and next_due is not None:
            return _Outcome(
                event=event,
                success=(
                    f"✅ {render_text(ok.item.text)} — "
                    f"next occurrence: {fmt_due(next_due, self.cfg.today())}"
                ),
            )
        return _Outcome(event=event, success=f"✅ {render_text(ok.item.text)}")

    def applied_verb(self) -> str:
        return "completed"


class UndoCommand(_ListAction):
    log_name = "cmd_undo"

    def usage(self) -> str:
        return f"Usage: /undo <id> — id shown by {LIST_CMDS} or /mine"

    def split(self, raw: str) -> tuple[str, str] | None:
        return (raw, "") if raw else None

    def begin_event(self, rest: str) -> Reopen:
        return Reopen()

    def rejected_reply(self, rej: Rejected) -> str:
        if rej.reason == "recurring":
            # Blanket refusal: undoing a rolled completion is
            # unsafe (the series already advanced); a series that
            # *ended* on the last occurrence could be undone, but
            # distinguishing the two needs extra state — accepted
            # false negative.
            return "⚠ can't undo a recurring completion"
        return "Not completed — nothing to undo"

    async def perform(self, update: Update, ok: Permitted, rest: str) -> _Outcome:
        await self.todoist.uncomplete_task(ok.task_id)
        return _Outcome(event=Reopen(), success=f"↩️ {render_text(ok.item.text)}")

    def applied_verb(self) -> str:
        return "reopened"


class EditCommand(_ListAction):
    log_name = "cmd_edit"

    def usage(self) -> str:
        return "Usage: /edit <id> <new text> — e.g. /edit 5c buy milk"

    def validate(self, rest: str) -> str | None:
        if len(rest) > 500:
            return "Task text too long — max 500 characters"
        return None

    def begin_event(self, rest: str) -> Edit:
        return Edit(text=rest)

    def rejected_reply(self, rej: Rejected) -> str:
        return "Already completed — can't edit"

    def rejected_debug(self, rej: Rejected) -> str | None:
        return "already done, refusing"

    async def perform(self, update: Update, ok: Permitted, rest: str) -> _Outcome:
        await self.todoist.update_task_content(ok.task_id, rest)
        return _Outcome(event=Edit(text=rest), success=f"✏️ {render_text(rest)}")

    def applied_verb(self) -> str:
        return "renamed"


class AssignCommand(_ListAction):
    log_name = "cmd_assign"

    def usage(self) -> str:
        return (
            "Usage: /assign <id> <assignee> — e.g. /assign 5c @abnv "
            "or /assign 5c Priya Singh"
        )

    def validate(self, rest: str) -> str | None:
        # Same cap as /due: without it the "not found" reply
        # interpolates rest!r and can exceed Telegram's 4096-char
        # message limit, surfacing a misleading send failure.
        if len(rest) > 150:
            return "Assignee too long — max 150 characters"
        return None

    def begin_event(self, rest: str) -> Assign:
        # begin validates the event's type only; the Todoist-canonical
        # assignee name is resolved in perform and carried by commit.
        return Assign(assignee=rest.strip())

    def rejected_reply(self, rej: Rejected) -> str:
        return "Already completed — can't assign"

    def rejected_debug(self, rej: Rejected) -> str | None:
        return "already done, refusing"

    async def perform(
        self, update: Update, ok: Permitted, rest: str
    ) -> _Outcome | None:
        # Raw mapping (original casing) — `self.mentions` here is the
        # casefolded render lookup and would store lowercase names.
        candidates = assignee_name_candidates(rest, self.cfg.assignee_mentions)
        # Resolve every candidate name (a bare @handle can map to
        # several Todoist names); genuine ambiguity is more than
        # one distinct collaborator.
        assignee_ids: list[str] = []
        for candidate in candidates:
            collaborator_id = await self.todoist.collaborator_id_for_name(
                self.project.id, candidate
            )
            if collaborator_id is not None and collaborator_id not in assignee_ids:
                assignee_ids.append(collaborator_id)
        if not assignee_ids:
            logger.debug(
                "cmd_assign task=%s: no collaborator for %d candidate(s)",
                ok.task_id,
                len(candidates),
            )
            await self._respond(
                update,
                "soft",
                f"Todoist collaborator {rest!r} not found on "
                "this project (or the lookup failed) — check the name or "
                "ASSIGNEE_MENTIONS",
            )
            return None
        if len(assignee_ids) > 1:
            logger.debug(
                "cmd_assign task=%s: ambiguous assignee, %d collaborators",
                ok.task_id,
                len(assignee_ids),
            )
            await self._respond(
                update,
                "soft",
                f"Ambiguous — {rest!r} matches "
                f"{len(assignee_ids)} collaborators, use their full Todoist name",
            )
            return None
        collaborator_id = assignee_ids[0]
        # Todoist-side casing wins for the stored/displayed name (a
        # candidate may be a casefolded mention key or the typed
        # spelling); the cache hit is guaranteed — the id was just
        # resolved through it.
        assignee_name = await self.todoist.collaborator_name(
            self.project.id, collaborator_id
        )
        if assignee_name is None:
            assignee_name = candidates[0]
        await self.todoist.update_task_assignee(ok.task_id, collaborator_id)
        # assignee_suffix is a line suffix (" — @handle"); the
        # confirmation already draws its own arrow separator.
        mention = assignee_suffix(assignee_name, self.mentions).removeprefix(" — ")
        return _Outcome(
            event=Assign(assignee=assignee_name),
            success=f"👤 {render_text(ok.item.text)} → {mention}",
            log_extra=collaborator_id,
        )

    def log_applied(self, ok: Permitted, applied: Applied, outcome: _Outcome) -> None:
        logger.debug(
            "%s task=%s collaborator=%s: assigned, updated %d list(s)",
            self.log_name,
            ok.task_id,
            outcome.log_extra,
            len(applied.touched),
        )


class DueCommand(_ListAction):
    log_name = "cmd_due"

    def usage(self) -> str:
        return (
            "Usage: /due <id> <when> — e.g. /due 5c tomorrow 5pm, "
            "/due 5c next monday or /due 5c no date"
        )

    def begin_event(self, rest: str) -> Reschedule:
        # begin validates the event's type only; the server-parsed
        # date and recurring flag are carried by perform's result.
        return Reschedule(due=None, recurring=False)

    def rejected_reply(self, rej: Rejected) -> str:
        return "Already completed — can't reschedule"

    def rejected_debug(self, rej: Rejected) -> str | None:
        return "already done, refusing"

    async def perform(
        self, update: Update, ok: Permitted, rest: str
    ) -> _Outcome | None:
        if len(rest) > 150:
            # Reject rather than truncate: a cut natural-language
            # string can parse to a valid but *different* date.
            await self._respond(
                update, "soft", "Date string too long — max 150 characters"
            )
            return None
        # Todoist's server-side parser resolves the string (same
        # parser as quick-add); garbled input comes back as a
        # TodoistError to the shell.
        updated = await self.todoist.update_task_due(ok.task_id, rest)
        day = effective_day(updated.due, updated.deadline, self.cfg.resolved_timezone)
        is_recurring = bool(updated.due and updated.due.is_recurring)
        logger.debug(
            "cmd_due task=%s: day=%s recurring=%s", ok.task_id, day, is_recurring
        )
        if day is None:
            success = f"📅 {render_text(ok.item.text)} → no date"
        else:
            success = (
                f"📅 {render_text(ok.item.text)} → {fmt_due(day, self.cfg.today())}"
            )
        return _Outcome(
            event=Reschedule(due=day, recurring=is_recurring), success=success
        )

    def log_applied(self, ok: Permitted, applied: Applied, outcome: _Outcome) -> None:
        # The day/recurring debug line fires in perform, pre-commit;
        # cmd_due has no post-commit line.
        pass


class MineCommand(_Command):
    """The sender's own tasks: due, then upcoming, then undated."""

    log_name = "cmd_mine"
    require_message = True
    require_user = True

    async def run(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        user = update.effective_user
        message = update.effective_message
        if chat is None or user is None or message is None:
            return
        assignee_name = todoist_name_for_user(
            user.id, user.username, self.cfg.assignee_mentions
        )
        if assignee_name is None:
            logger.debug("cmd_mine user=%s: no ASSIGNEE_MENTIONS mapping", user.id)
            await self._respond(
                update,
                "soft",
                "I can't match your Telegram account to a Todoist "
                "collaborator — set ASSIGNEE_MENTIONS "
                "(Todoist Name=@you or Name=<numeric id>) and retry",
            )
            return
        assignee_id = await self.todoist.collaborator_id_for_name(
            self.project.id, assignee_name
        )
        if assignee_id is None:
            logger.debug("cmd_mine user=%s: collaborator not in project cache", user.id)
            await self._respond(
                update,
                "soft",
                f"Todoist collaborator {assignee_name!r} not found on this "
                "project (or the lookup failed) — check ASSIGNEE_MENTIONS "
                "and retry /mine",
            )
            return
        today = self.cfg.today()
        due, upcoming, undated = await self.todoist.tasks_for_assignee(
            self.project.id, assignee_id, today
        )
        selected, dividers = take_mine(due, upcoming, undated, MINE_LIMIT)
        logger.debug(
            "cmd_mine user=%s: due=%d upcoming=%d undated=%d selected=%d",
            user.id,
            len(due),
            len(upcoming),
            len(undated),
            len(selected),
        )
        if not selected:
            set_mine_list(context.chat_data, user.id, None)
            await reply(update, "Nothing assigned to you 🎉")
            return
        entities = await build_entities(selected, self.todoist, self.project.id)
        chunks = render_list_message(
            list(entities),
            entities,
            today,
            self.mentions,
            show_dates=True,
            headline=MINE_TITLE,
            dividers=dividers,
        )
        message_id = await send_chunks(
            lambda text: message.reply_text(text, parse_mode=ParseMode.HTML),
            chunks,
        )
        put_tasks(context.chat_data, entities.values())
        set_mine_list(
            context.chat_data,
            user.id,
            TaskList(
                message_id=message_id,
                day=today,
                cutoff=today,
                task_ids=list(entities),
                headline=MINE_TITLE,
                show_dates=True,
                dividers=dividers,
            ),
        )


class ProjectCommand(_Command):
    """A link to the configured project — no lock, no typing."""

    log_name = "cmd_project"
    require_message = True
    use_lock = False
    typing = False

    async def run(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None:
            return
        await message.reply_text(
            f'📂 <a href="{html.escape(self.project.url, quote=True)}">'
            f"{html.escape(self.project.name)}</a> — open in Todoist",
            parse_mode=ParseMode.HTML,
        )


class UnknownCommand(_Command):
    """Soft 'unknown command' for anything unmatched — no lock."""

    log_name = "on_unknown"
    require_message = True
    use_lock = False
    typing = False

    async def run(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        text = message.text if message is not None else None
        first = (text or "").split(maxsplit=1)[0] if text else ""
        if "@" in first:
            # /cmd@SomeOtherBot — another bot's command, stay quiet.
            addressed = first.partition("@")[2].casefold()
            if addressed != bot_for_update(update).username.casefold():
                return
        await self._respond(update, "soft", "Unknown command — try /help")
