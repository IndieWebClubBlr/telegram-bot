from __future__ import annotations

from datetime import date, timezone
from zoneinfo import ZoneInfo

from todoist_api_python.models import Deadline, Due

from todoist_bot.todoist_client import _project_token, effective_day

_KOLKATA = ZoneInfo("Asia/Kolkata")  # UTC+5:30
_UTC = timezone.utc


class TestProjectToken:
    def test_plain_name(self) -> None:
        assert _project_token("Work") == "#Work"

    def test_leading_hash_in_name_not_special_cased(self) -> None:
        assert _project_token("#Work") == "##Work"

    def test_spaces_escaped(self) -> None:
        assert _project_token("My Project") == r"#My\ Project"

    def test_multiple_spaces_escaped(self) -> None:
        assert _project_token("My  Project") == r"#My\ \ Project"

    def test_backslash_escaped_before_spaces(self) -> None:
        assert _project_token("a\\b c") == r"#a\\b\ c"

    def test_trailing_space_escaped(self) -> None:
        assert _project_token("Work ") == r"#Work\ "


def _due(day: date) -> Due:
    return Due.from_dict({"date": day.isoformat(), "string": day.isoformat()})


def _deadline(day: date) -> Deadline:
    return Deadline.from_dict({"date": day.isoformat()})


class TestEffectiveDay:
    def test_neither_due_nor_deadline(self) -> None:
        assert effective_day(None, None, _KOLKATA) is None

    def test_due_only(self) -> None:
        assert effective_day(_due(date(2026, 10, 10)), None, _KOLKATA) == date(
            2026, 10, 10
        )

    def test_deadline_only(self) -> None:
        assert effective_day(None, _deadline(date(2026, 10, 10)), _KOLKATA) == date(
            2026, 10, 10
        )

    def test_due_naive_datetime_kept_in_configured_zone(self) -> None:
        due = Due.from_dict({"date": "2026-10-10T23:30:00", "string": "11:30pm"})
        assert effective_day(due, None, _KOLKATA) == date(2026, 10, 10)

    def test_deadline_naive_datetime_kept_in_configured_zone(self) -> None:
        deadline = Deadline.from_dict({"date": "2026-10-10T23:30:00"})
        assert effective_day(None, deadline, _KOLKATA) == date(2026, 10, 10)

    def test_due_aware_datetime_converted_to_configured_zone(self) -> None:
        # 18:30Z = 00:00 IST next day — the instant's IST day wins.
        due = Due.from_dict({"date": "2026-10-10T18:30:00Z", "string": "x"})
        assert effective_day(due, None, _KOLKATA) == date(2026, 10, 11)

    def test_due_aware_datetime_same_day_when_not_past_midnight(self) -> None:
        # 18:00Z = 23:30 IST — still the same calendar day.
        due = Due.from_dict({"date": "2026-10-10T18:00:00Z", "string": "x"})
        assert effective_day(due, None, _KOLKATA) == date(2026, 10, 10)

    def test_due_aware_datetime_converted_to_utc_zone(self) -> None:
        # Same instant, UTC configured: stays on the 10th.
        due = Due.from_dict({"date": "2026-10-10T18:30:00Z", "string": "x"})
        assert effective_day(due, None, _UTC) == date(2026, 10, 10)

    def test_deadline_aware_datetime_converted_to_configured_zone(self) -> None:
        deadline = Deadline.from_dict({"date": "2026-10-10T18:30:00Z"})
        assert effective_day(None, deadline, _KOLKATA) == date(2026, 10, 11)

    def test_due_earlier_than_deadline(self) -> None:
        assert effective_day(
            _due(date(2026, 10, 9)), _deadline(date(2026, 10, 10)), _KOLKATA
        ) == date(2026, 10, 9)

    def test_deadline_earlier_than_due(self) -> None:
        assert effective_day(
            _due(date(2026, 10, 10)), _deadline(date(2026, 10, 9)), _KOLKATA
        ) == date(2026, 10, 9)

    def test_equal_days(self) -> None:
        assert effective_day(
            _due(date(2026, 10, 10)), _deadline(date(2026, 10, 10)), _KOLKATA
        ) == date(2026, 10, 10)
