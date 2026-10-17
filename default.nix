{
  pkgs ? import <nixpkgs> { },
}:

let
  inherit (pkgs) lib;
  py = pkgs.python3.pkgs;
in
py.buildPythonApplication {
  pname = "iwcb-telegram-bot";
  version = "0.1.0";
  pyproject = true;

  # Allowlist: only ship what the build needs.  A filename denylist
  # would copy any future secret file (.env.local, secrets.yaml, …)
  # into the world-readable Nix store; lib.cleanSource alone does
  # not honor .gitignore either.  The second layer drops bytecode
  # that may sit inside src/.
  src = lib.cleanSourceWith {
    src = lib.sourceByRegex ./. [
      "^src(/.*)?$"
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

  pythonImportsCheck = [
    "iwcb_bot"
    "iwcb_bot.bot"
    "iwcb_bot.commands"
    "iwcb_bot.config"
    "iwcb_bot.messaging"
    "iwcb_bot.state"
    "iwcb_bot.todoist_client"
  ];

  meta = {
    description = "IWCB Telegram Bot — a Telegram bot for IWCB";
    homepage = "https://github.com/IndieWebClubBlr/telegram-bot";
    license = lib.licenses.mit;
    mainProgram = "iwcb-telegram-bot";
  };
}
