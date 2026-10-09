from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(Exception):
    """Missing or invalid configuration."""


@dataclass(frozen=True)
class Config:
    telegram_token: str = field(repr=False)
    todoist_token: str = field(repr=False)
    project_id: str
    allowed_chat_ids: frozenset[int]
    timezone: tzinfo | None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Config:
        env = os.environ if environ is None else environ
        return cls(
            telegram_token=_require(env, "TELEGRAM_BOT_TOKEN"),
            todoist_token=_require(env, "TODOIST_API_TOKEN"),
            project_id=_require(env, "TODOIST_PROJECT_ID"),
            allowed_chat_ids=_parse_chat_ids(_require(env, "ALLOWED_CHAT_IDS")),
            timezone=_parse_timezone(env),
        )

    def today(self) -> date:
        if self.timezone is None:
            return datetime.now().astimezone().date()
        return datetime.now(tz=self.timezone).date()


def _require(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set")
    return value


def _parse_timezone(env: Mapping[str, str]) -> tzinfo | None:
    raw = env.get("TIMEZONE", "").strip()
    if not raw:
        return None
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ConfigError(
            f"TIMEZONE is not a valid IANA time zone: {raw!r} "
            "(expected e.g. Asia/Kolkata)"
        ) from exc


def _parse_chat_ids(value: str) -> frozenset[int]:
    chat_ids: set[int] = set()
    for part in value.split(","):
        token = part.strip()
        if not token:
            raise ConfigError("ALLOWED_CHAT_IDS contains an empty entry")
        try:
            chat_ids.add(int(token))
        except ValueError as exc:
            raise ConfigError(
                f"ALLOWED_CHAT_IDS entry is not a chat id: {token!r}"
            ) from exc
    return frozenset(chat_ids)
