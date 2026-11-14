# Design

This document explains how the Todoist Telegram Bot is put together and why it
is put together that way. It is aimed at future maintainers who need to change
behaviour without undoing a decision they cannot see. For usage and deployment
instructions, see [README.md](./README.md).

## What the bot is

A Telegram front end for one Todoist project. Users talk to it in a chat
(group or private) with slash commands; it reads and writes tasks through the
Todoist API. Lists it sends stay interactive: `/done`, `/undo`, `/edit`,
`/assign` and `/due` act on the short ids shown in the most recent list.

A running process is bound to exactly one Telegram bot token and one Todoist
project. Running several bots means running several processes, each with its
own configuration and secrets.

## Goals and non-goals

Goals:

- A small, predictable command surface over Todoist.
- Lists as durable, actionable UI, not just query output.
- Never leave local display state claiming something the remote write did not
  actually do.
- Operationally boring: env-only configuration, fail fast on bad config,
  restart on transient failure.
- Privacy-safe logs by default: identifiers and counts, never content.
- Tests that run offline and cover real behaviour, not mocks of behaviour.
- Cheap deployment on one machine with Nix.

Non-goals:

- One process serving multiple Todoist projects; use one instance per project.
- Durable local state, a database, migrations, or a webhook server.
- Horizontal scaling or sharing state between instances.
- Full Todoist feature parity (labels, filters, comments, sections management).
- Localisation, plugins, or per-user persistent preferences.

Anti-goal: reporting success when the remote write failed, or silently changing
the meaning of a displayed list. The design prefers an explicit error or stale
display over a confident lie.

## Runtime architecture

```
                    Telegram (long polling)
                              | updates
                              v
+----------------------------------------------------------+
| bot.py         PTB Application, auth filter, lifecycle,   |
|                morning-announce loop, top-level on_error  |
+---------------+------------------------------------------+
                | commands
                v
+----------------------------------------------------------+
| commands.py    command classes + shared handler shell:    |
|                guards, typing, chat lock, error replies   |
+-------+-----------------------+--------------------------+
        |                       |
        v                       v
+--------------------+  +-----------------------------------+
| state.py           |  | messaging.py                      |
| pure in-memory     |  | Telegram effects: replies,        |
| state + event FSM  |  | reactions, rendering, list edits  |
+--------------------+  +--------------+--------------------+
                                       |
                                       v
                        +-----------------------------------+
                        | todoist_client.py                 |
                        | ABC + HTTP implementation         |
                        +-----------------------------------+
```

Module responsibilities:

- **`config.py`** turns environment variables into a frozen `Config`. It is the
  only place that validates configuration and the only place that knows how a
  wall-clock announcement time, time zone, or assignee mapping is encoded.
- **`__main__.py`** loads configuration, installs logging redaction, resolves
  the configured project once, and starts polling. It owns process exit codes.
- **`bot.py`** wires the PTB application: the chat allow-list filter, handler
  registration, lifecycle hooks, the morning loop, and the last-resort error
  handler.
- **`commands.py`** defines each command. A shared shell supplies argument
  guards, the typing indicator, the per-chat lock, and the common failure
  replies, so individual commands describe only their own work.
- **`state.py`** is deliberately pure: no Telegram and no Todoist imports. It
  holds the chat's displayed tasks and lists, resolves short ids, and
  implements the event state machine.
- **`messaging.py`** is the effect layer. It renders task lines, chunks long
  messages, sends and edits lists, applies reactions, and degrades safely when
  Telegram rejects an edit.
- **`todoist_client.py`** exposes an abstract client and one HTTP
  implementation. SDK and HTTP failures are translated into `TodoistError`
  subclasses so command code never depends on SDK exception types.

## Core design decisions

### One project per process

The command grammar assumes an implicit current project: `/today` means "this
project's today", `/add` always adds here, and IDs are resolved against lists
from this project. Supporting several projects in one process would require a
project selector on every command or in every chat, plus per-chat selection
state. Running one process per project keeps commands short, makes the
allow-list unambiguous, and gives each project its own failure domain. The Nix
module turns that into a per-instance systemd unit.

### Display state is in memory, and Todoist is the source of truth

Each chat keeps a map of task id to a small task record, plus ordered id lists:
the chat's current list and each user's `/mine` list. Lists carry presentation
(order, section dividers); the map carries the details needed to resolve and
render tasks. Writing a list prunes task records no stored list references.

Nothing is persisted to disk. The last shown list survives until process exit
or until it is replaced; after a restart, users run a list command again. This
avoids a schema, migrations, backups, and a second source of truth. The cost is
explicit in the UX: `/done` only matches tasks in a currently displayed list.

Lists are also per chat, and the bot does not fan mutations out across chats.
A task completed in one chat can still look active in another chat's older
list until that list is refreshed; acting on the stale line repeats the
remote write (for a recurring task, that can advance the series again).
Refreshing the list is the recovery path.

### Short ids are scoped to what was displayed

Todoist ids are long; Telegram users type short suffixes. The bot derives the
shortest unique lowercase suffix per displayed set, grows it on collision, and
resolves case-insensitively. Resolution is scoped to the lists the sender can
currently see, not to the whole project. That prevents acting on a task the
user never saw, and keeps the id stable enough to type.

### State changes have two phases

Mutating commands follow a begin → remote write → commit shape:

1. **Begin** resolves the id against displayed lists and validates the event
   against the task's current phase. It writes nothing.
2. The command performs the remote Todoist write.
3. **Commit** revalidates and writes the local display state, returning the
   lists that must be re-edited.

