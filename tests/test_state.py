from __future__ import annotations

from datetime import date, timedelta

from iwcb_bot.state import (
    ListItem,
    ListState,
    active_states,
    all_mine_states,
    any_done,
    assignee_name_candidates,
    displayed_matches,
    find_item,
    get_chat_state,
    get_mine_states,
    mutation_states,
    prune_mine_states,
    resolve_ids,
    resolve_one,
    resolve_query,
    set_chat_state,
    set_mine_state,
    sync_item,
    take_mine,
    task_suffixes,
    todoist_name_for_user,
    union_ids,
)

TODAY = date(2026, 10, 9)


def _state(
    day: date = TODAY, items: list[ListItem] | None = None, message_id: int | None = 7
) -> ListState:
    return ListState(
        message_id=message_id,
        day=day,
        cutoff=day,
        items=items if items is not None else [],
    )


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
        chat = _state(items=[ListItem(task_id="6xc1", text="report", due=TODAY)])
        mine = _state(
            items=[
                ListItem(task_id="6bc1", text="one", due=TODAY),
                ListItem(task_id="6ddc1", text="two", due=TODAY),
            ]
        )
        assert displayed_matches([mine, chat], "c1") == ["6xc1"]

    def test_same_task_in_two_lists_counts_once(self) -> None:
        item_id = "6abcdef"
        chat = _state(items=[ListItem(task_id=item_id, text="t", due=TODAY)])
        mine = _state(items=[ListItem(task_id=item_id, text="t", due=TODAY)])
        # "ef" is the displayed suffix in both lists (same single item).
        assert displayed_matches([mine, chat], "ef") == [item_id]

    def test_two_different_displayed_matches_not_unique(self) -> None:
        # Each single-item list displays its last 2 chars, so both
        # show "c1" for different tasks — not unique, caller falls back.
        chat = _state(items=[ListItem(task_id="6xc1", text="a", due=TODAY)])
        mine = _state(items=[ListItem(task_id="6yc1", text="b", due=TODAY)])
        assert displayed_matches([chat, mine], "c1") == ["6xc1", "6yc1"]

    def test_query_not_a_displayed_suffix(self) -> None:
        chat = _state(items=[ListItem(task_id="6abcdef", text="t", due=TODAY)])
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


