from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from todoist_api_python.models import Deadline, Due

from iwcb_bot.bot import (
    _assignee_suffix,
    _BODY_LIMIT,
    _chunk,
    _fmt_due,
    _ListItem,
    _md_html,
    _mentions_lookup,
    _NBSP,
    _next_announce,
    _one_line,
    _render_list,
    _render_text,
    _resolve_ids,
    _task_suffixes,
)
from iwcb_bot.config import Config
from iwcb_bot.todoist_client import _project_token, effective_day

TODAY = date(2026, 10, 9)


class TestTaskSuffixes:
    def test_empty(self) -> None:
        assert _task_suffixes([]) == []

    def test_single_task_uses_minimum_length(self) -> None:
        assert _task_suffixes(["abcdef"]) == ["ef"]

    def test_disjoint_ids(self) -> None:
        assert _task_suffixes(["abc", "xyz"]) == ["bc", "yz"]

    def test_colliding_tails_grow(self) -> None:
        assert _task_suffixes(["abc", "abcd"]) == ["bc", "cd"]

    def test_duplicate_ids_return_full_ids(self) -> None:
        assert _task_suffixes(["abc", "abc"]) == ["abc", "abc"]

    def test_case_variant_tails_push_past_case(self) -> None:
        # Labels must never differ only by case: these grow until a
        # genuinely different character separates the tails.
        assert _task_suffixes(["6QcJ", "6PcJ"]) == ["qcj", "pcj"]

    def test_output_is_lowercase_true_tail(self) -> None:
        ids = ["6AbCd", "6xYzF", "6aBcD2"]
        for task_id, label in zip(ids, _task_suffixes(ids), strict=True):
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
        assert _task_suffixes(ids) == ["j5", "gj", "5c", "cj"]


class TestResolveIds:
    def test_exact_match_wins_over_tail_collision(self) -> None:
        assert _resolve_ids(["abc", "xabc"], "abc") == ["abc"]

    def test_exact_match_is_case_insensitive(self) -> None:
        assert _resolve_ids(["AbC", "xyz"], "abc") == ["AbC"]

    def test_tail_fallback(self) -> None:
        assert _resolve_ids(["abcdef", "xyz"], "def") == ["abcdef"]

    def test_tail_fallback_is_case_insensitive(self) -> None:
        query = "ZX"  # as typed by the user; handlers casefold before resolving
        assert _resolve_ids(["6zX", "6ax"], query.casefold()) == ["6zX"]

    def test_tail_matches_multiple(self) -> None:
        assert _resolve_ids(["abc1", "ybc1"], "bc1") == ["abc1", "ybc1"]

    def test_no_match(self) -> None:
        assert _resolve_ids(["abc"], "zz") == []

    def test_exact_match_returned_before_any_tail_matches(self) -> None:
        # "bc" is a tail of both, but an exact id "bc" exists.
        assert _resolve_ids(["abc", "xbc", "bc"], "bc") == ["bc"]


class TestOneLine:
    def test_collapses_whitespace(self) -> None:
        assert _one_line("a\nb\t c") == "a b c"

    def test_truncates_to_limit(self) -> None:
        out = _one_line("x" * 600)
        assert len(out) == 500
        assert out.endswith("…")

    def test_short_text_untouched(self) -> None:
        assert _one_line("hello") == "hello"


class TestChunk:
    def test_short_text_single_chunk(self) -> None:
        assert _chunk("a\nb") == ["a\nb"]

    def test_splits_on_line_boundaries(self) -> None:
        text = "\n".join("line" + "x" * 100 for _ in range(50))
        chunks = _chunk(text)
        assert len(chunks) > 1
        assert all(len(c) <= 4000 for c in chunks)
        # Chunks reassemble to the original (minus separator handling).
        assert "\n".join(chunks).replace("\n", "") == text.replace("\n", "")

    def test_oversized_line_fails_loudly(self) -> None:
        with pytest.raises(AssertionError):
            _chunk("H" * 5000)


