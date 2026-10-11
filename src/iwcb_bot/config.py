from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone, tzinfo
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
    # Wall-clock time of the daily morning list in resolved_timezone;
    # None disables the morning announcement (MORNING_ANNOUNCE_TIME=off).
    morning_announce_time: time | None
    # Todoist assignee name → Telegram mention target (@handle or
    # numeric user id), from ASSIGNEE_MENTIONS.
    assignee_mentions: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Config:
        env = os.environ if environ is None else environ
        return cls(
            telegram_token=_require(env, "TELEGRAM_BOT_TOKEN"),
            todoist_token=_require(env, "TODOIST_API_TOKEN"),
            project_id=_require(env, "TODOIST_PROJECT_ID"),
            allowed_chat_ids=_parse_chat_ids(_require(env, "ALLOWED_CHAT_IDS")),
            timezone=_parse_timezone(env),
            morning_announce_time=_parse_morning_time(env),
            assignee_mentions=_parse_assignee_mentions(env),
        )

    def today(self) -> date:
        return datetime.now(tz=self.resolved_timezone).date()

    @property
    def resolved_timezone(self) -> tzinfo:
        """The timezone all day-boundary math uses.

        TIMEZONE when set; the server's local zone otherwise — same
        fallback as today().
        """
        if self.timezone is not None:
            return self.timezone
        return datetime.now().astimezone().tzinfo or timezone.utc


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


def _parse_morning_time(env: Mapping[str, str]) -> time | None:
    """MORNING_ANNOUNCE_TIME: HH:MM wall clock, or 'off' to disable.

    Unset falls back to the default 09:00 in the resolved timezone.
    """
    raw = env.get("MORNING_ANNOUNCE_TIME", "").strip()
    if not raw:
        return time(9, 0)
    if raw.casefold() == "off":
        return None
    hour_text, separator, minute_text = raw.partition(":")
    try:
        if not separator:
            raise ValueError(raw)
        return time(int(hour_text), int(minute_text))
    except ValueError as exc:
        raise ConfigError(
            f"MORNING_ANNOUNCE_TIME is not an HH:MM time or 'off': {raw!r} "
            "(expected e.g. 09:00)"
        ) from exc


def _parse_assignee_mentions(env: Mapping[str, str]) -> dict[str, str]:
    """ASSIGNEE_MENTIONS: ';'-separated 'Todoist Name=@handle' pairs.

    The target is a Telegram @username or a numeric user id.  Names are
    matched case-insensitively against Todoist collaborator names at
    render time.  Duplicate names: last pair wins.
    """
    raw = env.get("ASSIGNEE_MENTIONS", "").strip()
    if not raw:
        return {}
    mentions: dict[str, str] = {}
    for part in raw.split(";"):
        pair = part.strip()
        if not pair:
            continue
        name, separator, target = pair.partition("=")
        name = name.strip()
        target = target.strip()
        is_handle = (
            target.startswith("@")
            and len(target) > 1
            and all(c.isalnum() or c == "_" for c in target[1:])
        )
        if not separator or not name:
            raise ConfigError(
                f"ASSIGNEE_MENTIONS entry is not 'Name=target': {pair!r} "
                "(expected e.g. 'Abhinav Nair=@abnv')"
            )
        if not is_handle and not target.isdigit():
            raise ConfigError(
                f"ASSIGNEE_MENTIONS target is not an @handle or numeric id: "
                f"{target!r} (in entry {pair!r})"
            )
        mentions[name] = target
    return mentions


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