class TestSyncItem:
    def test_updates_every_copy_and_returns_touched_states(self) -> None:
        item_a = ListItem(task_id="ab", text="task", due=TODAY)
        item_b = ListItem(task_id="ab", text="task", due=TODAY)
        state_a = _state(items=[item_a])
        state_b = _state(items=[item_b])
        touched = sync_item([state_a, state_b], "ab", done=True)
        assert touched == [state_a, state_b]
        assert item_a.done and item_b.done

    def test_only_matching_task_mutated(self) -> None:
        other = ListItem(task_id="cd", text="other", due=TODAY)
        item = ListItem(task_id="ab", text="task", due=TODAY)
        state = _state(items=[item, other])
        touched = sync_item([state], "ab", text="new")
        assert touched == [state]
        assert item.text == "new"
        assert other.text == "other"

    def test_missing_task_touches_nothing(self) -> None:
        state = _state(items=[ListItem(task_id="ab", text="t", due=TODAY)])
        assert sync_item([state], "zz", done=True) == []

    def test_text_and_due_sync(self) -> None:
        item = ListItem(task_id="ab", text="old", due=TODAY)
        state = _state(items=[item])
        sync_item([state], "ab", text="new", due=date(2026, 10, 12))
        assert item.text == "new"
        assert item.due == date(2026, 10, 12)

    def test_due_none_clears_date(self) -> None:
        # /due <id> no date — explicit None means "clear", not "leave".
        item = ListItem(task_id="ab", text="t", due=TODAY)
        state = _state(items=[item])
        sync_item([state], "ab", due=None)
        assert item.due is None

    def test_due_default_leaves_date_alone(self) -> None:
        # A caller that says nothing about the date (e.g. /done on a
        # non-recurring task) must not clear it.
        item = ListItem(task_id="ab", text="t", due=TODAY)
        state = _state(items=[item])
        sync_item([state], "ab", done=True)
        assert item.due == TODAY

    def test_done_with_rolled_due_sets_both(self) -> None:
        item = ListItem(task_id="ab", text="t", due=TODAY)
        state = _state(items=[item])
        sync_item([state], "ab", done=True, due=date(2026, 10, 17))
        assert item.done is True
        assert item.due == date(2026, 10, 17)

    def test_none_fields_leave_copies_alone(self) -> None:
        item = ListItem(task_id="ab", text="t", due=TODAY, done=False)
        state = _state(items=[item])
        touched = sync_item([state], "ab")
        assert touched == [state]
        assert item.done is False and item.text == "t" and item.due == TODAY

    def test_assignee_sync(self) -> None:
        item = ListItem(task_id="ab", text="t", due=TODAY, assignee=None)
        state = _state(items=[item])
        touched = sync_item([state], "ab", assignee="Priya Singh")
        assert touched == [state]
        assert item.assignee == "Priya Singh"

    def test_assignee_sync_every_copy(self) -> None:
        item_a = ListItem(task_id="ab", text="t", due=TODAY)
        item_b = ListItem(task_id="ab", text="t", due=TODAY)
        state_a = _state(items=[item_a])
        state_b = _state(items=[item_b])
        touched = sync_item([state_a, state_b], "ab", assignee="Priya Singh")
        assert touched == [state_a, state_b]
        assert item_a.assignee == "Priya Singh"
        assert item_b.assignee == "Priya Singh"

    def test_assignee_none_leaves_copies_alone(self) -> None:
        item = ListItem(task_id="ab", text="t", due=TODAY, assignee="Old Name")
        state = _state(items=[item])
        sync_item([state], "ab", assignee=None)
        assert item.assignee == "Old Name"

    def test_recurring_flipped_on(self) -> None:
        # /due 5c every friday on a one-off — the flag must follow, or
        # later /done skips the roll fetch and /undo misses its guard.
        item = ListItem(task_id="ab", text="t", due=TODAY, recurring=False)
        state = _state(items=[item])
        sync_item([state], "ab", recurring=True)
        assert item.recurring is True

    def test_recurring_flipped_off(self) -> None:
        item = ListItem(task_id="ab", text="t", due=TODAY, recurring=True)
        state = _state(items=[item])
        sync_item([state], "ab", recurring=False)
        assert item.recurring is False

    def test_recurring_default_leaves_flag_alone(self) -> None:
        item = ListItem(task_id="ab", text="t", due=TODAY, recurring=True)
        state = _state(items=[item])
        sync_item([state], "ab", done=True)
        assert item.recurring is True


class TestAnyDone:
    def test_no_copies(self) -> None:
        assert any_done([], "ab") is False

    def test_undone_copy(self) -> None:
        state = _state(items=[ListItem(task_id="ab", text="t", due=TODAY)])
        assert any_done([state], "ab") is False

    def test_done_copy(self) -> None:
        state = _state(items=[ListItem(task_id="ab", text="t", due=TODAY, done=True)])
        assert any_done([state], "ab") is True

    def test_divergent_copies_any_done_wins(self) -> None:
        # Recurring task completed via one list; another list refreshed
        # and shows a fresh un-struck copy — /done must still refuse.
        struck = _state(items=[ListItem(task_id="ab", text="t", due=TODAY, done=True)])
        fresh = _state(items=[ListItem(task_id="ab", text="t", due=TODAY)])
        assert any_done([fresh, struck], "ab") is True
        assert any_done([struck, fresh], "ab") is True

    def test_other_task_done_irrelevant(self) -> None:
        state = _state(
            items=[
                ListItem(task_id="ab", text="t", due=TODAY),
                ListItem(task_id="cd", text="u", due=TODAY, done=True),
            ]
        )
        assert any_done([state], "ab") is False