This means a Todoist failure cannot leave a task struck through locally, and a
concurrent phase change between begin and commit is refused rather than
half-applied. Events (`Complete`, `Reopen`, `Edit`, `Assign`, `Reschedule`) and
outcomes (`Permitted`, `Rejected`, `Applied`, …) are explicit types rather than
boolean flags, so command code has to handle each case.

### Local rendering follows remote truth for recurring tasks

Completing a recurring task does not just strike it out: Todoist rolls the
same task id to the next occurrence, and the local strike is annotated with
that rolled date. The completed occurrence's date is kept separately from the
rolled date, so a later fetch of the next occurrence is not mistaken for the
occurrence this chat already completed and filtered out of the morning list.
Reopening such a task is refused locally because it would close the next
occurrence instead of restoring the previous one. Local state never invents a
next date; it uses what the API returned.

### Concurrency is deliberately narrow

PTB processes updates one at a time, and mutating commands additionally take a
per-chat lock. The other writer is the morning-announce loop. That combination
is the tested model. Do not enable PTB's `concurrent_updates` without
revisiting state locking, list edits, and the morning loop together.

### Failures are split by kind

- Configuration errors (bad env, unknown project, a bot token Telegram
  rejects) exit 2. The NixOS unit sets `RestartPreventExitStatus = 2`, so a
  typo does not produce a restart loop.
- Startup failures that may be transient (Todoist/Telegram unreachable) exit 1
  and are retried by systemd. A Todoist token rejection is not distinguishable
  from another Todoist startup failure, so it exits 1 and relies on the unit's
  start limit instead of restarting forever.
- Runtime command failures keep the bot alive and answer with a distinct
  failure reply. Telegram send failures say the action may have happened
  remotely and point at a list command.
- The morning loop contains errors per chat and per cycle: one broken chat does
  not silence the others, and a fetch failure is announced rather than looking
  like "nothing due".

### Logging is an identifier stream

Logs carry command names, numeric chat/user/task ids, counts, durations and
outcomes. They never carry message text, command arguments, names, usernames,
chat titles or task content. Third-party DEBUG logs are pinned to WARNING
because they dump payloads, and a handler-level redaction filter scrubs the two
tokens from anything that still escapes. Preserve this contract when adding
logging.

### Reactions are a presentation choice

`REACTION_REPLIES` lets action commands answer with an emoji reaction in group
chats instead of a text reply. Private chats always get text. List commands
always send their lists. The domain logic does not know which presentation was
used; `messaging.py` decides based on configuration and chat type.

### The morning announcement is in-process

A single loop sleeps until the next wall-clock target, fetches the project's
dated tasks once, and sends to allowed group chats only. The next target is
recomputed each cycle from the resolved time zone, so daylight-saving shifts
are picked up on the next cycle rather than persisting until restart. Fetching is chat-independent and happens outside chat
locks; only sending and state writes are locked per chat.

## Configuration and secrets

Configuration is environment-only. There are no config files or per-user
settings. Required values are the two tokens, the project id and the allowed
chat ids; everything else has a documented default.

The NixOS module turns non-secret options into a generated environment file and
lists it *after* the secrets file, so systemd's later-file-wins rule makes the
options authoritative for project id and chat ids. `TIMEZONE` is written only
when the option is set, so a null `timeZone` leaves the secrets file free to
supply it. The secrets file supplies only the tokens. Assignee
mentions can be supplied by either; keeping them in secrets avoids publishing
names and handles into the Nix store.

## Deployment model

The package is a plain Nix derivation; `default.nix` builds it and runs the
test suite in the sandbox. `module.nix` is a vanilla NixOS module (no flakes)
exposing `services.todoist-telegram-bot.instances.<name>`. Each enabled
instance becomes a hardened systemd unit named `todoist-telegram-bot-<name>`,
with its own secrets file, generated options file and dynamic user.

Instances share no state, so any number can run on one machine. Each needs a
distinct Telegram bot token; Telegram permits one long-polling consumer per bot.
They may share a Todoist account as long as each targets a different project.

Hardening is intentionally strict: read-only filesystem view, no capabilities,
a syscall allow-list, restricted address families and a dynamic user. The bot
writes nothing, so there is no `StateDirectory`. If a future systemd version
rejects an option, trim it in `module.nix` rather than weakening the unit
wholesale.

`module-test.nix` evaluates the module without booting a machine and asserts
the unit-per-enabled-instance wiring, per-instance files and assertions. It is
part of the normal verification battery.

## Testing strategy

Tests run offline and are part of the definition of correct:

- Command tests drive real PTB objects through the registered handlers with
  fake Telegram and Todoist collaborators; a socket guard makes accidental
  network calls fail.
- User-facing strings are treated as the UX contract and asserted exactly.
  Domain invariants (id resolution, phase transitions, chunking) get
  property-level or derived-bound assertions rather than magic numbers.
- The NixOS module has the evaluation test above; the package build runs the
  Python suite inside the Nix sandbox.
- `just check` is the single entry point for the full battery.

Live API testing is intentionally absent: credentials must not be required to
verify a change.

## Where to extend

- **New command:** subclass the shared command base (or the list-action base),
  register it in `bot.py`, add it to the `/help` text, and cover it in
  `tests/test_commands.py`.
- **New Todoist operation:** add it to the `TodoistClient` abstraction, the HTTP
  implementation, and the fake used by tests.
- **New configuration option:** parse and validate it in `config.py`, add a Nix
  option (and generated env line if non-secret), then document it in the README.
- **Rendering or Telegram behaviour:** keep it in `messaging.py` so commands
  stay about intent.
- **State or validation rule:** keep it in `state.py`, which stays free of
  Telegram and Todoist imports.

When changing versions, update `pyproject.toml`, `default.nix` and
`src/todoist_bot/__init__.py` together.