class TestMdHtml:
    def test_link_renders(self) -> None:
        out = _md_html("on [events.indieweb.org](https://events.indieweb.org)")
        assert out == (
            'on <a href="https://events.indieweb.org">events.indieweb.org</a>'
        )

    def test_javascript_scheme_blocked(self) -> None:
        out = _md_html("[x](javascript:alert(1))")
        assert "<a" not in out

    def test_mailto_scheme_blocked(self) -> None:
        out = _md_html("[x](mailto:a@b.c)")
        assert "<a" not in out

    def test_relative_link_blocked(self) -> None:
        out = _md_html("[x](/rel/path)")
        assert "<a" not in out

    def test_data_image_blocked(self) -> None:
        out = _md_html("[x](data:image/png;base64,AAAA)")
        assert "<a" not in out

    def test_bare_url_linkified(self) -> None:
        out = _md_html("see https://example.com/x")
        assert '<a href="https://example.com/x">' in out

    def test_raw_html_escaped(self) -> None:
        out = _md_html("a <b>x</b> <script>y</script>")
        assert "<b>" not in out and "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_image_disabled(self) -> None:
        out = _md_html("![alt](https://x.y/z.png)")
        assert "<img" not in out

    def test_inline_formatting(self) -> None:
        out = _md_html("**b** *i* ~~s~~ `c`")
        assert "<strong>b</strong>" in out
        assert "<em>i</em>" in out
        assert "<s>s</s>" in out
        assert "<code>c</code>" in out

    def test_unbalanced_brackets_left_alone(self) -> None:
        assert _md_html("Buy milk (2L) [unbalanced") == "Buy milk (2L) [unbalanced"


class TestRenderText:
    def test_plain_text_unchanged(self) -> None:
        assert _render_text("buy milk") == "buy milk"

    def test_empty_and_whitespace_only(self) -> None:
        assert _render_text("") == ""
        assert _render_text(" \n\t ") == ""

    def test_collapses_to_one_line(self) -> None:
        out = _render_text("line1\nline2  \tline3")
        assert "\n" not in out
        assert out == "line1 line2 line3"

    def test_hard_break_does_not_become_br(self) -> None:
        # Markdown hard break (two trailing spaces) must not emit <br>,
        # which Telegram rejects.
        out = _render_text("line1  \nline2")
        assert "<br" not in out
        assert "line1 line2" in out

    def test_markdown_link_renders(self) -> None:
        out = _render_text("[x](https://e.x)")
        assert out == '<a href="https://e.x">x</a>'

    def test_inline_formatting(self) -> None:
        out = _render_text("**b** *i* ~~s~~ `c`")
        assert "<strong>b</strong>" in out
        assert "<em>i</em>" in out
        assert "<s>s</s>" in out
        assert "<code>c</code>" in out

    def test_javascript_scheme_blocked(self) -> None:
        assert "<a" not in _render_text("[x](javascript:alert(1))")

    def test_raw_html_escaped(self) -> None:
        out = _render_text("<img src=x onerror=alert(1)>")
        assert "<img" not in out
        assert "&lt;img" in out

    def test_long_text_truncated_to_limit(self) -> None:
        out = _render_text("x" * 600)
        assert len(out) == 500
        assert out.endswith("…")

    def test_emoji_url_inflation_falls_back_to_plain(self) -> None:
        # Percent-encoding of the emoji URL blows the HTML render past
        # _BODY_LIMIT; the fallback keeps the message sendable and the
        # raw content readable (nothing here needs escaping).
        raw = "[](https://e.com/" + "🎉" * 482 + ")"
        assert len(_md_html(raw)) > _BODY_LIMIT
        out = _render_text(raw)
        assert len(out) <= _BODY_LIMIT
        assert out == raw

    def test_fallback_escapes_html(self) -> None:
        raw = "<script>hi</script> [](https://e.com/" + "🎉" * 460 + ")"
        assert len(_md_html(raw)) > _BODY_LIMIT
        out = _render_text(raw)
        assert len(out) <= _BODY_LIMIT
        assert "<script>" not in out
        assert "&lt;script&gt;" in out