class TestMineStates:
    def test_set_get_roundtrip(self) -> None:
        chat_data: dict[str, object] = {}
        state = _state()
        set_mine_state(chat_data, 42, state)
        assert get_mine_states(chat_data) == {42: state}

    def test_set_none_removes_entry_and_key(self) -> None:
        chat_data: dict[str, object] = {}
        set_mine_state(chat_data, 42, _state())
        set_mine_state(chat_data, 42, None)
        assert get_mine_states(chat_data) == {}
        assert "mine" not in chat_data

    def test_none_chat_data_is_noop(self) -> None:
        set_mine_state(None, 1, _state())
        assert get_mine_states(None) == {}

    def test_prune_drops_stale_keeps_today(self) -> None:
        chat_data: dict[str, object] = {}
        set_mine_state(chat_data, 1, _state(day=TODAY))
        set_mine_state(chat_data, 2, _state(day=date(2026, 10, 8)))
        prune_mine_states(chat_data, TODAY)
        assert set(get_mine_states(chat_data)) == {1}

    def test_prune_clears_key_when_all_stale(self) -> None:
        chat_data: dict[str, object] = {}
        set_mine_state(chat_data, 2, _state(day=date(2026, 10, 8)))
        prune_mine_states(chat_data, TODAY)
        assert "mine" not in chat_data

    def test_prune_keeps_unrelated_keys(self) -> None:
        chat_data: dict[str, object] = {"other": 1}
        prune_mine_states(chat_data, TODAY)
        assert chat_data == {"other": 1}


class TestActiveStates:
    def test_union_of_today_mine_and_chat(self) -> None:
        chat_data: dict[str, object] = {}
        mine = _state(items=[ListItem(task_id="ab", text="t", due=TODAY)])
        chat = _state(items=[ListItem(task_id="cd", text="u", due=TODAY)])
        set_mine_state(chat_data, 42, mine)
        set_chat_state(chat_data, chat)
        assert active_states(chat_data, 42, TODAY) == [mine, chat]

    def test_other_users_mine_excluded(self) -> None:
        chat_data: dict[str, object] = {}
        set_mine_state(chat_data, 1, _state())
        assert active_states(chat_data, 2, TODAY) == []

    def test_stale_chat_state_excluded(self) -> None:
        chat_data: dict[str, object] = {}
        set_chat_state(chat_data, _state(day=date(2026, 10, 8)))
        assert active_states(chat_data, 1, TODAY) == []

    def test_stale_mine_pruned_silently(self) -> None:
        chat_data: dict[str, object] = {}
        set_mine_state(chat_data, 1, _state(day=date(2026, 10, 8)))
        assert active_states(chat_data, 1, TODAY) == []
        assert "mine" not in chat_data

    def test_chat_state_roundtrip(self) -> None:
        chat_data: dict[str, object] = {}
        state = _state()
        set_chat_state(chat_data, state)
        assert get_chat_state(chat_data) is state
        set_chat_state(chat_data, None)
        assert get_chat_state(chat_data) is None


class TestAllMineStates:
    def test_returns_every_users_mine_list(self) -> None:
        chat_data: dict[str, object] = {}
        mine_a = _state(items=[ListItem(task_id="ab", text="t", due=TODAY)])
        mine_b = _state(items=[ListItem(task_id="cd", text="u", due=TODAY)])
        set_mine_state(chat_data, 1, mine_a)
        set_mine_state(chat_data, 2, mine_b)
        assert all_mine_states(chat_data, TODAY) == [mine_a, mine_b]

    def test_stale_lists_pruned(self) -> None:
        chat_data: dict[str, object] = {}
        set_mine_state(chat_data, 1, _state(day=date(2026, 10, 8)))
        today_state = _state()
        set_mine_state(chat_data, 2, today_state)
        assert all_mine_states(chat_data, TODAY) == [today_state]

    def test_empty_when_no_mine_lists(self) -> None:
        assert all_mine_states({}, TODAY) == []
        assert all_mine_states(None, TODAY) == []


