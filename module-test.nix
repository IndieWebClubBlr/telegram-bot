# Evaluation test for module.nix: two enabled instances and one disabled.
# Run with:
#   nix-instantiate --eval --strict ./module-test.nix   # prints true
let
  pkgs = import <nixpkgs> { };
  inherit (pkgs) lib;

  eval = lib.evalModules {
    specialArgs = { inherit pkgs; };
    modules = [
      ./module.nix
      {
        # Stubs for the NixOS options this module writes to, so the
        # module can be evaluated without importing a whole NixOS system.
        options.systemd.services = lib.mkOption {
          type = lib.types.attrsOf lib.types.anything;
          default = { };
        };
        options.assertions = lib.mkOption {
          type = lib.types.listOf lib.types.anything;
          default = [ ];
        };

        config.services.todoist-telegram-bot.instances = {
          club = {
            enable = true;
            environmentFile = "/run/secrets-rendered/todoist-club.env";
            projectId = "aaa";
            allowedChatIds = [ 1 ];
            timeZone = "Asia/Kolkata";
          };
          personal = {
            enable = true;
            environmentFile = "/run/secrets-rendered/todoist-personal.env";
            projectId = "bbb";
            allowedChatIds = [ 2 ];
          };
          paused = {
            enable = false;
            environmentFile = "/run/secrets-rendered/todoist-paused.env";
            projectId = "ccc";
            allowedChatIds = [ 3 ];
          };
        };
      }
    ];
  };

  services = eval.config.systemd.services;
  envFiles = name: map toString services.${name}.serviceConfig.EnvironmentFile;
in
assert services ? todoist-telegram-bot-club;
assert services ? todoist-telegram-bot-personal;
# enable = false produces no unit
assert !(services ? todoist-telegram-bot-paused);
# Each unit reads its own secrets file first and its own options file second.
assert lib.head (envFiles "todoist-telegram-bot-club") == "/run/secrets-rendered/todoist-club.env";
assert
  lib.head (envFiles "todoist-telegram-bot-personal") == "/run/secrets-rendered/todoist-personal.env";
assert lib.length (envFiles "todoist-telegram-bot-club") == 2;
# The unit actually runs the bot package (this forces the package default).
assert lib.hasInfix "/bin/todoist-telegram-bot"
  services.todoist-telegram-bot-club.serviceConfig.ExecStart;
assert lib.any (p: lib.hasInfix "todoist-telegram-bot-options-club.env" p) (
  envFiles "todoist-telegram-bot-club"
);
assert lib.any (p: lib.hasInfix "todoist-telegram-bot-options-personal.env" p) (
  envFiles "todoist-telegram-bot-personal"
);
# Three assertions per declared instance, all satisfied.
assert lib.length eval.config.assertions == 9;
assert lib.all (a: a.assertion) eval.config.assertions;
true
