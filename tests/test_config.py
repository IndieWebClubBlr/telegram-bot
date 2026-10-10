from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from iwcb_bot.config import Config, ConfigError

BASE_ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:secret-tg-token",
    "TODOIST_API_TOKEN": "0123456789abcdef-secret-todoist",
    "TODOIST_PROJECT_ID": "6X4Vw2Hfmg73Q2XR",
    "ALLOWED_CHAT_IDS": "123456789",
}


def make_config(**over: str) -> Config:
    return Config.from_env({**BASE_ENV, **over})


class TestFromEnv:
    def test_valid_config(self) -> None:
        cfg = make_config()
        assert cfg.project_id == "6X4Vw2Hfmg73Q2XR"
        assert cfg.allowed_chat_ids == frozenset({123456789})
        assert cfg.timezone is None

    @pytest.mark.parametrize("name", sorted(BASE_ENV))
    def test_missing_required_var_names_it(self, name: str) -> None:
        env = {k: v for k, v in BASE_ENV.items() if k != name}
        with pytest.raises(ConfigError, match=name):
            Config.from_env(env)

    def test_empty_value_rejected(self) -> None:
        with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
            make_config(TELEGRAM_BOT_TOKEN="  ")

    def test_bad_project_id_not_checked_at_parse_time(self) -> None:
        # Project existence is validated against the API at startup.
        assert make_config(TODOIST_PROJECT_ID="nope").project_id == "nope"


class TestChatIds:
    def test_whitespace_and_negatives(self) -> None:
        cfg = make_config(ALLOWED_CHAT_IDS=" 1 , -100123 ")
        assert cfg.allowed_chat_ids == frozenset({1, -100123})

    def test_empty_entry_rejected(self) -> None:
        with pytest.raises(ConfigError, match="empty entry"):
            make_config(ALLOWED_CHAT_IDS="1,,2")

    def test_non_integer_rejected(self) -> None:
        with pytest.raises(ConfigError, match="not a chat id"):
            make_config(ALLOWED_CHAT_IDS="abc")

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ConfigError):
            make_config(ALLOWED_CHAT_IDS="")


class TestTimezone:
    def test_valid_iana_name(self) -> None:
        cfg = make_config(TIMEZONE="Asia/Kolkata")
        assert cfg.timezone == ZoneInfo("Asia/Kolkata")

    def test_invalid_name_fails_fast(self) -> None:
        with pytest.raises(ConfigError, match="TIMEZONE"):
            make_config(TIMEZONE="Mars/Olympus_Mons")

    def test_unset_falls_back_to_none(self) -> None:
        assert make_config().timezone is None

    def test_today_returns_today_in_configured_zone(self) -> None:
        cfg = make_config(TIMEZONE="Asia/Kolkata")
        expected = datetime.now(tz=ZoneInfo("Asia/Kolkata")).date()
        assert cfg.today() == expected


class TestMorningAnnounceTime:
    def test_unset_defaults_to_9am(self) -> None:
        assert make_config().morning_announce_time == time(9, 0)

    def test_empty_defaults_to_9am(self) -> None:
        assert make_config(MORNING_ANNOUNCE_TIME="  ").morning_announce_time == time(
            9, 0
        )

    def test_hh_mm_parsed(self) -> None:
        cfg = make_config(MORNING_ANNOUNCE_TIME="08:30")
        assert cfg.morning_announce_time == time(8, 30)

    def test_unpadded_parsed(self) -> None:
        cfg = make_config(MORNING_ANNOUNCE_TIME="9:05")
        assert cfg.morning_announce_time == time(9, 5)

    def test_off_disables(self) -> None:
        assert make_config(MORNING_ANNOUNCE_TIME="off").morning_announce_time is None

    def test_off_case_insensitive(self) -> None:
        assert make_config(MORNING_ANNOUNCE_TIME="OFF").morning_announce_time is None

    @pytest.mark.parametrize("raw", ["25:00", "9am", "09-00", "12:61", ":30", "9:"])
    def test_invalid_rejected(self, raw: str) -> None:
        with pytest.raises(ConfigError, match="MORNING_ANNOUNCE_TIME"):
            make_config(MORNING_ANNOUNCE_TIME=raw)


class TestSecrets:
    def test_repr_masks_tokens(self) -> None:
        cfg = make_config()
        text = repr(cfg)
        assert "secret-tg-token" not in text
        assert "secret-todoist" not in text
        assert "telegram_token" not in text
        assert "todoist_token" not in text
