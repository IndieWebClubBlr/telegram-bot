# IWCB Telegram Bot

IWCB Telegram Bot — a Telegram bot for IWCB. Task management is backed by
Todoist: the bot operates on one configured project. Vanilla Nix packaging
plus a NixOS module (no flakes) — the module is imported by your machine's
NixOS configuration, which is also where secrets live (e.g. via sops-nix).

## Commands

All commands operate on the configured project only.

| Command | What it does |
| --- | --- |
| `/help` (alias `/start`) | Command list |
| `/add <text>` | Add a task using Todoist [quick-add syntax](https://developer.todoist.com/api/v1/#operation/quick_add_api_v1_tasks_quick_post): `/add buy milk tomorrow p1 @errand /Groceries +Abhinav`. Sections (`/Name`) and assignees (`+Name`) work because the configured project is auto-prepended to the quick-add text; a `#OtherProject` in the text is overridden — the configured project always wins. If the new task is due today or overdue, the reply also contains the refreshed task list — IDs shown by an earlier `/today` may have shifted |
| `/today` | Tasks in the project due today or overdue (⚠), sorted by due date. Each line starts with a short unique ID, shown in lowercase; overdue lines also end with `— due <date>` |
| `/done <id>` | Complete the task whose ID ends with `<id>` (as shown by /today; matching is case-insensitive). Resolved against a fresh fetch, so a task completed elsewhere gives a safe "no match" error. Strikes the line in place (lists that fit a single message) and confirms; for a recurring task the confirmation includes the next occurrence date |
| `/undo <id>` | Reopen a task completed with `/done`, un-striking it. Refuses recurring tasks — completing one rolls it to its next occurrence |
| `/edit <id> <new text>` | Edit the task whose ID ends with `<id>`, resolved against a fresh fetch like `/done`. The text is stored verbatim — quick-add parsing (dates, `p1`, `@labels`) happens only on creation (`/add`), so those tokens become part of the task name. Recurring: the new name applies to all occurrences. Max 500 characters |
| `/project` | Link to the configured project in Todoist |

Only chats listed in `ALLOWED_CHAT_IDS` are served; everyone else is ignored.
Any member of an allowed chat can use the commands (the filter is per chat,
not per user). Commands only act on new messages — editing an old message
does not re-run it. Unknown commands get a short reply pointing at `/help`;
commands addressed to other bots (`/cmd@OtherBot`) are ignored.

Task text is markdown (that's how Todoist stores it): links, **bold**,
*italic*, ~~strike~~, `code` and bare URLs are rendered in `/today` lines and
in `/add`, `/done`, `/undo` and `/edit` replies.

The bot keeps all state in memory: a restart loses the saved `/today` list, so
`/undo` needs a fresh `/today` first — `/done` re-fetches and keeps working
after a restart. Each `/today` (and each `/add` list-refresh) replaces the
saved list, which closes the `/undo` window for tasks completed before it.

Recurring tasks: completing one never leaves Todoist's completed state — the
same task stays active, just rolled to its next date. `/undo` therefore
refuses them instead of reporting a false success; bring the occurrence back
by rescheduling in Todoist.

While a command is talking to Todoist (`/add`, `/today`, `/done`, `/undo`,
`/edit`), Telegram shows the bot typing until the reply arrives.

## Configuration

Four variables are required — the bot exits with a clear error if any is
missing (exit code 2; the NixOS unit does not restart on configuration
errors) — plus one optional:

| Variable | Meaning |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Bot token from [@BotFather](https://t.me/BotFather); an invalid token aborts startup with exit code 2 (the token itself is never printed) |
| `TODOIST_API_TOKEN` | Todoist → Settings → Integrations → API token. An invalid or expired token is a startup error (exit code 1) — the unit retries every 5 s and gives up after 5 failures in 5 minutes |
| `TODOIST_PROJECT_ID` | Project ID: the part after the last dash of the project URL, `https://app.todoist.com/app/project/<name-slug>-<id>` (e.g. `6X4Vw2Hfmg73Q2XR`). Loaded once at startup; a bad ID aborts startup with exit code 2 |
| `ALLOWED_CHAT_IDS` | Comma-separated Telegram chat IDs allowed to use the bot. Group IDs are negative (e.g. `-1001234567890`) — learn one by forwarding a group message to an ID-reporting bot such as [@getidsbot](https://t.me/getidsbot) |
| `TIMEZONE` | Optional IANA time zone for the bot's "today" calculations (e.g. `Asia/Kolkata`). The server and the bot's users are often in different time zones, so set this explicitly; an invalid value aborts startup with exit code 2. Falls back to the server's local time zone. Note: `/add` due dates parse in the Todoist *account's* time zone — `TIMEZONE` only shifts the bot's today boundary |

## Local development

```bash
nix-shell
export TELEGRAM_BOT_TOKEN=123456:ABC...
export TODOIST_API_TOKEN=0123456789abcdef...
export TODOIST_PROJECT_ID="<your-project-id>"
export ALLOWED_CHAT_IDS=123456789
export TIMEZONE=Asia/Kolkata
python -m iwcb_bot
```

The shell provides Python with all dependencies on `PYTHONPATH`, plus the
tooling:

```bash
black src tests            # format Python
basedpyright --pythonpath "$(command -v python)"   # type-check Python
python -m pytest -q        # run the test suite
nixfmt *.nix               # format Nix files
```

Tip: keep tokens out of shell history with an env file kept **outside the
repo** (a `.env` inside the repo is excluded from the Nix build input — it
would otherwise be copied into the world-readable Nix store — but outside is
safer still):

```bash
set -a; source ~/secrets/iwcb.env; set +a
```

## Building

```bash
nix-build
./result/bin/iwcb-telegram-bot   # run the built package (needs env vars)
```

## Deploying on NixOS

Pin this repo with [niv](https://github.com/nmattia/niv) in your NixOS config
repo (`niv add <this-repo-url>`), then import the module:

```nix
{ config, ... }:
let
  sources = import ./nix/sources.nix;
in
{
  imports = [ "${sources.iwcb-telegram-bot}/module.nix" ];

  services.iwcb-telegram-bot = {
    enable = true;
    projectId = "<your-project-id>";
    allowedChatIds = [ 123456789 ];
    timeZone = "Asia/Kolkata";
    environmentFile = config.sops.templates."iwcb-bot.env".path;
  };
}
```

`projectId`, `allowedChatIds` and `environmentFile` are required options —
`nixos-rebuild` fails at evaluation time if any is missing. `timeZone` is
optional (null = the server's time zone) but should usually be set: the
server and the bot's users are often in different time zones, and "today"
means the users' today.

### Secrets with sops-nix

This repo contains no secrets. The importing repo decrypts them and renders a
systemd environment file (inside the same module as above — the
`{ config, ... }:` header is what makes `config.sops` available):

```nix
sops.defaultSopsFile = ./secrets.yaml;

sops.secrets.telegram_bot_token = { };
sops.secrets.todoist_api_token = { };

sops.templates."iwcb-bot.env".content = ''
  TELEGRAM_BOT_TOKEN=${config.sops.placeholder.telegram_bot_token}
  TODOIST_API_TOKEN=${config.sops.placeholder.todoist_api_token}
'';

services.iwcb-telegram-bot = {
  enable = true;
  projectId = "<your-project-id>";
  allowedChatIds = [ 123456789 ];
  timeZone = "Asia/Kolkata";
  environmentFile = config.sops.templates."iwcb-bot.env".path;
};
```

The environment file must contain only the two tokens. If it also sets
`TODOIST_PROJECT_ID`, `ALLOWED_CHAT_IDS` or `TIMEZONE`, the module options
still win: the module writes them to a second environment file that systemd
reads *after* this one (later `EnvironmentFile=` entries override earlier
ones, and all of them override `Environment=`).

### systemd hardening

The unit runs as a dynamic user with no writable filesystem, no capabilities,
no device/node access, an allowlist-only syscall filter
(`@system-service ~@privileged`), restricted address families and a read-only
view of the system. If a future systemd/NixOS version rejects one of the
options, trim it from `module.nix`.

## License

MIT — see [LICENSE](LICENSE).