class TestRenderList:
    def test_link_and_code_id(self) -> None:
        items = [
            _ListItem(
                task_id="6cabc",
                text="Post on [events.indieweb.org](https://events.indieweb.org)",
                due=TODAY,
            )
        ]
        out = _render_list(items, TODAY, show_dates=False)
        assert '<a href="https://events.indieweb.org">' in out
        assert out.startswith("<code>[")
        assert out.endswith(
            '</code>  Post on <a href="https://events.indieweb.org">events.indieweb.org</a>'
        )

    def test_multiline_text_stays_on_one_line(self) -> None:
        items = [_ListItem(task_id="aa", text="a\nb", due=TODAY)]
        out = _render_list(items, TODAY, show_dates=False)
        assert "\n" not in out
        assert "a b" in out

    def test_ids_bracketed_in_code_run(self) -> None:
        items = [
            _ListItem(task_id="zab", text="one", due=TODAY),
            _ListItem(task_id="ab", text="two", due=TODAY),
        ]
        out = _render_list(items, TODAY, show_dates=False)
        assert "<code>[zab]</code>" in out
        # narrower id is padded so both code runs share one width
        assert f"<code>[ab{_NBSP}]</code>" in out

    def test_long_text_truncated(self) -> None:
        items = [_ListItem(task_id="aa", text="x" * 600, due=TODAY)]
        out = _render_list(items, TODAY, show_dates=False)
        assert "…" in out
        assert "x" * 501 not in out

    def test_overdue_active_gets_marker_and_due_suffix(self) -> None:
        items = [_ListItem(task_id="aa", text="late", due=date(2026, 6, 9))]
        out = _render_list(items, TODAY, show_dates=False)
        assert "⚠ " in out
        assert "— due Tue 9 Jun" in out

    def test_overdue_done_has_no_due_suffix(self) -> None:
        items = [_ListItem(task_id="aa", text="late", due=date(2026, 6, 9), done=True)]
        out = _render_list(items, TODAY, show_dates=False)
        assert "<s>late</s>" in out
        assert "— due" not in out

    def test_today_item_has_no_due_suffix(self) -> None:
        items = [_ListItem(task_id="aa", text="task", due=TODAY)]
        out = _render_list(items, TODAY, show_dates=False)
        assert "— due" not in out

    def test_done_item_wrapped_in_strike(self) -> None:
        items = [_ListItem(task_id="aa", text="task", due=TODAY, done=True)]
        out = _render_list(items, TODAY, show_dates=False)
        assert "<s>task</s>" in out

    def test_show_dates_dated_all_lines(self) -> None:
        items = [
            _ListItem(task_id="aa", text="today", due=TODAY),
            _ListItem(task_id="ab", text="later", due=date(2026, 10, 12)),
            _ListItem(task_id="ac", text="done", due=date(2026, 10, 11), done=True),
        ]
        out = _render_list(items, TODAY, show_dates=True)
        lines = out.splitlines()
        assert "— due Fri 9 Oct" in lines[0]
        assert "— due Mon 12 Oct" in lines[1]
        assert "— due Sun 11 Oct" in lines[2]
        assert "<s>done</s>" in lines[2]

    def test_show_dates_overdue_keeps_marker(self) -> None:
        items = [_ListItem(task_id="aa", text="late", due=date(2026, 6, 9))]
        out = _render_list(items, TODAY, show_dates=True)
        assert "⚠ " in out
        assert "— due Tue 9 Jun" in out

    def test_every_rendered_line_is_balanced_html(self) -> None:
        items = [
            _ListItem(task_id="aa", text="line1\nline2 [x](https://e.x)", due=TODAY),
            _ListItem(task_id="ab", text="done", due=TODAY, done=True),
        ]
        out = _render_list(items, TODAY, show_dates=False)
        for line in out.splitlines():
            assert line.count("<s>") == line.count("</s>")
            assert line.count("<a ") == line.count("</a>")
            assert line.count("<code>") == line.count("</code>")


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


_KOLKATA = ZoneInfo("Asia/Kolkata")  # UTC+5:30
_UTC = timezone.utc


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


def _config(**overrides: object) -> Config:
    values: dict[str, object] = {
        "telegram_token": "t" * 40,
        "todoist_token": "0" * 36,
        "project_id": "2200000000",
        "allowed_chat_ids": frozenset({1}),
        "timezone": None,
        "morning_announce_time": time(9, 0),
    }
    values.update(overrides)
    return Config(**values)  # type: ignore[arg-type]


class TestResolvedTimezone:
    def test_configured_timezone_preferred(self) -> None:
        cfg = _config(timezone=_KOLKATA)
        assert cfg.resolved_timezone is _KOLKATA

    def test_unset_falls_back_to_server_local(self) -> None:
        cfg = _config()
        expected = datetime.now().astimezone().tzinfo
        assert cfg.resolved_timezone == expected


class TestFmtDue:
    _REF = date(2026, 10, 9)

    def test_same_year_omits_year(self) -> None:
        assert _fmt_due(date(2026, 6, 9), self._REF) == "Tue 9 Jun"

    def test_same_day(self) -> None:
        assert _fmt_due(self._REF, self._REF) == "Fri 9 Oct"

    def test_earlier_year_includes_year(self) -> None:
        assert _fmt_due(date(2025, 6, 9), self._REF) == "Mon 9 Jun 25"

    def test_later_year_includes_year(self) -> None:
        assert _fmt_due(date(2027, 1, 2), self._REF) == "Sat 2 Jan 27"

    def test_year_boundary_reference(self) -> None:
        # /week on 30 Dec reaches into January of the next year.
        assert _fmt_due(date(2027, 1, 2), date(2026, 12, 30)) == "Sat 2 Jan 27"
        assert _fmt_due(date(2026, 12, 31), date(2026, 12, 30)) == "Thu 31 Dec"


