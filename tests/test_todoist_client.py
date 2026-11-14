from __future__ import annotations

import asyncio
from datetime import date, timezone
from zoneinfo import ZoneInfo

from todoist_api_python.models import Deadline, Due

from todoist_bot.todoist_client import (
    HttpTodoistClient,
    _project_token,
    effective_day,
)

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


class _CollaboratorStub(HttpTodoistClient):
    """HttpTodoistClient with only the collaborator-cache machinery.

    Each refresh pops the next canned result: a mapping replaces the
    cache, None simulates an API failure (old cache kept).
    """

    def __init__(self, fetches: list[dict[str, str] | None]) -> None:
        self.calls = 0
        self._fetches = list(fetches)
        self._collaborators = None
        self._collaborator_misses = set()
        self._collaborator_lock = asyncio.Lock()

    async def _refresh_collaborators(self, project_id: str) -> bool:
        self.calls += 1
        result = self._fetches.pop(0) if self._fetches else None
        if result is None:
            return False
        self._collaborators = result
        return True


class TestCollaboratorNames:
    def test_failed_initial_fetch_is_one_attempt_for_the_whole_batch(self) -> None:
        client = _CollaboratorStub([None])
        names = asyncio.run(client.collaborator_names("project", ["a", "b", "a", "c"]))
        assert names == {"a": None, "b": None, "c": None}
        assert client.calls == 1

    def test_successful_fetch_resolves_and_dedupes_ids(self) -> None:
        client = _CollaboratorStub(
            [{"a": "Alice", "b": "Bob"}, {"a": "Alice", "b": "Bob"}]
        )
        names = asyncio.run(
            client.collaborator_names("project", ["a", "b", "a", "missing"])
        )
        assert names == {"a": "Alice", "b": "Bob", "missing": None}
        # one initial fetch; the unknown id triggers exactly one refresh
        assert client.calls == 2
        assert "missing" in client._collaborator_misses

    def test_failed_unknown_id_refresh_is_retried_next_time(self) -> None:
        client = _CollaboratorStub([{"a": "Alice"}, None, {"b": "Bob"}])
        assert asyncio.run(client.collaborator_names("project", ["a", "b"])) == {
            "a": "Alice",
            "b": None,
        }
        assert "b" not in client._collaborator_misses
        assert asyncio.run(client.collaborator_names("project", ["b"])) == {"b": "Bob"}
        assert "b" not in client._collaborator_misses


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
