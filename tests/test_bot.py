from __future__ import annotations

from datetime import date

import pytest

from iwcb_bot.bot import (
    _BODY_LIMIT,
    _chunk,
    _ListItem,
    _md_html,
    _one_line,
    _render_text,
    _render_today,
    _resolve_ids,
    _task_suffixes,
)

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


class TestRenderToday:
    def test_header_and_link(self) -> None:
        items = [
            _ListItem(
                task_id="6cabc",
                text="Post on [events.indieweb.org](https://events.indieweb.org)",
                due=TODAY,
            )
        ]
        out = _render_today("IndieWebClub", items, TODAY)
        assert out.startswith("<b>Due in IndieWebClub:</b>")
        assert '<a href="https://events.indieweb.org">' in out
        assert out.splitlines()[1].startswith("<code>")

    def test_multiline_text_stays_on_one_line(self) -> None:
        items = [_ListItem(task_id="aa", text="a\nb", due=TODAY)]
        out = _render_today("P", items, TODAY)
        body = out.splitlines()[1]
        assert "\n" not in body
        assert "a b" in body

    def test_long_text_truncated(self) -> None:
        items = [_ListItem(task_id="aa", text="x" * 600, due=TODAY)]
        out = _render_today("P", items, TODAY)
        assert "…" in out
        assert "x" * 501 not in out

    def test_overdue_active_gets_marker_and_due_suffix(self) -> None:
        items = [_ListItem(task_id="aa", text="late", due=date(2026, 6, 9))]
        out = _render_today("P", items, TODAY)
        body = out.splitlines()[1]
        assert "⚠ " in body
        assert "— due Tue 9 Jun" in body

    def test_overdue_done_has_no_due_suffix(self) -> None:
        items = [_ListItem(task_id="aa", text="late", due=date(2026, 6, 9), done=True)]
        out = _render_today("P", items, TODAY)
        body = out.splitlines()[1]
        assert "<s>late</s>" in body
        assert "— due" not in body

    def test_done_item_wrapped_in_strike(self) -> None:
        items = [_ListItem(task_id="aa", text="task", due=TODAY, done=True)]
        out = _render_today("P", items, TODAY)
        assert "<s>task</s>" in out

    def test_project_name_newline_collapsed(self) -> None:
        items = [_ListItem(task_id="aa", text="t", due=TODAY)]
        out = _render_today("Weird\nName", items, TODAY)
        assert out.startswith("<b>Due in Weird Name:</b>")

    def test_every_rendered_line_is_balanced_html(self) -> None:
        items = [
            _ListItem(task_id="aa", text="line1\nline2 [x](https://e.x)", due=TODAY),
            _ListItem(task_id="ab", text="done", due=TODAY, done=True),
        ]
        out = _render_today("P", items, TODAY)
        for line in out.splitlines():
            assert line.count("<s>") == line.count("</s>")
            assert line.count("<a ") == line.count("</a>")
            assert line.count("<code>") == line.count("</code>")
