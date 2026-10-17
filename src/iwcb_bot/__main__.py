from __future__ import annotations

import asyncio
import logging
import sys

from telegram.error import InvalidToken, TelegramError

from .bot import build_application
from .config import Config, ConfigError
from .todoist_client import HttpTodoistClient, TodoistError


def _redact(message: str, secrets: tuple[str, ...]) -> str:
    """Replace each configured secret in message with [redacted]."""
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[redacted]")
    return message


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
        record.msg = _redact(message, self._secrets)
        record.args = ()
        if record.exc_info and not record.exc_text:
            try:
                exc_text = logging.Formatter().formatException(record.exc_info)
            except Exception:  # noqa: BLE001 - never block logging
                exc_text = None
            if exc_text:
                record.exc_text = _redact(exc_text, self._secrets)
        return True


def main() -> None:
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    logging.basicConfig(
        level=getattr(logging, cfg.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Pinned even under LOG_LEVEL=DEBUG: both libraries log full
    # payloads at DEBUG — httpx (and its httpcore transport layer)
    # embed Bot-API URLs and headers (the token is redacted by the
    # filter below, but it's still far too noisy), and
    # python-telegram-bot's "Processing update …" / "Calling Bot API
    # endpoint …" lines dump incoming message text, usernames and
    # outgoing task content, which the documented DEBUG contract (ids
    # and action names only) must never emit.  The bot's own DEBUG
    # lines already cover what matters: durations, outcomes and ids.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # urllib3 backs the Todoist SDK's sync requests session.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)

    # Handler filters run for records propagated from child loggers
    # (e.g. PTB's bootstrap networkloop, which logs the token on a
    # rejected login); a logger filter would miss those entirely.
    # The same secrets tuple also scrubs the post-config stderr
    # prints below: PTB can embed raw response bodies — which a
    # middlebox may fill with the bot-API URL, token included — in
    # TelegramError messages, and print() bypasses logging handlers.
    secrets = (cfg.telegram_token, cfg.todoist_token)
    secret_filter = _SecretRedactionFilter(secrets)
    for handler in logging.getLogger().handlers:
        handler.addFilter(secret_filter)

    todoist = HttpTodoistClient(cfg.todoist_token, lambda: cfg.resolved_timezone)
    try:
        project = asyncio.run(todoist.resolve_project(cfg.project_id))
    except ConfigError as exc:
        asyncio.run(todoist.close())
        print(_redact(f"Configuration error: {exc}", secrets), file=sys.stderr)
        raise SystemExit(2) from exc
    except TodoistError as exc:
        asyncio.run(todoist.close())
        print(_redact(f"Startup error: {exc}", secrets), file=sys.stderr)
        raise SystemExit(1) from exc

    application = build_application(cfg, project, todoist)
    logger = logging.getLogger(__name__)
    # Project id only — the name is content, not a correlation key.
    logger.info("Polling project %s", project.id)
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
        print(_redact(f"Telegram error: {exc}", secrets), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
