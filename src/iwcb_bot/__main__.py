from __future__ import annotations

import asyncio
import logging
import sys

from telegram.error import InvalidToken, TelegramError

from .bot import build_application
from .config import Config, ConfigError
from .todoist_client import TodoistClient, TodoistError


class _SecretRedactionFilter(logging.Filter):
    """Keep configured secrets out of every emitted log record.

    python-telegram-bot's InvalidToken handling logs full tracebacks at
    ERROR, and those messages embed the raw bot token; the formatter
    appends exception text separately from the record message, so both
    paths are rewritten here.
    """

    def __init__(self, secrets: tuple[str, ...]) -> None:
        super().__init__()
        self._secrets = tuple(secret for secret in secrets if secret)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never block logging
            return True
        for secret in self._secrets:
            message = message.replace(secret, "[redacted]")
        record.msg = message
        record.args = ()
        if record.exc_info and not record.exc_text:
            try:
                exc_text = logging.Formatter().formatException(record.exc_info)
            except Exception:  # noqa: BLE001 - never block logging
                exc_text = None
            if exc_text:
                for secret in self._secrets:
                    exc_text = exc_text.replace(secret, "[redacted]")
                record.exc_text = exc_text
        return True


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    logging.getLogger().addFilter(
        _SecretRedactionFilter((cfg.telegram_token, cfg.todoist_token))
    )

    todoist = TodoistClient(cfg.todoist_token, cfg.resolved_timezone)
    try:
        project = asyncio.run(todoist.resolve_project(cfg.project_id))
    except ConfigError as exc:
        asyncio.run(todoist.close())
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except TodoistError as exc:
        asyncio.run(todoist.close())
        print(f"Startup error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    application = build_application(cfg, project, todoist)
    logger = logging.getLogger(__name__)
    logger.info("Polling for project %s (%s)", project.name, project.id)
    try:
        # The Todoist client is closed by the application's post_shutdown
        # hook (bot.py) — inside the polling event loop, where the
        # aiohttp session was created.
        application.run_polling(drop_pending_updates=True)
    except KeyboardInterrupt:
        pass
    except InvalidToken:
        # PTB's InvalidToken message embeds the token verbatim — never
        # print it; a bad token is a configuration error (exit 2, the
        # systemd unit does not restart on it).
        print(
            "Telegram error: bot token rejected by Telegram "
            "(check TELEGRAM_BOT_TOKEN)",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    except TelegramError as exc:
        print(f"Telegram error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