class TestNextAnnounce:
    _AT = time(9, 0)

    def test_before_announce_time_same_day(self) -> None:
        now = datetime(2026, 10, 10, 8, 0, tzinfo=_KOLKATA)
        target = _next_announce(self._AT, _KOLKATA, now)
        assert target == datetime(2026, 10, 10, 9, 0, tzinfo=_KOLKATA)

    def test_after_announce_time_rolls_to_tomorrow(self) -> None:
        now = datetime(2026, 10, 10, 10, 0, tzinfo=_KOLKATA)
        target = _next_announce(self._AT, _KOLKATA, now)
        assert target == datetime(2026, 10, 11, 9, 0, tzinfo=_KOLKATA)

    def test_exactly_at_announce_time_rolls_to_tomorrow(self) -> None:
        now = datetime(2026, 10, 10, 9, 0, tzinfo=_KOLKATA)
        target = _next_announce(self._AT, _KOLKATA, now)
        assert target == datetime(2026, 10, 11, 9, 0, tzinfo=_KOLKATA)

    def test_just_before_announce_time(self) -> None:
        now = datetime(2026, 10, 10, 8, 59, 59, tzinfo=_KOLKATA)
        target = _next_announce(self._AT, _KOLKATA, now)
        assert target == datetime(2026, 10, 10, 9, 0, tzinfo=_KOLKATA)

    def test_just_after_midnight(self) -> None:
        now = datetime(2026, 10, 10, 0, 0, 1, tzinfo=_KOLKATA)
        target = _next_announce(self._AT, _KOLKATA, now)
        assert target == datetime(2026, 10, 10, 9, 0, tzinfo=_KOLKATA)

    def test_utc_instant_converted_to_configured_zone(self) -> None:
        # 2026-10-10 02:30Z = 08:00 IST → target 09:00 IST.
        now = datetime(2026, 10, 10, 2, 30, tzinfo=timezone.utc)
        target = _next_announce(self._AT, _KOLKATA, now)
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
        target = _next_announce(self._AT, new_york, now)
        assert target == datetime(2026, 3, 8, 9, 0, tzinfo=new_york)
        assert target.utcoffset() == timedelta(hours=-4)
        assert target.timestamp() - now.timestamp() == 12 * 3600.0


class TestMentionsLookup:
    def test_casefolds_names(self) -> None:
        cfg = _config(assignee_mentions={"Abhinav Nair": "@abnv"})
        assert _mentions_lookup(cfg) == {"abhinav nair": "@abnv"}

    def test_empty_by_default(self) -> None:
        assert _mentions_lookup(_config()) == {}


class TestAssigneeSuffix:
    _MENTIONS = {"abhinav nair": "@abnv", "priya singh": "123456789"}

    def test_unassigned_renders_nothing(self) -> None:
        assert _assignee_suffix(None, self._MENTIONS) == ""

    def test_mapped_handle_renders_handle(self) -> None:
        assert _assignee_suffix("Abhinav Nair", self._MENTIONS) == " — @abnv"

    def test_mapped_id_renders_tg_mention(self) -> None:
        assert (
            _assignee_suffix("Priya Singh", self._MENTIONS)
            == ' — <a href="tg://user?id=123456789">Priya Singh</a>'
        )

    def test_unmapped_name_renders_escaped_text(self) -> None:
        assert _assignee_suffix("A&B C", self._MENTIONS) == " — A&amp;B C"

    def test_lookup_is_case_insensitive(self) -> None:
        assert _assignee_suffix("ABHINAV NAIR", self._MENTIONS) == " — @abnv"

    def test_name_html_escaped_in_tg_mention(self) -> None:
        mentions = {"a&b": "1"}
        assert (
            _assignee_suffix("A&B", mentions)
            == ' — <a href="tg://user?id=1">A&amp;B</a>'
        )


class TestRenderListAssignee:
    _ITEM = _ListItem(
        task_id="62ab",
        text="buy milk",
        due=date(2026, 10, 9),
        assignee="Abhinav Nair",
    )

    def test_assignee_between_text_and_due(self) -> None:
        rendered = _render_list(
            [self._ITEM],
            date(2026, 10, 9),
            show_dates=True,
            mentions={"abhinav nair": "@abnv"},
        )
        assert rendered == "<code>[ab]</code>  buy milk — @abnv — due Fri 9 Oct"

    def test_no_mentions_map_shows_plain_name(self) -> None:
        rendered = _render_list([self._ITEM], date(2026, 10, 9), show_dates=False)
        assert "buy milk — Abhinav Nair" in rendered

    def test_unassigned_has_no_suffix(self) -> None:
        item = _ListItem(task_id="62ab", text="buy milk", due=date(2026, 10, 9))
        rendered = _render_list(
            [item], date(2026, 10, 9), show_dates=False, mentions={"x": "@y"}
        )
        assert rendered == "<code>[ab]</code>  buy milk"
