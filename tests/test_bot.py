from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast
from zoneinfo import ZoneInfo

from todoist_bot.bot import _send_morning_list, next_announce
from todoist_bot.state import get_chat_list, get_tasks, put_tasks

from helpers import (
    PROJECT,
    FakeTelegram,
    FakeTodoist,
    due_task,
    make_config,
    make_state_task,
    seed_chat_list,
)

_KOLKATA = ZoneInfo("Asia/Kolkata")  # UTC+5:30


class TestNextAnnounce:
    _AT = time(9, 0)

    def test_before_announce_time_same_day(self) -> None:
        now = datetime(2026, 10, 10, 8, 0, tzinfo=_KOLKATA)
        target = next_announce(self._AT, _KOLKATA, now)
        assert target == datetime(2026, 10, 10, 9, 0, tzinfo=_KOLKATA)

    def test_after_announce_time_rolls_to_tomorrow(self) -> None:
        now = datetime(2026, 10, 10, 10, 0, tzinfo=_KOLKATA)
        target = next_announce(self._AT, _KOLKATA, now)
        assert target == datetime(2026, 10, 11, 9, 0, tzinfo=_KOLKATA)

    def test_exactly_at_announce_time_rolls_to_tomorrow(self) -> None:
        now = datetime(2026, 10, 10, 9, 0, tzinfo=_KOLKATA)
        target = next_announce(self._AT, _KOLKATA, now)
        assert target == datetime(2026, 10, 11, 9, 0, tzinfo=_KOLKATA)

    def test_just_before_announce_time(self) -> None:
        now = datetime(2026, 10, 10, 8, 59, 59, tzinfo=_KOLKATA)
        target = next_announce(self._AT, _KOLKATA, now)
        assert target == datetime(2026, 10, 10, 9, 0, tzinfo=_KOLKATA)

    def test_just_after_midnight(self) -> None:
        now = datetime(2026, 10, 10, 0, 0, 1, tzinfo=_KOLKATA)
        target = next_announce(self._AT, _KOLKATA, now)
        assert target == datetime(2026, 10, 10, 9, 0, tzinfo=_KOLKATA)

    def test_utc_instant_converted_to_configured_zone(self) -> None:
        # 2026-10-10 02:30Z = 08:00 IST → target 09:00 IST.
        now = datetime(2026, 10, 10, 2, 30, tzinfo=timezone.utc)
        target = next_announce(self._AT, _KOLKATA, now)
        assert target == datetime(2026, 10, 10, 9, 0, tzinfo=_KOLKATA)
        # The absolute delay is 1h even though wall-clock subtraction
        # would agree here; the timestamp math matters across DST.
        assert target.timestamp() - now.timestamp() == 3600.0

    def test_dst_transition_target_uses_day_offset(self) -> None:
        # New York springs forward 2026-03-08 02:00 → 03:00 (EST→EDT).
        # From 2026-03-07 20:00 EST the next 09:00 is on the 8th, at
        # the post-transition -04:00 offset; the real delay is 12h
        # (wall-clock subtraction would say 13h).
        new_york = ZoneInfo("America/New_York")
        now = datetime(2026, 3, 7, 20, 0, tzinfo=new_york)
        target = next_announce(self._AT, new_york, now)
        assert target == datetime(2026, 3, 8, 9, 0, tzinfo=new_york)
        assert target.utcoffset() == timedelta(hours=-4)
        assert target.timestamp() - now.timestamp() == 12 * 3600.0


