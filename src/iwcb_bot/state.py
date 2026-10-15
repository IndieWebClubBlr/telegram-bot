"""Pure list-state helpers shared by the Telegram handlers.

Everything here is deliberately free of telegram/PTB imports so the
state model can be unit-tested without mocks.  Storage is normalized:
one task map per chat (task id → Task entity) plus ordered id lists
(the chat list and each user's /mine) pointing into it — lists carry
presentation, the map carries task details, and every list write prunes
entities no stored list references anymore.
"""

from __future__ import annotations

from collections.abc import Iterable, MutableMapping
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Literal, TypeVar

T = TypeVar("T")

# resolve_one's decision: no match, more than one, or exactly one.
ResolutionOutcome = Literal["none", "ambiguous", "ok"]

_CHAT_LIST_KEY = "list"
_MINE_LISTS_KEY = "mine"
_TASKS_KEY = "tasks"


class _UnsetType:
    """Marker distinguishing "parameter not mentioned" from None.

    update_task's due needs both: None clears a task's date (/due no
    date), while a caller that says nothing about the date must leave
    it alone — impossible if None doubled as both meanings.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


UNSET = _UnsetType()

# Section headers for /mine, in budget-fill order.
MINE_SECTIONS = (
    "\N{CALENDAR} Due",
    "\N{TEAR-OFF CALENDAR} Upcoming",
    "\N{SPIRAL CALENDAR PAD} No date",
)


@dataclass
class Task:
    task_id: str
    text: str
    # None for tasks with no due date or deadline (/mine's "No date"
    # section); chat lists only ever contain dated tasks.
    due: date | None = None
    done: bool = False
    recurring: bool = False
    assignee: str | None = None


@dataclass
class TaskList:
    message_id: int | None
    day: date
    cutoff: date
    # Display order — ids into this chat's task map, never the
    # entities themselves (one record per task, shared by every list).
    task_ids: list[str]
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


def displayed_matches(states: list[TaskList], folded: str) -> list[str]:
    """Task ids whose *displayed* suffix in some list equals `folded`.

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
        suffixes = task_suffixes(state.task_ids)
        for task_id, suffix in zip(state.task_ids, suffixes, strict=True):
            if suffix == folded and task_id not in matches:
                matches.append(task_id)
    return matches


def union_ids(states: list[TaskList]) -> list[str]:
    """Task ids across all lists, deduplicated (first wins)."""
    return list(
        dict.fromkeys(task_id for state in states for task_id in state.task_ids)
    )


def resolve_query(states: list[TaskList], folded: str) -> list[str]:
    """Resolve a typed id against the given lists.

    Prefers an exact match of a *displayed* short suffix (the label
    the user copied from a message); a shown label can otherwise
    collide with a never-displayed tail in the other list and be
    rejected as ambiguous.  Falls back to exact/tail matching over
    the union of both lists.
    """
    displayed = displayed_matches(states, folded)
    if len(displayed) == 1:
        return displayed
    return resolve_ids(union_ids(states), folded)


def resolve_one(
    states: list[TaskList], folded: str
) -> tuple[ResolutionOutcome, list[str]]:
    """resolve_query reduced to the single-match decision /done, /undo,
    /edit, /assign and /due share.

    Returns ("none", []) when nothing matches, ("ambiguous", matches)
    for more than one, and ("ok", [task_id]) for exactly one.
    """
    resolved = resolve_query(states, folded)
    if not resolved:
        return "none", []
    if len(resolved) > 1:
        return "ambiguous", resolved
    return "ok", resolved


def get_tasks(chat_data: MutableMapping[str, Any] | None) -> dict[str, Task]:
    """The chat's live task map (task id → entity), if any.

    Returns the stored dict itself — callers write through it via
    put_tasks/update_task or prune_tasks, never a copy.
    """
    if chat_data is None:
        return {}
    tasks = chat_data.get(_TASKS_KEY)
    return tasks if isinstance(tasks, dict) else {}


def put_tasks(
    chat_data: MutableMapping[str, Any] | None, entities: Iterable[Task]
) -> None:
    """Upsert fetched entities into the map, keyed by task id.

    Builders run this BEFORE storing the list that references them, so
    every stored id always has its entity.
    """
    if chat_data is None:
        return
    tasks = get_tasks(chat_data)
    for task in entities:
        tasks[task.task_id] = task
    chat_data[_TASKS_KEY] = tasks


def prune_tasks(chat_data: MutableMapping[str, Any] | None) -> None:
    """Drop entities no stored list references.

    Runs at the end of every list write (set_chat_list, set_mine_list,
    prune_mine_lists): a list that is replaced or expires takes its
    entities with it unless another list still points at them.
    """
    if chat_data is None:
        return
    tasks = get_tasks(chat_data)
    if not tasks:
        return
    referenced: set[str] = set()
    chat = get_chat_list(chat_data)
    if chat is not None:
        referenced.update(chat.task_ids)
    for task_list in get_mine_lists(chat_data).values():
        referenced.update(task_list.task_ids)
    for task_id in [key for key in tasks if key not in referenced]:
        del tasks[task_id]


def get_chat_list(
    chat_data: MutableMapping[str, Any] | None,
) -> TaskList | None:
    """The chat-level list (from /today, /tomorrow, /week or the morning
    announcement), if any."""
    if chat_data is None:
        return None
    state = chat_data.get(_CHAT_LIST_KEY)
    return state if isinstance(state, TaskList) else None


def set_chat_list(
    chat_data: MutableMapping[str, Any] | None, task_list: TaskList | None
) -> None:
    if chat_data is None:
        return
    if task_list is None:
        chat_data.pop(_CHAT_LIST_KEY, None)
    else:
        chat_data[_CHAT_LIST_KEY] = task_list
    prune_tasks(chat_data)