class TestUnionIds:
    def test_empty_states(self) -> None:
        assert union_ids([]) == []

    def test_deduplicates_across_states_first_wins(self) -> None:
        a = _state(
            items=[ListItem(task_id="ab", text="t"), ListItem(task_id="cd", text="u")]
        )
        b = _state(
            items=[ListItem(task_id="cd", text="u2"), ListItem(task_id="ef", text="v")]
        )
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
        chat = _state(items=[ListItem(task_id="pp11", text="a")])
        mine = _state(
            items=[
                ListItem(task_id="qq11", text="b"),
                ListItem(task_id="r11", text="c"),
            ]
        )
        assert (
            len(resolve_ids(union_ids([chat, mine]), "11")) == 3
        )  # would be ambiguous
        assert resolve_query([chat, mine], "11") == ["pp11"]

    def test_two_displayed_matches_fall_back_to_union(self) -> None:
        a = _state(items=[ListItem(task_id="pp11", text="a")])
        b = _state(items=[ListItem(task_id="tt11", text="b")])
        assert resolve_query([a, b], "11") == ["pp11", "tt11"]

    def test_full_id_match_without_display(self) -> None:
        state = _state(items=[ListItem(task_id="abcdef", text="a")])
        assert resolve_query([state], "abcdef") == ["abcdef"]

    def test_no_match_returns_empty(self) -> None:
        state = _state(items=[ListItem(task_id="abcdef", text="a")])
        assert resolve_query([state], "zz") == []


class TestResolveOne:
    def test_none(self) -> None:
        assert resolve_one([], "ab") == ("none", [])

    def test_ok(self) -> None:
        state = _state(items=[ListItem(task_id="abcdef", text="a")])
        assert resolve_one([state], "abcdef") == ("ok", ["abcdef"])

    def test_ambiguous(self) -> None:
        state = _state(
            items=[
                ListItem(task_id="pp11", text="a"),
                ListItem(task_id="qq11", text="b"),
            ]
        )
        outcome, ids = resolve_one([state], "11")
        assert outcome == "ambiguous"
        assert ids == ["pp11", "qq11"]

    def test_displayed_precedence_yields_ok_over_ambiguous_union(self) -> None:
        chat = _state(items=[ListItem(task_id="pp11", text="a")])
        mine = _state(
            items=[
                ListItem(task_id="qq11", text="b"),
                ListItem(task_id="r11", text="c"),
            ]
        )
        assert resolve_one([chat, mine], "11") == ("ok", ["pp11"])


class TestFindItem:
    def test_finds_first_stored_copy(self) -> None:
        first = ListItem(task_id="ab", text="t")
        a = _state(items=[first, ListItem(task_id="cd", text="u")])
        b = _state(items=[ListItem(task_id="ab", text="t2")])
        assert find_item([a, b], "ab") is first

    def test_missing_id_returns_none(self) -> None:
        assert find_item([], "zz") is None
        assert (
            find_item([_state(items=[ListItem(task_id="ab", text="t")])], "zz") is None
        )


class TestMutationStates:
    def test_appends_other_users_mine_states(self) -> None:
        chat_state = _state(items=[ListItem(task_id="ab", text="t")])
        other = _state(items=[ListItem(task_id="ab", text="t")])
        chat_data: dict[str, object] = {}
        set_mine_state(chat_data, 222, other)
        result = mutation_states(chat_data, TODAY, [chat_state])
        assert len(result) == 2
        assert result[0] is chat_state
        assert result[1] is other

    def test_identity_dedup_includes_shared_state_once(self) -> None:
        # The sender's own /mine is already in the resolution states;
        # the merge must not visit the same object twice (sync_item
        # appends one entry per matching state).
        shared = _state(items=[ListItem(task_id="ab", text="t")])
        chat_data: dict[str, object] = {}
        set_mine_state(chat_data, 222, shared)
        result = mutation_states(chat_data, TODAY, [shared])
        assert len(result) == 1
        assert result[0] is shared

    def test_stale_mine_pruned_from_scope(self) -> None:
        stale = _state(
            day=TODAY - timedelta(days=1),
            items=[ListItem(task_id="ab", text="t")],
        )
        chat_data: dict[str, object] = {}
        set_mine_state(chat_data, 222, stale)
        assert mutation_states(chat_data, TODAY, []) == []

    def test_empty_inputs(self) -> None:
        assert mutation_states({}, TODAY, []) == []