class TestSendMorningList:
    """The morning announcement body (_send_morning_list).

    Called directly with a fake Application; the loop/scheduling is not
    exercised here — next_announce covers the date math.
    """

    CHAT_ID = 1

    def _application(self, chat_data: dict[str, Any]) -> Any:
        return cast(
            Any,
            SimpleNamespace(
                chat_data={self.CHAT_ID: chat_data},
                bot=FakeTelegram(),
            ),
        )

    def _send(
        self,
        application: Any,
        todoist: FakeTodoist,
        tasks: list[Any],
        *,
        today: Any,
        send_when_empty: bool,
    ) -> None:
        asyncio.run(
            _send_morning_list(
                application,
                self.CHAT_ID,
                tasks,
                today,
                today,
                {},
                todoist,
                PROJECT.id,
                send_when_empty=send_when_empty,
            )
        )

    def test_sends_titled_list_and_stores_it(self) -> None:
        cfg = make_config()
        today = cfg.today()
        todoist = FakeTodoist()
        chat_data: dict[str, Any] = {}
        application = self._application(chat_data)
        self._send(
            application,
            todoist,
            [due_task("t1", "buy milk", due_date=today)],
            today=today,
            send_when_empty=True,
        )
        # the announcement goes out under the reminder title
        texts = application.bot.sent_texts()
        assert len(texts) == 1
        assert "Daily Task Reminder" in texts[0]
        assert "buy milk" in texts[0]
        # and becomes the chat's last shown list for /done, /undo, /edit
        stored = get_chat_list(chat_data)
        assert stored is not None
        assert stored.headline is not None
        assert "Daily Task Reminder" in stored.headline
        assert stored.task_ids == ["t1"]
        assert stored.day == today

    def test_empty_day_sends_congratulations_when_enabled(self) -> None:
        today = make_config().today()
        chat_data: dict[str, Any] = {}
        application = self._application(chat_data)
        self._send(
            application,
            FakeTodoist(),
            [],
            today=today,
            send_when_empty=True,
        )
        texts = application.bot.sent_texts()
        assert len(texts) == 1
        assert "Daily Task Reminder" in texts[0]
        assert "Nothing due today 🎉" in texts[0]

    def test_empty_day_is_silent_and_clears_stale_list(self) -> None:
        cfg = make_config()
        today = cfg.today()
        chat_data: dict[str, Any] = {}
        # yesterday's list is still stored; an empty quiet morning must
        # retire it so the old message stops updating.
        seed_chat_list(
            chat_data,
            {"5c": make_state_task("5c", "buy milk")},
            day=today - timedelta(days=1),
        )
        application = self._application(chat_data)
        self._send(
            application,
            FakeTodoist(),
            [],
            today=today,
            send_when_empty=False,
        )
        assert application.bot.calls == []
        assert get_chat_list(chat_data) is None

    def test_locally_done_task_is_not_reannounced(self) -> None:
        cfg = make_config()
        today = cfg.today()
        chat_data: dict[str, Any] = {}
        # a /done landed since the fetch: the record is done locally
        put_tasks(chat_data, [make_state_task("t1", "buy milk", due=today, done=True)])
        application = self._application(chat_data)
        self._send(
            application,
            FakeTodoist(),
            [due_task("t1", "buy milk", due_date=today)],
            today=today,
            send_when_empty=False,
        )
        assert application.bot.calls == []
        assert get_chat_list(chat_data) is None

    def test_next_occurrence_of_locally_done_recurring_is_announced(self) -> None:
        # Regression: completing yesterday's occurrence stores done=True
        # with due=yesterday and rolled_due=today.  Today's fetch is the
        # next occurrence and must still be announced.
        cfg = make_config()
        today = cfg.today()
        yesterday = today - timedelta(days=1)
        chat_data: dict[str, Any] = {}
        put_tasks(
            chat_data,
            [
                make_state_task(
                    "t1",
                    "water plants",
                    due=yesterday,
                    done=True,
                    recurring=True,
                    rolled_due=today,
                )
            ],
        )
        application = self._application(chat_data)
        self._send(
            application,
            FakeTodoist(),
            [due_task("t1", "water plants", due_date=today, is_recurring=True)],
            today=today,
            send_when_empty=True,
        )
        texts = application.bot.sent_texts()
        assert len(texts) == 1
        assert "water plants" in texts[0]
        assert "Nothing due today" not in texts[0]
        stored = get_chat_list(chat_data)
        assert stored is not None
        assert stored.task_ids == ["t1"]
        assert not get_tasks(chat_data)["t1"].done