def get_mine_lists(
    chat_data: MutableMapping[str, Any] | None,
) -> dict[int, TaskList]:
    """Per-user /mine lists keyed by Telegram user id."""
    if chat_data is None:
        return {}
    states = chat_data.get(_MINE_LISTS_KEY)
    return states if isinstance(states, dict) else {}


def set_mine_list(
    chat_data: MutableMapping[str, Any] | None,
    user_id: int,
    task_list: TaskList | None,
) -> None:
    if chat_data is None:
        return
    states = get_mine_lists(chat_data)
    if task_list is None:
        states.pop(user_id, None)
    else:
        states[user_id] = task_list
    if states:
        chat_data[_MINE_LISTS_KEY] = states
    else:
        chat_data.pop(_MINE_LISTS_KEY, None)
    prune_tasks(chat_data)


def prune_mine_lists(chat_data: MutableMapping[str, Any] | None, today: date) -> None:
    """Drop per-user lists from previous days.

    Chat-level list expiry stays with the handlers (they clear it and
    explain why); /mine expiry is silent bookkeeping.  Also runs the
    task-map prune so entities only stale /mine lists referenced go.
    """
    states = get_mine_lists(chat_data)
    if chat_data is None or not states:
        return
    for user_id in [uid for uid, state in states.items() if state.day != today]:
        del states[user_id]
    if not states:
        chat_data.pop(_MINE_LISTS_KEY, None)
    prune_tasks(chat_data)


def active_lists(
    chat_data: MutableMapping[str, Any] | None, user_id: int, today: date
) -> list[TaskList]:
    """Today's lists a user's /done, /undo and /edit act on.

    The sender's own /mine list (if any) plus the chat-level list, both
    from today; stale /mine entries are pruned first.  This is the
    resolution scope — a user must not resolve ids from someone else's
    /mine list.  Mutation needs no wider scope: it writes the shared
    task record once and every list sees it.
    """
    prune_mine_lists(chat_data, today)
    states: list[TaskList] = []
    mine = get_mine_lists(chat_data).get(user_id)
    if mine is not None:
        states.append(mine)
    chat = get_chat_list(chat_data)
    if chat is not None and chat.day == today:
        states.append(chat)
    return states


def is_done(tasks: dict[str, Task], task_id: str) -> bool:
    """The single record's done flag (transitional — begin/commit in Task 3).

    Replaces the old any_done vote across per-list copies: with one
    map record there is nothing to diverge, and the /done and /undo
    guards read it directly.  An id no list references anymore (so it
    is not in the map) reads False — the caller's resolution already
    rejected such ids before this is consulted.
    """
    item = tasks.get(task_id)
    return item is not None and item.done


def lists_displaying(
    chat_data: MutableMapping[str, Any] | None, today: date, task_id: str
) -> list[TaskList]:
    """Today's stored lists whose id order contains the task.

    Transitional — begin/commit owns this in Task 3.  The chat list
    first, then every user's /mine in insertion order — the same
    re-edit set the old sync_item fan-out returned, now derived from
    one record instead of copies.  Callers re-edit each returned
    list's message after a write.
    """
    out: list[TaskList] = []
    chat = get_chat_list(chat_data)
    if chat is not None and chat.day == today and task_id in chat.task_ids:
        out.append(chat)
    for task_list in get_mine_lists(chat_data).values():
        if task_list.day == today and task_id in task_list.task_ids:
            out.append(task_list)
    return out


def update_task(
    chat_data: MutableMapping[str, Any] | None,
    today: date,
    task_id: str,
    *,
    done: bool | None = None,
    text: str | None = None,
    due: date | None | _UnsetType = UNSET,
    assignee: str | None = None,
    recurring: bool | None = None,
) -> list[TaskList]:
    """Apply a mutation to the single map record and return its lists.

    Transitional shim until begin/commit (typed FSM) replaces it; the
    handlers already pass chat_data so the move is mechanical.  due
    follows sentinel semantics: UNSET (the default) leaves the date
    alone, a date sets it, and None clears it (/due no date).
    recurring is passed by /due, which can flip a task between one-off
    and a series.  The record must already be in the map — builders
    put entities before storing lists — so a missing id is a broken
    invariant, not a no-op.
    """
    tasks = get_tasks(chat_data)
    item = tasks.get(task_id)
    assert item is not None, f"task {task_id} missing from the chat's map"
    updates: dict[str, Any] = {}
    if done is not None:
        updates["done"] = done
    if text is not None:
        updates["text"] = text
    if not isinstance(due, _UnsetType):
        updates["due"] = due
    if assignee is not None:
        updates["assignee"] = assignee
    if recurring is not None:
        updates["recurring"] = recurring
    tasks[task_id] = replace(item, **updates)
    return lists_displaying(chat_data, today, task_id)


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


def assignee_name_candidates(raw: str, mentions: dict[str, str]) -> list[str]:
    """Todoist collaborator name(s) an /assign argument can refer to.

    `mentions` is the raw ASSIGNEE_MENTIONS mapping (Todoist name →
    @handle or numeric id).  A bare Telegram username (with or without
    a leading @) or a numeric id is matched against the mapping's
    targets and expands to every name configured for it (several names
    may share one handle; the caller resolves each to a collaborator
    and errors on genuine ambiguity).  Anything else is taken verbatim
    as a Todoist collaborator name (e.g. "Priya Singh").  Returns []
    for blank input.
    """
    stripped = raw.strip()
    if not stripped:
        return []
    folded = stripped.lstrip("@").casefold()
    matched = [
        name
        for name, target in mentions.items()
        if target.lstrip("@").casefold() == folded
    ]
    return matched or [stripped]
