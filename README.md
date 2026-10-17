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
| `/add <text>` | Add a task using Todoist [quick-add syntax](https://developer.todoist.com/api/v1/#operation/quick_add_api_v1_tasks_quick_post): `/add buy milk tomorrow p1 @errand /Groceries +Abhinav`. Sections (`/Name`) and assignees (`+Name`) work because the configured project is auto-prepended to the quick-add text; a `#OtherProject` in the text is overridden — the configured project always wins. If the new task falls within the last shown list's window (today, when no list has been shown yet), the reply also contains that list refreshed — IDs shown earlier may have shifted; if that refresh fails, the confirmation still arrives with a note (under `REACTION_REPLIES=true` it cannot — see that row) |
| `/today` | Tasks in the project due today or overdue (⚠), sorted by due date; deadlines count as due dates (the earlier of the two wins when a task has both). Each line starts with a short unique ID shown as `[id]` in lowercase; overdue lines also end with `— due <date>`, with the year appended when it differs from the current one |
| `/tomorrow` | Everything `/today` shows plus tasks due tomorrow; header `Due by <tomorrow>:`, every line carries its due date |
| `/week` | Cumulative through end of today + 6 days (7 calendar days including today); header `Due by <date>:`, every line carries its due date |
| `/mine` | Your own tasks (up to 10): due/overdue first, then all future-dated sorted by date, then tasks with no date, under section headers `📅 Due`, `📆 Upcoming` and `🗓 No date`. Requires the sender to be mapped in `ASSIGNEE_MENTIONS`; unmapped senders get a hint instead of a list. Each `/mine` replaces your previous `/mine` list (the old message stops updating); other users' `/mine` lists are untouched |
| `/done <id>` | Complete the task whose ID ends with `<id>` from the last shown list (`/today`, `/tomorrow` or `/week`) or your `/mine` list (matching is case-insensitive; the same task in both lists is one match). Strikes the line in place (lists that fit a single message) in every list that shows it — including other users' `/mine` lists — and confirms; for a recurring task the struck line shows the rolled next-occurrence date and the confirmation includes it too. A task already struck in the list is refused (completing it again would close the next occurrence of a recurring task). Stale data (task changed elsewhere) gives a safe error |
| `/undo <id>` | Reopen a task completed with `/done` from the last shown list or your `/mine` list, un-striking it in every list that shows it. Refuses recurring tasks — completing one rolls it to its next occurrence |
| `/edit <id> <new text>` | Edit the task whose ID ends with `<id>` from the last shown list or your `/mine` list; the new text appears in every list that shows the task. Already-completed tasks are refused. The text is stored verbatim — quick-add parsing (dates, `p1`, `@labels`) happens only on creation (`/add`), so those tokens become part of the task name. Recurring: the new name applies to all occurrences. Max 500 characters |
| `/assign <id> <assignee>` | Assign the task whose ID ends with `<id>` to a project collaborator. The assignee is a Telegram `@handle` (resolved through `ASSIGNEE_MENTIONS`, with or without the `@`), a numeric Telegram id from the same mapping, or a Todoist collaborator name (`Priya Singh` — multi-word names work). The new assignee appears in every list that shows the task, and mapped names render as clickable mentions. Already-completed tasks are refused; a handle shared by several collaborators is ambiguous — use the full Todoist name. Clearing an assignee is not supported (Todoist UI only) |
| `/due <id> <when>` | Change when the task whose ID ends with `<id>` is due. `<when>` is any Todoist natural-language date — `tomorrow`, `next monday 5pm`, `every friday`, or `no date` to clear — parsed by Todoist's own server-side parser (the same one quick-add uses); the reply echoes what was understood (`📅 buy milk → Fri 17 Oct`). Works on recurring tasks (updates the pattern). Already-completed tasks are refused. Max 150 characters |
| `/project` | Link to the configured project in Todoist |

Only chats listed in `ALLOWED_CHAT_IDS` are served; everyone else is ignored.
Any member of an allowed chat can use the commands (the filter is per chat,
not per user). Commands only act on new messages — editing an old message
does not re-run it. Unknown commands get a short reply pointing at `/help`
(or a 🤨 reaction under `REACTION_REPLIES=true`);
commands addressed to other bots (`/cmd@OtherBot`) are ignored.

Task text is markdown (that's how Todoist stores it): links, **bold**,
*italic*, ~~strike~~, `code` and bare URLs are rendered in list lines and
in `/add`, `/done`, `/undo`, `/edit` and `/assign` replies.

The bot keeps all state in memory: a restart loses the saved lists, so `/done`,
`/undo` and `/edit` need a fresh `/today`, `/tomorrow`, `/week` or `/mine`
first. Each list command (and each `/add` list-refresh) replaces the chat-level
saved list, which closes the `/undo` window for tasks completed before it; a
`/mine` replaces only your own per-user list. Lists from a previous day are
discarded — the bot asks for a fresh one instead of acting on stale IDs.
`/done`, `/undo` and `/edit` resolve IDs against your `/mine` list and the
chat-level list together, and apply the change everywhere the task appears.

Recurring tasks: completing one never leaves Todoist's completed state — the
same task stays active, just rolled to its next date. `/undo` therefore
refuses them instead of reporting a false success; bring the occurrence back
by rescheduling in Todoist.

While a command is talking to Todoist (`/add`, `/today`, `/tomorrow`, `/week`,
`/mine`, `/done`, `/undo`, `/edit`, `/assign`, `/due`), Telegram shows the bot typing until the reply (or `REACTION_REPLIES` reaction) arrives.

## Daily morning list

Unless disabled with `MORNING_ANNOUNCE_TIME=off`, the bot proactively sends
the day's task list — titled **Daily Task Reminder**, otherwise same rendering
as `/today` — to every allowed **group** chat (private chats are skipped) at
the configured wall-clock time
(default 09:00) in the `TIMEZONE` zone. The sent list becomes that chat's last
shown list — `/done`, `/undo` and `/edit` work on it immediately, no fresh
`/today` needed, and the title stays visible as lines are struck. In forum
groups the message lands in the General topic (commands still answer in their
own topic). An empty day gets the title over "Nothing due today 🎉", or
nothing at all with `MORNING_ANNOUNCE_WHEN_EMPTY=false` (the chat's stored
list from the previous day is retired either way); a Todoist
failure is reported in-chat so silence is never mistaken for "nothing due".
If the bot is down at the scheduled time, the list is not re-sent until the
next day.

## Configuration

Four variables are required — the bot exits with a clear error if any is
missing (exit code 2; the NixOS unit does not restart on configuration
errors) — plus a few optional ones:

| Variable | Meaning |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Bot token from [@BotFather](https://t.me/BotFather); an invalid token aborts startup with exit code 2 (the token itself is never printed) |
| `TODOIST_API_TOKEN` | Todoist → Settings → Integrations → API token. An invalid or expired token is a startup error (exit code 1) — the unit retries every 5 s and gives up after 5 failures in 5 minutes |
| `TODOIST_PROJECT_ID` | Project ID: the part after the last dash of the project URL, `https://app.todoist.com/app/project/<name-slug>-<id>` (e.g. `6X4Vw2Hfmg73Q2XR`). Loaded once at startup; a bad ID aborts startup with exit code 2 |
| `ALLOWED_CHAT_IDS` | Comma-separated Telegram chat IDs allowed to use the bot. Group IDs are negative (e.g. `-1001234567890`) — learn one by forwarding a group message to an ID-reporting bot such as [@getidsbot](https://t.me/getidsbot) |
| `TIMEZONE` | Optional IANA time zone for the bot's "today" calculations (e.g. `Asia/Kolkata`). The server and the bot's users are often in different time zones, so set this explicitly; an invalid value aborts startup with exit code 2. Falls back to the server's local time zone — but only its offset at startup, so on a DST-observing server the morning reminder drifts by an hour after a transition until restart; set `TIMEZONE` to avoid this. Naive due datetimes (the normal Todoist shape) are taken as wall-clock times in this zone; timezone-aware dues are converted into it before the calendar day is taken |
| `MORNING_ANNOUNCE_TIME` | Optional wall-clock time (24h `HH:MM`, in the `TIMEZONE` zone) at which the bot proactively sends the day's task list to every allowed group chat (private chats are skipped). Defaults to `09:00`; set to `off` to disable. An invalid value aborts startup with exit code 2 |
| `MORNING_ANNOUNCE_WHEN_EMPTY` | `true` (default) or `false` — whether the daily list is sent when nothing is due. `true` posts the title over "Nothing due today 🎉"; `false` stays silent and clears the chat's stored list so the previous day's message stops updating. Case-insensitive; an invalid value aborts startup with exit code 2 |
| `REACTION_REPLIES` | `false` (default) or `true` — in group chats, action-command feedback (`/add`, `/done`, `/undo`, `/edit`, `/assign`, `/due`, unknown-command hints and `/mine`'s configuration hints) becomes a reaction on the command message instead of a text reply: 👍 success, 🤨 soft refusal (usage error, no match, already completed), 👎 failure (Todoist/API errors). One reaction per command, so an `/add` whose list refresh fails still gets only 👍 — the stale-list note has no channel in this mode. List messages (`/today`, `/tomorrow`, `/week`, `/mine`, `/help`, `/project`) are always sent as text, private chats always keep text replies, and a rejected reaction logs at `DEBUG` and stays silent — no text fallback. Case-insensitive; an invalid value aborts startup with exit code 2 |
| `ASSIGNEE_MENTIONS` | Optional `;`-separated `Todoist Name=Telegram` pairs mapping shared-project assignee names to Telegram mentions — `Abhinav Nair=123456789;Priya Singh=@priya`. Names match case-insensitively; mapped names render as clickable mentions, unmapped assignees as plain text, unassigned tasks show no suffix. Names and targets must each be unique (compared case-insensitively) — a duplicate aborts startup with exit code 2. Prefer numeric ids where the mapping is used for identity (`/mine`): a Telegram `@handle` can be renamed and re-acquired by someone else |
| `LOG_LEVEL` | Optional bot log level: `DEBUG`, `INFO`, `WARNING` or `ERROR` (case-insensitive). Defaults to `INFO`. `DEBUG` logs each command action and Todoist API call with durations, and includes numeric chat/user/task ids for correlation — it never logs names, usernames, chat titles, task text or command arguments. An invalid value aborts startup with exit code 2 |

## Assignees

Task lines in `/today`, `/tomorrow`, `/week`, `/mine` and the daily reminder
show the Todoist assignee between the task text and the due date, e.g.
`[ab]  buy milk — @abnv — due Tue 9 Oct`. The project's collaborator
list is fetched once on first need and cached; an unknown assignee id
(e.g. a collaborator added after startup) triggers one refresh.

`/mine` also needs the reverse direction: the sending Telegram user is
matched to their Todoist collaborator name through `ASSIGNEE_MENTIONS`
(numeric user id first, then `@username`). Numeric ids are stable;
an `@username` mapping binds identity to a handle a third party can
acquire after a rename, so prefer ids here. A sender who is not mapped
gets a configuration hint instead of a list.

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
