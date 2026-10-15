from __future__ import annotations

from datetime import date, timedelta

import pytest

from iwcb_bot.state import (
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
    Task,
    TaskList,
    Unresolved,
    active_lists,
    apply_event,
    assignee_name_candidates,
    begin,
    commit,
    displayed_matches,
    get_chat_list,
    get_mine_lists,
    get_tasks,
    lists_displaying,
    prune_mine_lists,
    prune_tasks,
    put_tasks,
    reject_reason,
    resolve_ids,
    resolve_one,
    resolve_query,
    set_chat_list,
    set_mine_list,
    take_mine,
    task_phase,
    task_suffixes,
    todoist_name_for_user,
    union_ids,
)

TODAY = date(2026, 10, 9)


def _tasklist(
    ids: list[str] | None = None, day: date = TODAY, message_id: int | None = 7
) -> TaskList:
    return TaskList(
        message_id=message_id,
        day=day,
        cutoff=day,
        task_ids=ids if ids is not None else [],
    )


def _task(
    task_id: str,
    text: str = "t",
    due: date | None = TODAY,
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


def _chat_data(
    chat: TaskList | None = None,
    mine: dict[int, TaskList] | None = None,
    tasks: list[Task] | None = None,
) -> dict[str, object]:
    # Lists first, entities last: set_chat_list/set_mine_list prune the
    # map, so putting entities before both lists are stored would drop
    # the ones the second list still needs (real builders put-then-store
    # for a single list per command; fixtures store every list first).
    data: dict[str, object] = {}
    if chat is not None:
        set_chat_list(data, chat)
    for user_id, task_list in (mine or {}).items():
        set_mine_list(data, user_id, task_list)
    if tasks:
        put_tasks(data, tasks)
    return data


class TestTaskSuffixes:
    def test_empty(self) -> None:
        assert task_suffixes([]) == []

    def test_single_task_uses_minimum_length(self) -> None:
        assert task_suffixes(["abcdef"]) == ["ef"]

    def test_disjoint_ids(self) -> None:
        assert task_suffixes(["abc", "xyz"]) == ["bc", "yz"]

    def test_colliding_tails_grow(self) -> None:
        assert task_suffixes(["abc", "abcd"]) == ["bc", "cd"]

    def test_duplicate_ids_return_full_ids(self) -> None:
        assert task_suffixes(["abc", "abc"]) == ["abc", "abc"]

    def test_case_variant_tails_push_past_case(self) -> None:
        # Labels must never differ only by case: these grow until a
        # genuinely different character separates the tails.
        assert task_suffixes(["6QcJ", "6PcJ"]) == ["qcj", "pcj"]

    def test_output_is_lowercase_true_tail(self) -> None:
        ids = ["6AbCd", "6xYzF", "6aBcD2"]
        for task_id, label in zip(ids, task_suffixes(ids), strict=True):
            assert label == task_id.casefold()[-len(label) :]
            assert label == label.lower()
            assert len(label) >= 2

    def test_real_world_ids(self) -> None:
        ids = [
            "6c7jJ8QV3VJX6Pj5",
            "6hj3CxfMhh5MfHGj",
            "6hj7qxQ8Xq2VQV5C",
            "6hj7r3Qr2x75jmCj",
        ]
        assert task_suffixes(ids) == ["j5", "gj", "5c", "cj"]


class TestResolveIds:
    def test_exact_match_wins_over_tail_collision(self) -> None:
        assert resolve_ids(["abc", "xabc"], "abc") == ["abc"]

    def test_exact_match_is_case_insensitive(self) -> None:
        assert resolve_ids(["AbC", "xyz"], "abc") == ["AbC"]

    def test_tail_fallback(self) -> None:
        assert resolve_ids(["abcdef", "xyz"], "def") == ["abcdef"]

    def test_tail_fallback_is_case_insensitive(self) -> None:
        query = "ZX"  # as typed by the user; handlers casefold before resolving
        assert resolve_ids(["6zX", "6ax"], query.casefold()) == ["6zX"]

    def test_tail_matches_multiple(self) -> None:
        assert resolve_ids(["abc1", "ybc1"], "bc1") == ["abc1", "ybc1"]

    def test_no_match(self) -> None:
        assert resolve_ids(["abc"], "zz") == []

    def test_exact_match_returned_before_any_tail_matches(self) -> None:
        # "bc" is a tail of both, but an exact id "bc" exists.
        assert resolve_ids(["abc", "xbc", "bc"], "bc") == ["bc"]


class TestDisplayedMatches:
    def test_no_states(self) -> None:
        assert displayed_matches([], "ab") == []

    def test_shown_label_resolves_even_with_cross_list_collision(self) -> None:
        # Chat list shows [c1] for 6xc1 (its shortest unique tail);
        # /mine holds 6bc1 and 6ddc1, which display "bc1"/"dc1" but
        # both tail-match "c1" in the union.  Typing the shown "c1"
        # must resolve to the chat task, not go ambiguous.
        chat = _tasklist(ids=["6xc1"])
        mine = _tasklist(ids=["6bc1", "6ddc1"])
        assert displayed_matches([mine, chat], "c1") == ["6xc1"]

    def test_same_task_in_two_lists_counts_once(self) -> None:
        item_id = "6abcdef"
        chat = _tasklist(ids=[item_id])
        mine = _tasklist(ids=[item_id])
        # "ef" is the displayed suffix in both lists (same single item).
        assert displayed_matches([mine, chat], "ef") == [item_id]

    def test_two_different_displayed_matches_not_unique(self) -> None:
        # Each single-item list displays its last 2 chars, so both
        # show "c1" for different tasks — not unique, caller falls back.
        chat = _tasklist(ids=["6xc1"])
        mine = _tasklist(ids=["6yc1"])
        assert displayed_matches([chat, mine], "c1") == ["6xc1", "6yc1"]

    def test_query_not_a_displayed_suffix(self) -> None:
        chat = _tasklist(ids=["6abcdef"])
        # "abcdef" is the full id, not the displayed 2-char suffix.
        assert displayed_matches([chat], "abcdef") == []


class TestTodoistNameForUser:
    _MENTIONS = {"Abhinav Nair": "@abnv", "Priya Singh": "123456789"}

    def test_numeric_id_match_wins_first(self) -> None:
        assert (
            todoist_name_for_user(123456789, "priya_s", self._MENTIONS) == "Priya Singh"
        )

    def test_username_fallback_casefolded(self) -> None:
        assert todoist_name_for_user(999, "ABNV", self._MENTIONS) == "Abhinav Nair"

    def test_no_username_no_id_match(self) -> None:
        assert todoist_name_for_user(999, None, self._MENTIONS) is None

    def test_unmapped_user(self) -> None:
        assert todoist_name_for_user(999, "stranger", self._MENTIONS) is None

    def test_empty_mentions(self) -> None:
        assert todoist_name_for_user(1, "x", {}) is None

    def test_returns_configured_casing(self) -> None:
        # Displayed in the /mine error message — must be the operator's
        # configured spelling, not a casefolded key.
        mentions = {"JANE DOE": "@jane"}
        assert todoist_name_for_user(999, "jane", mentions) == "JANE DOE"


class TestAssigneeNameCandidates:
    _MENTIONS = {
        "Abhinav Nair": "@abnv",
        "Priya Singh": "123456789",
        "Ana Diaz": "@abnv",
    }

    def test_blank_input(self) -> None:
        assert assignee_name_candidates("   ", self._MENTIONS) == []

    def test_handle_with_at(self) -> None:
        assert assignee_name_candidates("@abnv", self._MENTIONS) == [
            "Abhinav Nair",
            "Ana Diaz",
        ]

    def test_handle_without_at(self) -> None:
        assert assignee_name_candidates("abnv", self._MENTIONS) == [
            "Abhinav Nair",
            "Ana Diaz",
        ]

    def test_handle_case_insensitive(self) -> None:
        assert assignee_name_candidates("@ABNV", self._MENTIONS) == [
            "Abhinav Nair",
            "Ana Diaz",
        ]

    def test_numeric_target(self) -> None:
        assert assignee_name_candidates("123456789", self._MENTIONS) == ["Priya Singh"]

    def test_multiword_todoist_name_passthrough(self) -> None:
        assert assignee_name_candidates("Priya Singh", self._MENTIONS) == [
            "Priya Singh"
        ]

    def test_unknown_handle_falls_back_to_verbatim(self) -> None:
        # A name that merely looks unmapped is still tried as a
        # Todoist collaborator name downstream.
        assert assignee_name_candidates("@stranger", self._MENTIONS) == ["@stranger"]

    def test_empty_mentions(self) -> None:
        assert assignee_name_candidates("Priya Singh", {}) == ["Priya Singh"]

    def test_padded_input_stripped(self) -> None:
        assert assignee_name_candidates("  @abnv  ", self._MENTIONS) == [
            "Abhinav Nair",
            "Ana Diaz",
        ]


class TestTakeMine:
    def test_budget_fills_due_first(self) -> None:
        due = ["d1", "d2"]
        upcoming = ["u1", "u2", "u3"]
        undated = ["n1"]
        selected, dividers = take_mine(due, upcoming, undated, limit=4)
        assert selected == ["d1", "d2", "u1", "u2"]
        assert dividers == {0: "📅 Due", 2: "📆 Upcoming"}

    def test_undated_fills_remaining(self) -> None:
        selected, dividers = take_mine([], ["u1"], ["n1", "n2"], limit=3)
        assert selected == ["u1", "n1", "n2"]
        assert dividers == {0: "📆 Upcoming", 1: "🗓 No date"}

    def test_empty_sections_have_no_dividers(self) -> None:
        selected, dividers = take_mine(["d1"], [], [], limit=10)
        assert selected == ["d1"]
        assert dividers == {0: "📅 Due"}

    def test_all_empty(self) -> None:
        selected, dividers = take_mine([], [], [], limit=10)
        assert selected == []
        assert dividers == {}

    def test_limit_zero(self) -> None:
        selected, dividers = take_mine(["d1"], ["u1"], ["n1"], limit=0)
        assert selected == []
        assert dividers == {}

    def test_budget_exhausted_before_later_section(self) -> None:
        selected, dividers = take_mine(["d1", "d2", "d3"], ["u1"], ["n1"], limit=3)
        assert selected == ["d1", "d2", "d3"]
        assert dividers == {0: "📅 Due"}


class TestTaskMap:
    def test_get_tasks_missing_is_empty(self) -> None:
        assert get_tasks({}) == {}
        assert get_tasks(None) == {}

    def test_put_upserts_by_id(self) -> None:
        data: dict[str, object] = {}
        put_tasks(data, [_task("ab", text="old"), _task("cd")])
        put_tasks(data, [_task("ab", text="new")])
        tasks = get_tasks(data)
        assert set(tasks) == {"ab", "cd"}
        assert tasks["ab"].text == "new"

    def test_prune_keeps_referenced_by_chat_and_mine(self) -> None:
        # Build lists first (fixture rule), then entities, then prune:
        # "ef" is referenced by nothing and must go; "ab"/"cd" stay.
        data: dict[str, object] = {}
        set_chat_list(data, _tasklist(ids=["ab"]))
        set_mine_list(data, 2, _tasklist(ids=["cd"]))
        put_tasks(data, [_task("ab"), _task("cd"), _task("ef")])
        prune_tasks(data)
        assert set(get_tasks(data)) == {"ab", "cd"}

    def test_set_chat_list_clear_frees_unreferenced(self) -> None:
        data: dict[str, object] = {}
        put_tasks(data, [_task("ab")])
        set_chat_list(data, _tasklist(ids=["ab"]))
        set_chat_list(data, None)
        assert get_tasks(data) == {}

    def test_setters_run_prune(self) -> None:
        data: dict[str, object] = {}
        put_tasks(data, [_task("zz")])
        set_chat_list(data, _tasklist(ids=["ab"]))
        # "ab" was never put, "zz" is unreferenced: prune kept nothing.
        assert get_tasks(data) == {}


class TestListsDisplaying:
    def test_empty_chat_data(self) -> None:
        assert lists_displaying({}, TODAY, "ab") == []

    def test_chat_only(self) -> None:
        chat = _tasklist(ids=["ab"])
        data = _chat_data(chat=chat)
        assert lists_displaying(data, TODAY, "ab") == [chat]

    def test_skips_list_without_the_task(self) -> None:
        chat = _tasklist(ids=["cd"])
        data = _chat_data(chat=chat)
        assert lists_displaying(data, TODAY, "ab") == []

    def test_includes_other_users_mine(self) -> None:
        chat = _tasklist(ids=["ab"])
        mine = _tasklist(ids=["ab", "cd"])
        other = _tasklist(ids=["ef"])
        data = _chat_data(chat=chat, mine={2: mine, 3: other})
        assert lists_displaying(data, TODAY, "ab") == [chat, mine]

    def test_skips_other_day_lists(self) -> None:
        stale_chat = _tasklist(ids=["ab"], day=date(2026, 10, 8))
        stale_mine = _tasklist(ids=["ab"], day=date(2026, 10, 8))
        data: dict[str, object] = {}
        set_chat_list(data, stale_chat)
        set_mine_list(data, 2, stale_mine)
        put_tasks(data, [_task("ab")])
        assert lists_displaying(data, TODAY, "ab") == []

    def test_mine_only_without_chat_list(self) -> None:
        mine = _tasklist(ids=["ab"])
        data = _chat_data(mine={2: mine})
        assert lists_displaying(data, TODAY, "ab") == [mine]


class TestMineLists:
    def test_set_get_roundtrip(self) -> None:
        chat_data: dict[str, object] = {}
        task_list = _tasklist()
        set_mine_list(chat_data, 42, task_list)
        assert get_mine_lists(chat_data) == {42: task_list}

    def test_set_none_removes_entry_and_key(self) -> None:
        chat_data: dict[str, object] = {}
        set_mine_list(chat_data, 42, _tasklist())
        set_mine_list(chat_data, 42, None)
        assert get_mine_lists(chat_data) == {}
        assert "mine" not in chat_data

    def test_none_chat_data_is_noop(self) -> None:
        set_mine_list(None, 1, _tasklist())
        assert get_mine_lists(None) == {}

    def test_prune_drops_stale_keeps_today(self) -> None:
        chat_data: dict[str, object] = {}
        set_mine_list(chat_data, 1, _tasklist(day=TODAY))
        set_mine_list(chat_data, 2, _tasklist(day=date(2026, 10, 8)))
        prune_mine_lists(chat_data, TODAY)
        assert set(get_mine_lists(chat_data)) == {1}

    def test_prune_clears_key_when_all_stale(self) -> None:
        chat_data: dict[str, object] = {}
        set_mine_list(chat_data, 2, _tasklist(day=date(2026, 10, 8)))
        prune_mine_lists(chat_data, TODAY)
        assert "mine" not in chat_data

    def test_prune_keeps_unrelated_keys(self) -> None:
        chat_data: dict[str, object] = {"other": 1}
        prune_mine_lists(chat_data, TODAY)
        assert chat_data == {"other": 1}

    def test_mine_setter_runs_prune(self) -> None:
        # Mirror of the chat-setter prune test: deleting the set_mine_list
        # prune hook would otherwise pass the battery unnoticed.
        data = _chat_data(chat=_tasklist(ids=["ab"]), tasks=[_task("ab"), _task("cd")])
        set_mine_list(data, 2, _tasklist(ids=["cd"]))
        assert set(get_tasks(data)) == {"ab", "cd"}  # both still referenced
        set_mine_list(data, 2, None)
        assert set(get_tasks(data)) == {"ab"}  # mine-only "cd" freed


class TestActiveLists:
    def test_union_of_today_mine_and_chat(self) -> None:
        chat_data: dict[str, object] = {}
        mine = _tasklist(ids=["ab"])
        chat = _tasklist(ids=["cd"])
        set_mine_list(chat_data, 42, mine)
        set_chat_list(chat_data, chat)
        assert active_lists(chat_data, 42, TODAY) == [mine, chat]

    def test_other_users_mine_excluded(self) -> None:
        chat_data: dict[str, object] = {}
        set_mine_list(chat_data, 1, _tasklist())
        assert active_lists(chat_data, 2, TODAY) == []

    def test_stale_chat_list_excluded(self) -> None:
        chat_data: dict[str, object] = {}
        set_chat_list(chat_data, _tasklist(day=date(2026, 10, 8)))
        assert active_lists(chat_data, 1, TODAY) == []

    def test_stale_mine_pruned_silently(self) -> None:
        chat_data: dict[str, object] = {}
        set_mine_list(chat_data, 1, _tasklist(day=date(2026, 10, 8)))
        assert active_lists(chat_data, 1, TODAY) == []
        assert "mine" not in chat_data

    def test_chat_list_roundtrip(self) -> None:
        chat_data: dict[str, object] = {}
        task_list = _tasklist()
        set_chat_list(chat_data, task_list)
        assert get_chat_list(chat_data) is task_list
        set_chat_list(chat_data, None)
        assert get_chat_list(chat_data) is None


class TestUnionIds:
    def test_empty_states(self) -> None:
        assert union_ids([]) == []

    def test_deduplicates_across_states_first_wins(self) -> None:
        a = _tasklist(ids=["ab", "cd"])
        b = _tasklist(ids=["cd", "ef"])
        assert union_ids([a, b]) == ["ab", "cd", "ef"]


class TestResolveQuery:
    """Displayed-suffix precedence over union tail matching."""

    def test_empty_states(self) -> None:
        assert resolve_query([], "ab") == []

    def test_displayed_unique_beats_colliding_union(self) -> None:
        # 'pp11' displays as '11' in its single-item list; 'qq11' sits
        # in a multi-item list where it displays as 'q11', so its
        # never-shown tail '11' collides in the union — the shown
        # label wins instead of resolving ambiguously.
        chat = _tasklist(ids=["pp11"])
        mine = _tasklist(ids=["qq11", "r11"])
        assert (
            len(resolve_ids(union_ids([chat, mine]), "11")) == 3
        )  # would be ambiguous
        assert resolve_query([chat, mine], "11") == ["pp11"]

    def test_two_displayed_matches_fall_back_to_union(self) -> None:
        a = _tasklist(ids=["pp11"])
        b = _tasklist(ids=["tt11"])
        assert resolve_query([a, b], "11") == ["pp11", "tt11"]

    def test_full_id_match_without_display(self) -> None:
        state = _tasklist(ids=["abcdef"])
        assert resolve_query([state], "abcdef") == ["abcdef"]

    def test_no_match_returns_empty(self) -> None:
        state = _tasklist(ids=["abcdef"])
        assert resolve_query([state], "zz") == []


class TestResolveOne:
    def test_none(self) -> None:
        assert resolve_one([], "ab") == ("none", [])

    def test_ok(self) -> None:
        state = _tasklist(ids=["abcdef"])
        assert resolve_one([state], "abcdef") == ("ok", ["abcdef"])

    def test_ambiguous(self) -> None:
        state = _tasklist(ids=["pp11", "qq11"])
        outcome, ids = resolve_one([state], "11")
        assert outcome == "ambiguous"
        assert ids == ["pp11", "qq11"]

    def test_displayed_precedence_yields_ok_over_ambiguous_union(self) -> None:
        chat = _tasklist(ids=["pp11"])
        mine = _tasklist(ids=["qq11", "r11"])
        assert resolve_one([chat, mine], "11") == ("ok", ["pp11"])


class TestTaskPhase:
    def test_missing_is_active_by_invariant(self) -> None:
        assert task_phase({}, "ab") == "active"

    def test_flag_maps_to_phase(self) -> None:
        assert task_phase({"ab": _task("ab")}, "ab") == "active"
        assert task_phase({"ab": _task("ab", done=True)}, "ab") == "completed"


class TestRejectReason:
    def test_complete_active_permitted_completed_rejected(self) -> None:
        assert reject_reason({"ab": _task("ab")}, "ab", Complete()) is None
        done = {"ab": _task("ab", done=True)}
        assert reject_reason(done, "ab", Complete()) == "already_completed"

    def test_reopen_active_rejected_completed_permitted(self) -> None:
        active = {"ab": _task("ab")}
        assert reject_reason(active, "ab", Reopen()) == "not_completed"
        done = {"ab": _task("ab", done=True)}
        assert reject_reason(done, "ab", Reopen()) is None

    def test_reopen_recurring_record_rejected(self) -> None:
        # Phase first (partner ruling): an ACTIVE recurring task reads
        # not_completed, so /undo's wording stays byte-identical.
        active = {"ab": _task("ab", recurring=True)}
        assert reject_reason(active, "ab", Reopen()) == "not_completed"
        done = {"ab": _task("ab", done=True, recurring=True)}
        assert reject_reason(done, "ab", Reopen()) == "recurring"

    def test_modify_completed_rejected_active_permitted(self) -> None:
        events: list[Event] = [
            Edit(text="x"),
            Assign(assignee="Priya Singh"),
            Reschedule(due=None, recurring=False),
        ]
        active = {"ab": _task("ab")}
        done = {"ab": _task("ab", done=True)}
        for event in events:
            assert reject_reason(active, "ab", event) is None
            assert reject_reason(done, "ab", event) == "already_completed"


class TestApplyEvent:
    def test_complete_unset_keeps_due(self) -> None:
        out = apply_event(_task("ab", due=TODAY), Complete())
        assert out.done is True
        assert out.due == TODAY

    def test_complete_date_sets_due_and_strikes(self) -> None:
        rolled = TODAY + timedelta(days=1)
        out = apply_event(_task("ab", due=TODAY), Complete(due=rolled))
        assert out.done is True
        assert out.due == rolled

    def test_reopen_unstrikes_keeps_fields(self) -> None:
        item = _task("ab", text="milk", due=TODAY, done=True, assignee="Priya")
        out = apply_event(item, Reopen())
        assert out == _task("ab", text="milk", due=TODAY, done=False, assignee="Priya")

    def test_edit_replaces_text_only(self) -> None:
        out = apply_event(_task("ab", text="old", due=TODAY), Edit(text="new"))
        assert out == _task("ab", text="new", due=TODAY)

    def test_assign_sets_assignee(self) -> None:
        out = apply_event(_task("ab"), Assign(assignee="Priya Singh"))
        assert out.assignee == "Priya Singh"

    def test_reschedule_sets_due_and_recurring(self) -> None:
        later = TODAY + timedelta(days=3)
        out = apply_event(_task("ab", due=TODAY), Reschedule(due=later, recurring=True))
        assert out.due == later
        assert out.recurring is True

    def test_reschedule_none_clears_due(self) -> None:
        out = apply_event(_task("ab"), Reschedule(due=None, recurring=False))
        assert out.due is None

    def test_reschedule_flips_recurring_off(self) -> None:
        # /due a rolled series back to a one-off — the flag must follow,
        # or later /undo hits the recurring refusal it should not.
        out = apply_event(
            _task("ab", due=TODAY, recurring=True),
            Reschedule(due=TODAY, recurring=False),
        )
        assert out.recurring is False


class TestBegin:
    def test_unresolved_reports_list_count(self) -> None:
        data = _chat_data(chat=_tasklist(["ab"]), tasks=[_task("ab")])
        assert begin(data, 1, TODAY, "zz", Edit(text="x")) == Unresolved(list_count=1)

    def test_ambiguous_reports_count(self) -> None:
        data = _chat_data(
            chat=_tasklist(["abx", "cbx"]), tasks=[_task("abx"), _task("cbx")]
        )
        assert begin(data, 1, TODAY, "bx", Edit(text="x")) == Ambiguous(count=2)

    def test_permitted_returns_task_and_entity(self) -> None:
        milk = _task("ab", text="milk")
        data = _chat_data(chat=_tasklist(["ab"]), tasks=[milk])
        assert begin(data, 1, TODAY, "ab", Edit(text="x")) == Permitted(
            task_id="ab", item=milk
        )

    def test_rejected_carries_reason_and_task_id(self) -> None:
        data = _chat_data(chat=_tasklist(["ab"]), tasks=[_task("ab", done=True)])
        assert begin(data, 1, TODAY, "ab", Complete()) == Rejected(
            reason="already_completed", task_id="ab"
        )

    def test_stale_mine_pruned_before_resolving(self) -> None:
        data = _chat_data(
            mine={1: _tasklist(["cd"], day=TODAY - timedelta(days=1))},
            tasks=[_task("cd")],
        )
        assert begin(data, 1, TODAY, "cd", Edit(text="x")) == Unresolved(list_count=0)
        assert get_mine_lists(data) == {}
        assert get_tasks(data) == {}  # prune_tasks ran with the stale list gone

    def test_writes_nothing(self) -> None:
        chat = _tasklist(["ab"])
        mine = {2: _tasklist(["ab"])}
        entity = _task("ab")
        data = _chat_data(chat=chat, mine=mine, tasks=[entity])
        assert begin(data, 1, TODAY, "ab", Complete()) == Permitted(
            task_id="ab", item=entity
        )
        assert get_chat_list(data) is chat
        assert get_mine_lists(data) == mine
        assert get_tasks(data) == {"ab": entity}

    def test_missing_entity_is_a_programming_error(self) -> None:
        # A resolved id whose entity vanished breaks the storage
        # invariant — assert, never a silent Not-found-later path.
        data = _chat_data(chat=_tasklist(ids=["zz"]))
        with pytest.raises(AssertionError, match="missing from the chat's map"):
            begin(data, 1, TODAY, "zz", Complete())


class TestCommit:
    def test_single_map_write_strikes_record(self) -> None:
        chat = _tasklist(["ab"])
        data = _chat_data(chat=chat, tasks=[_task("ab")])
        outcome = commit(data, 1, TODAY, "ab", Complete())
        assert outcome == Applied(touched=[chat])
        assert get_tasks(data)["ab"].done is True

    def test_complete_unset_keeps_due(self) -> None:
        data = _chat_data(chat=_tasklist(["ab"]), tasks=[_task("ab", due=TODAY)])
        commit(data, 1, TODAY, "ab", Complete())
        item = get_tasks(data)["ab"]
        assert item.done is True
        assert item.due == TODAY

    def test_reopen_clears_record(self) -> None:
        data = _chat_data(chat=_tasklist(["ab"]), tasks=[_task("ab", done=True)])
        outcome = commit(data, 1, TODAY, "ab", Reopen())
        assert isinstance(outcome, Applied)
        assert get_tasks(data)["ab"].done is False

    def test_reschedule_none_clears_due(self) -> None:
        data = _chat_data(chat=_tasklist(["ab"]), tasks=[_task("ab", due=TODAY)])
        commit(data, 1, TODAY, "ab", Reschedule(due=None, recurring=False))
        assert get_tasks(data)["ab"].due is None

    def test_edit_rejected_when_completed_in_between(self) -> None:
        data = _chat_data(chat=_tasklist(["ab"]), tasks=[_task("ab", text="old")])
        first = commit(data, 1, TODAY, "ab", Complete())
        assert isinstance(first, Applied)
        second = commit(data, 1, TODAY, "ab", Edit(text="new"))
        assert second == Rejected(reason="already_completed", task_id="ab")
        item = get_tasks(data)["ab"]
        assert item.text == "old"  # rejection wrote nothing
        assert item.done is True

    def test_commit_touched_includes_other_users_mine(self) -> None:
        chat = _tasklist(["ab"])
        mine2 = _tasklist(["ab"])
        mine3 = _tasklist(["ab"])
        data = _chat_data(chat=chat, mine={2: mine2, 3: mine3}, tasks=[_task("ab")])
        outcome = commit(data, 1, TODAY, "ab", Complete())
        assert outcome == Applied(touched=[chat, mine2, mine3])

    def test_replaces_entity_not_mutates(self) -> None:
        original = _task("ab", text="old")
        data = _chat_data(chat=_tasklist(["ab"]), tasks=[original])
        commit(data, 1, TODAY, "ab", Edit(text="new"))
        stored = get_tasks(data)["ab"]
        assert stored is not original
        assert original.text == "old"

    def test_missing_entity_is_a_programming_error(self) -> None:
        data = _chat_data(chat=_tasklist(ids=["zz"]))
        with pytest.raises(AssertionError, match="missing from the chat's map"):
            commit(data, 1, TODAY, "zz", Complete())
