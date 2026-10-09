from __future__ import annotations

import asyncio
import logging
import sys

from telegram.error import InvalidToken, TelegramError

from .bot import build_application
from .config import Config, ConfigError
from .todoist_client import TodoistClient, TodoistError


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

    todoist = TodoistClient(cfg.todoist_token)
    try:
        project = asyncio.run(todoist.resolve_project(cfg.project_id))
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except TodoistError as exc:
        print(f"Startup error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    application = build_application(cfg, project, todoist)
    logger = logging.getLogger(__name__)
    logger.info("Polling for project %s (%s)", project.name, project.id)
    try:
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
    finally:
        asyncio.run(todoist.close())


if __name__ == "__main__":
    main()
