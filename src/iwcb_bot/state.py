"""Pure list-state helpers shared by the Telegram handlers.

Everything here is deliberately free of telegram/PTB imports so the
state model (items, per-chat and per-user lists, id resolution, item
sync across copies) can be unit-tested without mocks.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, TypeVar

T = TypeVar("T")

_CHAT_LIST_KEY = "list"
_MINE_LISTS_KEY = "mine"

# Section headers for /mine, in budget-fill order.
MINE_SECTIONS = (
    "\N{CALENDAR} Due",
    "\N{TEAR-OFF CALENDAR} Upcoming",
    "\N{SPIRAL CALENDAR PAD} No date",
)


@dataclass
class ListItem:
    task_id: str
    text: str
    # None for tasks with no due date or deadline (/mine's "No date"
    # section); chat lists only ever contain dated tasks.
    due: date | None = None
    done: bool = False
    recurring: bool = False
    assignee: str | None = None


@dataclass
class ListState:
    message_id: int | None
    day: date
    cutoff: date
    items: list[ListItem]
    headline: str | None = None
    # Every line carries its due date (/mine, and /tomorrow//week by
    # derivation); chat /today lines only date overdue items.
    show_dates: bool = False
    # Section headers for /mine: item-index → bold header text printed
    # above that line.  Chat lists use {}.
    dividers: dict[int, str] = field(default_factory=dict)


def task_suffixes(task_ids: list[str]) -> list[str]:
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


def resolve_ids(task_ids: list[str], folded: str) -> list[str]:
    """Match by exact casefolded id first, then by casefolded tail."""
    exact = [task_id for task_id in task_ids if task_id.casefold() == folded]
    if exact:
        return exact
    return [task_id for task_id in task_ids if task_id.casefold().endswith(folded)]


def displayed_matches(states: list[ListState], folded: str) -> list[str]:
    """Task ids whose *displayed* suffix in some state equals `folded`.

    Each list renders its own short suffixes (task_suffixes per
    message), so a label the bot printed can collide with a
    never-displayed tail in the other list once /done//undo//edit
    resolve across both.  When exactly one task's displayed suffix
    matches, that is the unambiguous answer; otherwise the caller
    falls back to resolve_ids over the union.  The same task shown in
    several lists counts once.
    """
    matches: list[str] = []
    for state in states:
        suffixes = task_suffixes([item.task_id for item in state.items])
        for item, suffix in zip(state.items, suffixes, strict=True):
            if suffix == folded and item.task_id not in matches:
                matches.append(item.task_id)
    return matches


def get_chat_state(
    chat_data: MutableMapping[str, Any] | None,
) -> ListState | None:
    """The chat-level list (from /today, /tomorrow, /week or the morning
    announcement), if any."""
    if chat_data is None:
        return None
    state = chat_data.get(_CHAT_LIST_KEY)
    return state if isinstance(state, ListState) else None


def set_chat_state(
    chat_data: MutableMapping[str, Any] | None, state: ListState | None
) -> None:
    if chat_data is None:
        return
    if state is None:
        chat_data.pop(_CHAT_LIST_KEY, None)
    else:
        chat_data[_CHAT_LIST_KEY] = state


def get_mine_states(
    chat_data: MutableMapping[str, Any] | None,
) -> dict[int, ListState]:
    """Per-user /mine lists keyed by Telegram user id."""
    if chat_data is None:
        return {}
    states = chat_data.get(_MINE_LISTS_KEY)
    return states if isinstance(states, dict) else {}


def set_mine_state(
    chat_data: MutableMapping[str, Any] | None,
    user_id: int,
    state: ListState | None,
) -> None:
    if chat_data is None:
        return
    states = get_mine_states(chat_data)
    if state is None:
        states.pop(user_id, None)
    else:
        states[user_id] = state
    if states:
        chat_data[_MINE_LISTS_KEY] = states
    else:
        chat_data.pop(_MINE_LISTS_KEY, None)


def prune_mine_states(chat_data: MutableMapping[str, Any] | None, today: date) -> None:
    """Drop per-user lists from previous days.

    Chat-level state expiry stays with the handlers (they clear it and
    explain why); /mine expiry is silent bookkeeping.
    """
    states = get_mine_states(chat_data)
    if chat_data is None or not states:
        return
    for user_id in [uid for uid, state in states.items() if state.day != today]:
        del states[user_id]
    if not states:
        chat_data.pop(_MINE_LISTS_KEY, None)


def active_states(
    chat_data: MutableMapping[str, Any] | None, user_id: int, today: date
) -> list[ListState]:
    """Today's lists a user's /done, /undo and /edit act on.

    The sender's own /mine list (if any) plus the chat-level list, both
    from today; stale /mine entries are pruned first.  This is the
    resolution scope — a user must not resolve ids from someone else's
    /mine list — not the mutation scope: mutations additionally sync
    into every user's /mine list via all_mine_states.
    """
    prune_mine_states(chat_data, today)
    states: list[ListState] = []
    mine = get_mine_states(chat_data).get(user_id)
    if mine is not None:
        states.append(mine)
    chat = get_chat_state(chat_data)
    if chat is not None and chat.day == today:
        states.append(chat)
    return states


def all_mine_states(
    chat_data: MutableMapping[str, Any] | None, today: date
) -> list[ListState]:
    """Every user's today-/mine list in the chat, pruned first.

    The mutation scope for /done, /undo and /edit: a completed task
    must strike in all users' /mine lists (an unstruck copy elsewhere
    would pass the already-done guard and complete a recurring task a
    second time).  Order follows insertion (users sorted by id would
    need sorting here; callers dedupe against the resolution states).
    """
    prune_mine_states(chat_data, today)
    return list(get_mine_states(chat_data).values())


def sync_item(
    states: list[ListState],
    task_id: str,
    *,
    done: bool | None = None,
    text: str | None = None,
    due: date | None = None,
) -> list[ListState]:
    """Apply a mutation to every stored copy of a task.

    The same task can appear in the chat list and in several users'
    /mine lists; each state holds its own ListItem copy, so a /done
    anywhere must strike everywhere.  Returns the states that contained
    the task (in input order) so the caller can re-edit their messages.
    """
    touched: list[ListState] = []
    for state in states:
        for item in state.items:
            if item.task_id != task_id:
                continue
            if done is not None:
                item.done = done
            if text is not None:
                item.text = text
            if due is not None:
                item.due = due
            touched.append(state)
            break
    return touched


def any_done(states: list[ListState], task_id: str) -> bool:
    """True when any stored copy of the task is marked done.

    Copies of a recurring task (same id, still active after completion)
    can diverge when one list is refreshed and another is not: the
    /done and /undo guards must consider every copy, not just the
    first — otherwise a re-run after a refresh can complete a recurring
    task a second time (closing the next occurrence).
    """
    return any(
        item.done for state in states for item in state.items if item.task_id == task_id
    )


def take_mine(
    due: list[T], upcoming: list[T], undated: list[T], limit: int
) -> tuple[list[T], dict[int, str]]:
    """Fill the /mine budget in section order, with divider positions.

    Due first, then upcoming, then undated; each section keeps its
    source order (the client pre-sorts by date, then content).  Returns
    the selected items and a map of selected-index → section header for
    the non-empty sections that made the cut.
    """
    selected: list[T] = []
    dividers: dict[int, str] = {}
    for section, title in zip((due, upcoming, undated), MINE_SECTIONS, strict=True):
        if len(selected) >= limit or not section:
            continue
        dividers[len(selected)] = title
        selected.extend(section[: limit - len(selected)])
    return selected, dividers


def todoist_name_for_user(
    user_id: int, username: str | None, mentions: dict[str, str]
) -> str | None:
    """Reverse-map a Telegram user to their configured Todoist name.

    `mentions` is the raw ASSIGNEE_MENTIONS mapping (Todoist name as
    configured → @handle or numeric id).  The numeric id matches first
    (it is stable); the @username is a fallback for handle-only
    mappings.  Returns the configured name key verbatim (original
    casing, for display in errors) — collaborators are matched
    case-insensitively downstream — or None.
    """
    for name, target in mentions.items():
        if target == str(user_id):
            return name
    if username:
        folded = f"@{username}".casefold()
        for name, target in mentions.items():
            if target.casefold() == folded:
                return name
    return None
