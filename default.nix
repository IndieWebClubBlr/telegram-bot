{
  pkgs ? import <nixpkgs> { },
}:

let
  inherit (pkgs) lib;
  py = pkgs.python3.pkgs;
in
py.buildPythonApplication {
  pname = "todoist-telegram-bot";
  version = "1.0.0";
  pyproject = true;

  # Allowlist: only ship what the build needs.  A filename denylist
  # would copy any future secret file (.env.local, secrets.yaml, …)
  # into the world-readable Nix store; lib.cleanSource alone does
  # not honor .gitignore either.  The second layer drops bytecode
  # that may sit inside src/.
  src = lib.cleanSourceWith {
    src = lib.sourceByRegex ./. [
      "^src(/.*)?$"
      "^tests(/.*)?$"
      "^pyproject\\.toml$"
      "^README\\.md$"
      "^LICENSE$"
    ];
    filter =
      path: type:
      let
        base = baseNameOf path;
      in
      base != "__pycache__" && !(lib.hasSuffix ".pyc" base) && lib.cleanSourceFilter path type;
  };

  build-system = [ py.hatchling ];

  dependencies = [
    py.python-telegram-bot
    py.todoist-api-python
    py.markdown-it-py
    py.linkify-it-py
  ];

  # Run the offline command suite in the build sandbox (no network; the
  # tests' socket guard makes any accidental real call fail loudly).
  nativeCheckInputs = [
    py.pytest
    py.pytestCheckHook
  ];

  pythonImportsCheck = [
    "todoist_bot"
    "todoist_bot.bot"
    "todoist_bot.commands"
    "todoist_bot.config"
    "todoist_bot.messaging"
    "todoist_bot.state"
    "todoist_bot.todoist_client"
  ];

  meta = {
    description = "A Telegram bot for managing a Todoist project";
    homepage = "https://github.com/IndieWebClubBlr/telegram-bot";
    license = lib.licenses.mit;
    mainProgram = "todoist-telegram-bot";
  };
}
