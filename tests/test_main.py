from __future__ import annotations

from iwcb_bot.__main__ import _redact

TOKEN = "123456:secret-tg-token"
TODOIST = "0123456789abcdef-secret-todoist"
SECRETS = (TOKEN, TODOIST)


class TestRedact:
    def test_replaces_each_secret(self) -> None:
        msg = f"error calling https://api.telegram.org/bot{TOKEN}/getMe"
        assert _redact(msg, SECRETS) == (
            "error calling https://api.telegram.org/bot[redacted]/getMe"
        )

    def test_multiple_secrets_in_one_message(self) -> None:
        msg = f"tg={TOKEN} todoist={TODOIST}"
        assert _redact(msg, SECRETS) == "tg=[redacted] todoist=[redacted]"

    def test_message_without_secret_unchanged(self) -> None:
        assert _redact("plain message", SECRETS) == "plain message"

    def test_empty_secrets_pass_through(self) -> None:
        msg = f"token={TOKEN}"
        assert _redact(msg, ()) == msg
        assert _redact(msg, ("",)) == msg

    def test_partial_secret_replaced_everywhere(self) -> None:
        msg = f"{TOKEN} then {TOKEN} again"
        assert _redact(msg, (TOKEN,)) == "[redacted] then [redacted] again"
