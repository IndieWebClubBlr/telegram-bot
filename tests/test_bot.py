from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from iwcb_bot.bot import next_announce

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
