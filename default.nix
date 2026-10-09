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

  # Denylist: lib.cleanSource alone would copy .env (tokens) into the
  # world-readable Nix store — it does not honor .gitignore.
  src = lib.cleanSourceWith {
    src = ./.;
    filter =
      path: type:
      let
        base = baseNameOf path;
        denied = builtins.elem base [
          ".env"
          ".direnv"
          ".direnvrc"
          ".envrc"
          ".gitignore"
          "__pycache__"
          ".pytest_cache"
        ];
      in
      !denied && !(lib.hasSuffix ".pyc" base) && lib.cleanSourceFilter path type;
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
    "iwcb_bot.config"
    "iwcb_bot.todoist_client"
  ];

  meta = {
    description = "IWCB Telegram Bot — a Telegram bot for IWCB";
    homepage = "https://github.com/IndieWebClubBlr/telegram-bot";
    license = lib.licenses.mit;
    mainProgram = "iwcb-telegram-bot";
  };
}
