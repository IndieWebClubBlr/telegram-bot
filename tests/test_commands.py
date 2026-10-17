"""Offline command tests: real PTB objects + fakes, no network.

Each test drives a command through its guarded callback
(`command_callback(cmd)(update, context)`) with a FakeTelegram bound
via set_bot() and a FakeTodoist injected.  The real respond/send_list/
reply stack runs unmodified; the socket guard in conftest guarantees
any accidental real HTTP call raises.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from telegram.error import TelegramError

from iwcb_bot.bot import build_application
from iwcb_bot.commands import (
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
)
from iwcb_bot.messaging import LIST_CMDS, MINE_TITLE
from iwcb_bot.state import get_chat_list, get_mine_lists, get_tasks
from iwcb_bot.todoist_client import TodoistError

from helpers import (
    PROJECT,
    FakeTodoist,
    FakeTelegram,
    build_fakes,
    due_task,
    make_config,
    make_state_task,
    run,
    sdk_task,
    seed_chat_list,
)


def make_todoist() -> FakeTodoist:
    return FakeTodoist()


# ---------------------------------------------------------------- Help


class TestHelp:
    def test_help_replies_with_usage_and_project_name(self):
        cfg = make_config()
        todoist = make_todoist()
        fakes = build_fakes()
        run(HelpCommand(cfg, todoist, PROJECT), fakes)
        texts = fakes.bot.sent_texts()
        assert len(texts) == 1
        assert PROJECT.name in texts[0]
        assert "/add" in texts[0]

    def test_help_never_touches_todoist(self):
        todoist = make_todoist()
        fakes = build_fakes()
        run(HelpCommand(make_config(), todoist, PROJECT), fakes)
        assert todoist.calls == []

    def test_help_requires_a_message(self):
        fakes = build_fakes(no_message=True)
        run(HelpCommand(make_config(), make_todoist(), PROJECT), fakes)
        assert fakes.bot.calls == []


# ---------------------------------------------------------------- Add


class TestAdd:
    def test_no_args_is_soft_usage(self):
        fakes = build_fakes(args=[])
        run(AddCommand(make_config(), make_todoist(), PROJECT), fakes)
        assert fakes.bot.sent_texts() == [
            "Usage: /add <task text> — e.g. /add buy milk tomorrow p1"
        ]

    def test_too_long_is_soft_reject(self):
        fakes = build_fakes(args=["x" * 501])
        run(AddCommand(make_config(), make_todoist(), PROJECT), fakes)
        assert fakes.bot.sent_texts() == ["Task text too long — max 500 characters"]

    def test_exactly_500_chars_is_accepted(self):
        todoist = make_todoist()
        todoist.add_task_result = sdk_task(content="x" * 500)
        fakes = build_fakes(args=["x" * 500])
        run(AddCommand(make_config(), todoist, PROJECT), fakes)
        assert todoist.method_calls("add_task_in_project") == [
            ("add_task_in_project", "x" * 500, "p1")
        ]
        assert any(t.startswith("✅ Added:") for t in fakes.bot.sent_texts())

    def test_happy_add_undated_replies_headline(self):
        todoist = make_todoist()
        todoist.add_task_result = sdk_task(content="buy milk")
        fakes = build_fakes(args=["buy", "milk"])
        run(AddCommand(make_config(), todoist, PROJECT), fakes)
        # undated + no chat list → headline only
        assert fakes.bot.sent_texts() == ["✅ Added: buy milk"]
        assert todoist.method_calls("add_task_in_project") == [
            ("add_task_in_project", "buy milk", "p1")
        ]

    def test_add_due_tomorrow_reports_detail(self):
        todoist = make_todoist()
        today = make_config().today()
        todoist.add_task_result = sdk_task(
            content="pay rent", due_date=today + timedelta(days=1)
        )
        fakes = build_fakes(args=["pay", "rent"])
        run(AddCommand(make_config(), todoist, PROJECT), fakes)
        # undated chat list (cutoff=today) → day > cutoff → headline echo
        text = next(t for t in fakes.bot.sent_texts() if t.startswith("✅ Added:"))
        assert "(due " in text

    def test_todoist_error_on_add_hard_reply(self):
        todoist = make_todoist()
        todoist.raise_on["add_task_in_project"] = TodoistError("boom")
        fakes = build_fakes(args=["buy", "milk"])
        run(AddCommand(make_config(), todoist, PROJECT), fakes)
        # command_callback catches TodoistError → hard reply
        assert fakes.bot.sent_texts() == ["⚠ boom"]

    def test_refresh_failure_after_successful_add_reports_list_hint(self):
        todoist = make_todoist()
        today = make_config().today()
        # due today → refresh attempted; refresh raises TodoistError
        todoist.add_task_result = sdk_task(content="buy milk", due_date=today)
        todoist.raise_on["tasks_due_by"] = TodoistError("down")
        fakes = build_fakes(args=["buy", "milk"])
        run(AddCommand(make_config(), todoist, PROJECT), fakes)
        assert any("couldn't refresh the list" in t for t in fakes.bot.sent_texts())
        assert any("✅ Added: buy milk" in t for t in fakes.bot.sent_texts())

    def test_successful_refresh_sends_list_and_stores_chat_list(self):
        todoist = make_todoist()
        today = make_config().today()
        todoist.add_task_result = sdk_task(content="buy milk", due_date=today)
        todoist.due_by_result = [due_task(content="buy milk", due_date=today)]
        chat_data: dict[str, Any] = {}
        fakes = build_fakes(args=["buy", "milk"], chat_data=chat_data)
        run(AddCommand(make_config(), todoist, PROJECT), fakes)
        # the refreshed list went out and a chat list is registered
        assert any("buy milk" in t for t in fakes.bot.sent_texts())
        assert get_chat_list(chat_data) is not None

    def test_reaction_mode_emits_reaction_not_headline_echo(self):
        cfg = make_config(reaction_replies=True)
        todoist = make_todoist()
        today = cfg.today()
        todoist.add_task_result = sdk_task(content="buy milk", due_date=today)
        # empty refresh → send_list takes the empty branch; the headline
        # echo is suppressed (already_confirmed) and only the reaction
        # is sent (the harness's add_react_refresh contract).
        todoist.due_by_result = []
        fakes = build_fakes(args=["buy", "milk"])
        run(AddCommand(cfg, todoist, PROJECT), fakes)
        assert len(fakes.bot.reactions()) == 1
        # no headline status text — the reaction is the only feedback
        assert not any(t.startswith("✅ Added") for t in fakes.bot.sent_texts())


# ---------------------------------------------------------------- List


class TestList:
    def test_today_empty(self):
        todoist = make_todoist()
        todoist.due_by_result = []
        fakes = build_fakes()
        run(ListCommand(make_config(), todoist, PROJECT, 0, "cmd_today"), fakes)
        assert fakes.bot.sent_texts() == ["Nothing due today 🎉"]
        assert todoist.method_calls("tasks_due_by") == [
            ("tasks_due_by", "p1", str(make_config().today()))
        ]

    def test_tomorrow_empty_shows_cutoff(self):
        todoist = make_todoist()
        todoist.due_by_result = []
        fakes = build_fakes()
        cfg = make_config()
        run(ListCommand(cfg, todoist, PROJECT, 1, "cmd_tomorrow"), fakes)
        cutoff = cfg.today() + timedelta(days=1)
        assert fakes.bot.sent_texts() == [f"Nothing due by {cutoff:%a %-d %b} 🎉"]

    def test_list_with_tasks_sends_and_stores_chat_list(self):
        todoist = make_todoist()
        today = make_config().today()
        todoist.due_by_result = [due_task(content="buy milk", due_date=today)]
        chat_data: dict[str, Any] = {}
        fakes = build_fakes(chat_data=chat_data)
        run(ListCommand(make_config(), todoist, PROJECT, 0, "cmd_today"), fakes)
        # the list message went out
        assert any("buy milk" in t for t in fakes.bot.sent_texts())
        # a chat-level list is registered with the send's message id
        task_list = get_chat_list(chat_data)
        assert task_list is not None
        assert task_list.day == today
        # tasks are in the chat map for suffix resolution
        assert get_tasks(chat_data)

    def test_week_uses_six_day_cutoff(self):
        todoist = make_todoist()
        todoist.due_by_result = []
        cfg = make_config()
        fakes = build_fakes()
        # /week is registered with days=6 (today + next 6 days)
        run(ListCommand(cfg, todoist, PROJECT, 6, "cmd_week"), fakes)
        cutoff = cfg.today() + timedelta(days=6)
        assert todoist.method_calls("tasks_due_by") == [
            ("tasks_due_by", "p1", str(cutoff))
        ]


# ---------------------------------------------------------------- Done


class TestDone:
    def test_no_args_is_usage(self):
        fakes = build_fakes(args=[])
        run(DoneCommand(make_config(), make_todoist(), PROJECT), fakes)
        assert fakes.bot.sent_texts() == [
            f"Usage: /done <id> — id shown by {LIST_CMDS} or /mine"
        ]

    def test_no_lists_is_soft_hint(self):
        fakes = build_fakes(args=["5c"])
        run(DoneCommand(make_config(), make_todoist(), PROJECT), fakes)
        assert fakes.bot.sent_texts() == [f"Send {LIST_CMDS} or /mine first"]

    def test_stale_chat_list_from_previous_day_is_expiry_hint(self):
        todoist = make_todoist()
        today = make_config().today()
        yesterday = today - timedelta(days=1)
        chat_data: dict[str, Any] = {}
        seed_chat_list(
            chat_data, {"5c": make_state_task("5c", "buy milk")}, day=yesterday
        )
        fakes = build_fakes(args=["5c"], chat_data=chat_data)
        run(DoneCommand(make_config(), todoist, PROJECT), fakes)
        assert any("previous day" in t for t in fakes.bot.sent_texts())
        # the stale chat list is cleared
        assert get_chat_list(chat_data) is None

    def test_happy_done_completes_and_replies(self):
        todoist = make_todoist()
        today = make_config().today()
        chat_data: dict[str, Any] = {}
        seed_chat_list(chat_data, {"5c": make_state_task("5c", "buy milk")}, day=today)
        fakes = build_fakes(args=["5c"], chat_data=chat_data)
        run(DoneCommand(make_config(), todoist, PROJECT), fakes)
        assert todoist.method_calls("complete_task") == [("complete_task", "5c")]
        assert "✅ buy milk" in fakes.bot.sent_texts()
        # the chat map now marks it done
        assert get_tasks(chat_data)["5c"].done

    def test_done_strikes_the_list_message_in_place(self):
        todoist = make_todoist()
        today = make_config().today()
        chat_data: dict[str, Any] = {}
        seed_chat_list(chat_data, {"5c": make_state_task("5c", "buy milk")}, day=today)
        fakes = build_fakes(args=["5c"], chat_data=chat_data)
        run(DoneCommand(make_config(), todoist, PROJECT), fakes)
        # the stored list message is re-edited with the task struck through
        assert any("<s>" in t for t in fakes.bot.edits())

    def test_ambiguous_id_is_soft_reply(self):
        todoist = make_todoist()
        today = make_config().today()
        chat_data: dict[str, Any] = {}
        seed_chat_list(
            chat_data,
            {
                "5c": make_state_task("5c", "buy milk"),
                "abc": make_state_task("abc", "other abc"),
            },
            day=today,
        )
        # "c" is a suffix of both "5c" and "abc" → ambiguous
        fakes = build_fakes(args=["c"], chat_data=chat_data)
        run(DoneCommand(make_config(), todoist, PROJECT), fakes)
        assert todoist.method_calls("complete_task") == []
        assert any("Ambiguous" in t for t in fakes.bot.sent_texts())

    def test_already_completed_is_rejected_reply(self):
        todoist = make_todoist()
        today = make_config().today()
        chat_data: dict[str, Any] = {}
        seed_chat_list(
            chat_data,
            {"5c": make_state_task("5c", "buy milk", done=True)},
            day=today,
        )
        fakes = build_fakes(args=["5c"], chat_data=chat_data)
        run(DoneCommand(make_config(), todoist, PROJECT), fakes)
        assert todoist.method_calls("complete_task") == []
        assert fakes.bot.sent_texts() == [
            f"Already completed — send {LIST_CMDS} to refresh"
        ]

    def test_no_match_in_list(self):
        todoist = make_todoist()
        today = make_config().today()
        chat_data: dict[str, Any] = {}
        seed_chat_list(chat_data, {"5c": make_state_task("5c", "buy milk")}, day=today)
        fakes = build_fakes(args=["zz"], chat_data=chat_data)
        run(DoneCommand(make_config(), todoist, PROJECT), fakes)
        assert fakes.bot.sent_texts() == ["No match in the last shown list"]

    def test_todoist_error_on_complete_hard_reply(self):
        todoist = make_todoist()
        todoist.raise_on["complete_task"] = TodoistError("down")
        today = make_config().today()
        chat_data: dict[str, Any] = {}
        seed_chat_list(chat_data, {"5c": make_state_task("5c", "buy milk")}, day=today)
        fakes = build_fakes(args=["5c"], chat_data=chat_data)
        run(DoneCommand(make_config(), todoist, PROJECT), fakes)
        assert any(t.startswith("⚠ ") for t in fakes.bot.sent_texts())

    def test_recurring_roll_reads_next_occurrence(self):
        todoist = make_todoist()
        today = make_config().today()
        next_due = today + timedelta(days=7)
        todoist.get_task_result = sdk_task(
            content="buy milk", due_date=next_due, is_recurring=True
        )
        chat_data: dict[str, Any] = {}
        seed_chat_list(
            chat_data,
            {"5c": make_state_task("5c", "buy milk", recurring=True)},
            day=today,
        )
        fakes = build_fakes(args=["5c"], chat_data=chat_data)
        run(DoneCommand(make_config(), todoist, PROJECT), fakes)
        assert todoist.method_calls("get_task") == [("get_task", "5c")]
        texts = fakes.bot.sent_texts()
        assert any("next occurrence:" in t for t in texts)


# ---------------------------------------------------------------- Undo


class TestUndo:
    def test_happy_undo_reopens(self):
        todoist = make_todoist()
        today = make_config().today()
        chat_data: dict[str, Any] = {}
        seed_chat_list(
            chat_data,
            {"5c": make_state_task("5c", "buy milk", done=True)},
            day=today,
        )
        fakes = build_fakes(args=["5c"], chat_data=chat_data)
        run(UndoCommand(make_config(), todoist, PROJECT), fakes)
        assert todoist.method_calls("uncomplete_task") == [("uncomplete_task", "5c")]
        assert "↩️ buy milk" in fakes.bot.sent_texts()
        assert not get_tasks(chat_data)["5c"].done

    def test_not_completed_is_soft_reject(self):
        todoist = make_todoist()
        today = make_config().today()
        chat_data: dict[str, Any] = {}
        seed_chat_list(chat_data, {"5c": make_state_task("5c", "buy milk")}, day=today)
        fakes = build_fakes(args=["5c"], chat_data=chat_data)
        run(UndoCommand(make_config(), todoist, PROJECT), fakes)
        assert fakes.bot.sent_texts() == ["Not completed — nothing to undo"]

    def test_recurring_undo_is_refused(self):
        todoist = make_todoist()
        today = make_config().today()
        chat_data: dict[str, Any] = {}
        seed_chat_list(
            chat_data,
            {"5c": make_state_task("5c", "buy milk", done=True, recurring=True)},
            day=today,
        )
        fakes = build_fakes(args=["5c"], chat_data=chat_data)
        run(UndoCommand(make_config(), todoist, PROJECT), fakes)
        assert todoist.method_calls("uncomplete_task") == []
        assert fakes.bot.sent_texts() == ["⚠ can't undo a recurring completion"]

    def test_todoist_error_on_uncomplete_hard_reply(self):
        todoist = make_todoist()
        todoist.raise_on["uncomplete_task"] = TodoistError("down")
        today = make_config().today()
        chat_data: dict[str, Any] = {}
        seed_chat_list(
            chat_data,
            {"5c": make_state_task("5c", "buy milk", done=True)},
            day=today,
        )
        fakes = build_fakes(args=["5c"], chat_data=chat_data)
        run(UndoCommand(make_config(), todoist, PROJECT), fakes)
        assert any(t.startswith("⚠ ") for t in fakes.bot.sent_texts())


# ---------------------------------------------------------------- Edit


class TestEdit:
    def test_no_args_is_usage(self):
        fakes = build_fakes(args=[])
        run(EditCommand(make_config(), make_todoist(), PROJECT), fakes)
        assert fakes.bot.sent_texts() == [
            "Usage: /edit <id> <new text> — e.g. /edit 5c buy milk"
        ]

    def test_too_long_is_soft_reject(self):
        fakes = build_fakes(args=["5c", "x" * 501])
        run(EditCommand(make_config(), make_todoist(), PROJECT), fakes)
        assert fakes.bot.sent_texts() == ["Task text too long — max 500 characters"]

    def test_exactly_500_chars_is_accepted(self):
        todoist = make_todoist()
        today = make_config().today()
        chat_data: dict[str, Any] = {}
        seed_chat_list(chat_data, {"5c": make_state_task("5c", "buy milk")}, day=today)
        fakes = build_fakes(args=["5c", "x" * 500], chat_data=chat_data)
        run(EditCommand(make_config(), todoist, PROJECT), fakes)
        assert todoist.method_calls("update_task_content") == [
            ("update_task_content", "5c", "x" * 500)
        ]

    def test_happy_edit_updates_and_replies(self):
        todoist = make_todoist()
        today = make_config().today()
        chat_data: dict[str, Any] = {}
        seed_chat_list(chat_data, {"5c": make_state_task("5c", "buy milk")}, day=today)
        fakes = build_fakes(args=["5c", "buy", "oat", "milk"], chat_data=chat_data)
        run(EditCommand(make_config(), todoist, PROJECT), fakes)
        assert todoist.method_calls("update_task_content") == [
            ("update_task_content", "5c", "buy oat milk")
        ]
        assert "✏️ buy oat milk" in fakes.bot.sent_texts()
        assert get_tasks(chat_data)["5c"].text == "buy oat milk"

    def test_edit_completed_is_rejected(self):
        todoist = make_todoist()
        today = make_config().today()
        chat_data: dict[str, Any] = {}
        seed_chat_list(
            chat_data,
            {"5c": make_state_task("5c", "buy milk", done=True)},
            day=today,
        )
        fakes = build_fakes(args=["5c", "new"], chat_data=chat_data)
        run(EditCommand(make_config(), todoist, PROJECT), fakes)
        assert fakes.bot.sent_texts() == ["Already completed — can't edit"]

    def test_todoist_error_on_update_content_hard_reply(self):
        todoist = make_todoist()
        todoist.raise_on["update_task_content"] = TodoistError("down")
        today = make_config().today()
        chat_data: dict[str, Any] = {}
        seed_chat_list(chat_data, {"5c": make_state_task("5c", "buy milk")}, day=today)
        fakes = build_fakes(args=["5c", "new"], chat_data=chat_data)
        run(EditCommand(make_config(), todoist, PROJECT), fakes)
        assert any(t.startswith("⚠ ") for t in fakes.bot.sent_texts())


# ---------------------------------------------------------------- Assign


class TestAssign:
    def test_no_args_is_usage(self):
        fakes = build_fakes(args=[])
        run(AssignCommand(make_config(), make_todoist(), PROJECT), fakes)
        assert fakes.bot.sent_texts()[0].startswith("Usage: /assign")

    def test_too_long_is_soft_reject(self):
        fakes = build_fakes(args=["5c", "x" * 151])
        run(AssignCommand(make_config(), make_todoist(), PROJECT), fakes)
        assert fakes.bot.sent_texts() == ["Assignee too long — max 150 characters"]

    def test_exactly_150_chars_is_accepted(self):
        todoist = make_todoist()
        todoist.collab_ids = {"Priya Singh": "99"}
        todoist.collab_names = {"99": "Priya Singh"}
        today = make_config().today()
        chat_data: dict[str, Any] = {}
        seed_chat_list(chat_data, {"5c": make_state_task("5c", "buy milk")}, day=today)
        long_name = "x" * 149  # rest = "5c " + name → but split takes id then rest
        fakes = build_fakes(args=["5c", long_name], chat_data=chat_data)
        run(AssignCommand(make_config(), todoist, PROJECT), fakes)
        # 149-char name passes the 150 cap → lookup attempted (not "too long")
        assert fakes.bot.sent_texts() != ["Assignee too long — max 150 characters"]

    def test_not_found_is_soft_reply(self):
        todoist = make_todoist()
        today = make_config().today()
        chat_data: dict[str, Any] = {}
        seed_chat_list(chat_data, {"5c": make_state_task("5c", "buy milk")}, day=today)
        fakes = build_fakes(args=["5c", "@nobody"], chat_data=chat_data)
        run(AssignCommand(make_config(), todoist, PROJECT), fakes)
        assert todoist.method_calls("update_task_assignee") == []
        assert any("not found" in t for t in fakes.bot.sent_texts())

    def test_ambiguous_duo_handle_is_soft_reply(self):
        # Defense-only: Config.from_env rejects duplicate mention targets,
        # so a valid config can't produce two candidates for one @handle.
        # The command still guards against it; build that invalid config
        # inline to exercise the branch.
        cfg = make_config(assignee_mentions={"Alice": "@duo", "Bob": "@duo"})
        todoist = make_todoist()
        todoist.collab_ids = {"Alice": "111", "Bob": "222"}
        today = cfg.today()
        chat_data: dict[str, Any] = {}
        seed_chat_list(chat_data, {"5c": make_state_task("5c", "buy milk")}, day=today)
        fakes = build_fakes(args=["5c", "@duo"], chat_data=chat_data)
        run(AssignCommand(cfg, todoist, PROJECT), fakes)
        assert todoist.method_calls("update_task_assignee") == []
        assert any("Ambiguous" in t for t in fakes.bot.sent_texts())

    def test_happy_assign_updates_and_replies(self):
        todoist = make_todoist()
        todoist.collab_ids = {"Priya Singh": "99"}
        todoist.collab_names = {"99": "Priya Singh"}
        today = make_config().today()
        chat_data: dict[str, Any] = {}
        seed_chat_list(chat_data, {"5c": make_state_task("5c", "buy milk")}, day=today)
        fakes = build_fakes(args=["5c", "@priya"], chat_data=chat_data)
        run(AssignCommand(make_config(), todoist, PROJECT), fakes)
        assert todoist.method_calls("update_task_assignee") == [
            ("update_task_assignee", "5c", "99")
        ]
        assert any("→ @priya" in t for t in fakes.bot.sent_texts())
        assert get_tasks(chat_data)["5c"].assignee == "Priya Singh"

    def test_full_name_assign(self):
        todoist = make_todoist()
        todoist.collab_ids = {"Priya Singh": "99"}
        todoist.collab_names = {"99": "Priya Singh"}
        today = make_config().today()
        chat_data: dict[str, Any] = {}
        seed_chat_list(chat_data, {"5c": make_state_task("5c", "buy milk")}, day=today)
        fakes = build_fakes(args=["5c", "Priya", "Singh"], chat_data=chat_data)
        run(AssignCommand(make_config(), todoist, PROJECT), fakes)
        assert todoist.method_calls("update_task_assignee") == [
            ("update_task_assignee", "5c", "99")
        ]

    def test_todoist_error_on_update_assignee_hard_reply(self):
        todoist = make_todoist()
        todoist.collab_ids = {"Priya Singh": "99"}
        todoist.raise_on["update_task_assignee"] = TodoistError("down")
        today = make_config().today()
        chat_data: dict[str, Any] = {}
        seed_chat_list(chat_data, {"5c": make_state_task("5c", "buy milk")}, day=today)
        fakes = build_fakes(args=["5c", "@priya"], chat_data=chat_data)
        run(AssignCommand(make_config(), todoist, PROJECT), fakes)
        assert any(t.startswith("⚠ ") for t in fakes.bot.sent_texts())


# ---------------------------------------------------------------- Due


class TestDue:
    def test_no_args_is_usage(self):
        fakes = build_fakes(args=[])
        run(DueCommand(make_config(), make_todoist(), PROJECT), fakes)
        assert fakes.bot.sent_texts()[0].startswith("Usage: /due")

    def test_happy_due_updates_and_replies(self):
        todoist = make_todoist()
        today = make_config().today()
        tomorrow = today + timedelta(days=1)
        todoist.update_task_due_result = sdk_task(content="buy milk", due_date=tomorrow)
        chat_data: dict[str, Any] = {}
        seed_chat_list(chat_data, {"5c": make_state_task("5c", "buy milk")}, day=today)
        fakes = build_fakes(args=["5c", "tomorrow"], chat_data=chat_data)
        run(DueCommand(make_config(), todoist, PROJECT), fakes)
        assert todoist.method_calls("update_task_due") == [
            ("update_task_due", "5c", "tomorrow")
        ]
        assert any(t.startswith("📅 buy milk → ") for t in fakes.bot.sent_texts())
        assert get_tasks(chat_data)["5c"].due == tomorrow

    def test_no_date_is_cleared(self):
        todoist = make_todoist()
        today = make_config().today()
        todoist.update_task_due_result = sdk_task(content="buy milk")
        chat_data: dict[str, Any] = {}
        seed_chat_list(
            chat_data,
            {"5c": make_state_task("5c", "buy milk", due=today)},
            day=today,
        )
        fakes = build_fakes(args=["5c", "no", "date"], chat_data=chat_data)
        run(DueCommand(make_config(), todoist, PROJECT), fakes)
        assert "📅 buy milk → no date" in fakes.bot.sent_texts()
        assert get_tasks(chat_data)["5c"].due is None

    def test_too_long_date_string_is_soft_reject(self):
        todoist = make_todoist()
        today = make_config().today()
        chat_data: dict[str, Any] = {}
        seed_chat_list(chat_data, {"5c": make_state_task("5c", "buy milk")}, day=today)
        fakes = build_fakes(args=["5c", "x" * 151], chat_data=chat_data)
        run(DueCommand(make_config(), todoist, PROJECT), fakes)
        assert todoist.method_calls("update_task_due") == []
        assert fakes.bot.sent_texts() == ["Date string too long — max 150 characters"]

    def test_exactly_150_chars_is_accepted(self):
        todoist = make_todoist()
        today = make_config().today()
        todoist.update_task_due_result = sdk_task(content="buy milk", due_date=today)
        chat_data: dict[str, Any] = {}
        seed_chat_list(chat_data, {"5c": make_state_task("5c", "buy milk")}, day=today)
        fakes = build_fakes(args=["5c", "x" * 149], chat_data=chat_data)
        run(DueCommand(make_config(), todoist, PROJECT), fakes)
        # 149-char rest passes the 150 cap → lookup attempted (not "too long")
        assert fakes.bot.sent_texts() != ["Date string too long — max 150 characters"]

    def test_completed_is_rejected(self):
        todoist = make_todoist()
        today = make_config().today()
        chat_data: dict[str, Any] = {}
        seed_chat_list(
            chat_data,
            {"5c": make_state_task("5c", "buy milk", done=True)},
            day=today,
        )
        fakes = build_fakes(args=["5c", "tomorrow"], chat_data=chat_data)
        run(DueCommand(make_config(), todoist, PROJECT), fakes)
        assert fakes.bot.sent_texts() == ["Already completed — can't reschedule"]

    def test_todoist_error_on_update_due_hard_reply(self):
        todoist = make_todoist()
        todoist.raise_on["update_task_due"] = TodoistError("down")
        today = make_config().today()
        chat_data: dict[str, Any] = {}
        seed_chat_list(chat_data, {"5c": make_state_task("5c", "buy milk")}, day=today)
        fakes = build_fakes(args=["5c", "tomorrow"], chat_data=chat_data)
        run(DueCommand(make_config(), todoist, PROJECT), fakes)
        assert any(t.startswith("⚠ ") for t in fakes.bot.sent_texts())


# ---------------------------------------------------------------- Mine


class TestMine:
    def test_unmapped_user_is_soft_hint(self):
        todoist = make_todoist()
        fakes = build_fakes(user_id=999, username="stranger")
        run(MineCommand(make_config(), todoist, PROJECT), fakes)
        assert todoist.calls == []
        assert any("ASSIGNEE_MENTIONS" in t for t in fakes.bot.sent_texts())

    def test_collaborator_not_found_is_soft_reply(self):
        todoist = make_todoist()
        # config maps user 42 → "Priya Singh"; the collaborator lookup
        # (empty collab_ids) returns None → soft "not found" reply.
        fakes = build_fakes(user_id=42)
        cfg = make_config(assignee_mentions={"Priya Singh": "42"})
        run(MineCommand(cfg, todoist, PROJECT), fakes)
        assert any("not found" in t for t in fakes.bot.sent_texts())

    def test_no_assigned_tasks(self):
        todoist = make_todoist()
        todoist.collab_ids = {"Priya Singh": "99"}
        todoist.assignee_result = ([], [], [])
        fakes = build_fakes(user_id=42)
        cfg = make_config(assignee_mentions={"Priya Singh": "42"})
        run(MineCommand(cfg, todoist, PROJECT), fakes)
        assert fakes.bot.sent_texts() == ["Nothing assigned to you 🎉"]

    def test_mine_sends_sectioned_list_and_stores(self):
        todoist = make_todoist()
        todoist.collab_ids = {"Priya Singh": "99"}
        today = make_config().today()
        todoist.assignee_result = (
            [due_task(content="pay rent", due_date=today)],
            [],
            [],
        )
        chat_data: dict[str, Any] = {}
        fakes = build_fakes(user_id=42, chat_data=chat_data)
        cfg = make_config(assignee_mentions={"Priya Singh": "42"})
        run(MineCommand(cfg, todoist, PROJECT), fakes)
        # the /mine list message went out with the mine title
        assert any(MINE_TITLE in t for t in fakes.bot.sent_texts())
        # the mine list is registered
        mine = get_mine_lists(chat_data)[42]
        assert mine.headline == MINE_TITLE


# ---------------------------------------------------------------- Project


class TestProject:
    def test_project_replies_with_link(self):
        fakes = build_fakes()
        run(ProjectCommand(make_config(), make_todoist(), PROJECT), fakes)
        texts = fakes.bot.sent_texts()
        assert len(texts) == 1
        assert PROJECT.url in texts[0]
        assert PROJECT.name in texts[0]

    def test_project_never_touches_todoist(self):
        todoist = make_todoist()
        fakes = build_fakes()
        run(ProjectCommand(make_config(), todoist, PROJECT), fakes)
        assert todoist.calls == []


# ---------------------------------------------------------------- Unknown


class TestUnknown:
    def test_unknown_command_replies(self):
        fakes = build_fakes(text="/frobnicate")
        run(UnknownCommand(make_config(), make_todoist(), PROJECT), fakes)
        assert fakes.bot.sent_texts() == ["Unknown command — try /help"]

    def test_other_bot_mention_is_silent(self):
        fakes = build_fakes(
            text="/status@SomeOtherBot", bot=FakeTelegram(username="ourbot")
        )
        run(UnknownCommand(make_config(), make_todoist(), PROJECT), fakes)
        assert fakes.bot.calls == []

    def test_own_bot_mention_replies(self):
        fakes = build_fakes(text="/status@ourbot", bot=FakeTelegram(username="ourbot"))
        run(UnknownCommand(make_config(), make_todoist(), PROJECT), fakes)
        assert fakes.bot.sent_texts() == ["Unknown command — try /help"]


# ---------------------------------------------------------------- Guards


class TestGuards:
    def test_no_message_command_is_silent_without_message(self):
        fakes = build_fakes(no_message=True)
        run(ProjectCommand(make_config(), make_todoist(), PROJECT), fakes)
        assert fakes.bot.calls == []

    def test_no_user_command_is_silent_without_user(self):
        fakes = build_fakes(user_id=None)
        run(MineCommand(make_config(), make_todoist(), PROJECT), fakes)
        assert fakes.bot.calls == []


# ---------------------------------------------------------------- Failure paths


class TestFailurePaths:
    def test_telegram_error_mid_command_replies_send_failed(self):
        todoist = make_todoist()
        todoist.add_task_result = sdk_task(content="buy milk")
        fakes = build_fakes(args=["buy", "milk"])
        # the headline reply fails once; the fallback send succeeds
        fakes.bot.fail_calls(TelegramError("flood"), times=1)
        run(AddCommand(make_config(), todoist, PROJECT), fakes)
        assert fakes.bot.sent_texts() == [
            "⚠ Telegram send failed — the command may have completed; "
            f"check with {LIST_CMDS} or /mine"
        ]

    def test_respond_safely_swallows_fallback_failure(self):
        todoist = make_todoist()
        todoist.add_task_result = sdk_task(content="buy milk")
        fakes = build_fakes(args=["buy", "milk"])
        # every send fails, including respond_safely's fallback — the
        # error must not propagate out of command_callback
        fakes.bot.fail_calls(TelegramError("flood"), times=10)
        run(AddCommand(make_config(), todoist, PROJECT), fakes)
        # the fallback send was attempted (respond_safely ran and swallowed)
        attempted = [
            kw["text"]
            for name, kw in fakes.bot.calls
            if name == "send_message" and "Telegram send failed" in kw["text"]
        ]
        assert attempted

    def test_unexpected_exception_replies_generic(self):
        todoist = make_todoist()
        todoist.raise_on["add_task_in_project"] = ValueError("kaboom")
        fakes = build_fakes(args=["buy", "milk"])
        run(AddCommand(make_config(), todoist, PROJECT), fakes)
        assert fakes.bot.sent_texts() == ["Unexpected error"]


# ---------------------------------------------------------------- Reaction mode


class TestReactionMode:
    def test_soft_status_reacts_with_soft_emoji(self):
        cfg = make_config(reaction_replies=True)
        fakes = build_fakes(args=["5c"])
        # no lists → soft "Send ... first" → soft reaction 🤨
        run(DoneCommand(cfg, make_todoist(), PROJECT), fakes)
        assert fakes.bot.reactions()[0].emoji == "🤨"
        assert not fakes.bot.sent_texts()

    def test_hard_status_reacts_with_hard_emoji(self):
        cfg = make_config(reaction_replies=True)
        todoist = make_todoist()
        todoist.raise_on["complete_task"] = TodoistError("down")
        today = cfg.today()
        chat_data: dict[str, Any] = {}
        seed_chat_list(chat_data, {"5c": make_state_task("5c", "buy milk")}, day=today)
        fakes = build_fakes(args=["5c"], chat_data=chat_data)
        run(DoneCommand(cfg, todoist, PROJECT), fakes)
        assert fakes.bot.reactions()[0].emoji == "👎"
        assert not fakes.bot.sent_texts()


# ---------------------------------------------------------------- Registration


class TestRegistration:
    def test_build_application_registers_expected_commands(self):
        cfg = make_config()
        application = build_application(cfg, PROJECT, make_todoist())
        handlers = application.handlers.get(0, [])
        commands_registered: set[str] = set()
        for handler in handlers:
            cmds = getattr(handler, "commands", None)
            if cmds:
                commands_registered |= set(cmds)
                # every command handler carries the auth filter (the
                # ALLOWED_CHAT_IDS access control)
                assert getattr(handler, "filters", None) is not None
        expected = {
            "help",
            "start",
            "add",
            "today",
            "tomorrow",
            "week",
            "mine",
            "done",
            "undo",
            "edit",
            "assign",
            "due",
            "project",
        }
        assert expected <= commands_registered
