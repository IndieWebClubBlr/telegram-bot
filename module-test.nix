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

        config.services.iwcb-telegram-bot.instances = {
          club = {
            enable = true;
            environmentFile = "/run/secrets-rendered/iwcb-club.env";
            projectId = "aaa";
            allowedChatIds = [ 1 ];
            timeZone = "Asia/Kolkata";
          };
          personal = {
            enable = true;
            environmentFile = "/run/secrets-rendered/iwcb-personal.env";
            projectId = "bbb";
            allowedChatIds = [ 2 ];
          };
          paused = {
            enable = false;
            environmentFile = "/run/secrets-rendered/iwcb-paused.env";
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
assert services ? iwcb-telegram-bot-club;
assert services ? iwcb-telegram-bot-personal;
# enable = false produces no unit
assert !(services ? iwcb-telegram-bot-paused);
# Each unit reads its own secrets file first and its own options file second.
assert lib.head (envFiles "iwcb-telegram-bot-club") == "/run/secrets-rendered/iwcb-club.env";
assert
  lib.head (envFiles "iwcb-telegram-bot-personal") == "/run/secrets-rendered/iwcb-personal.env";
assert lib.length (envFiles "iwcb-telegram-bot-club") == 2;
# The unit actually runs the bot package (this forces the package default).
assert lib.hasInfix "/bin/iwcb-telegram-bot"
  services.iwcb-telegram-bot-club.serviceConfig.ExecStart;
assert lib.any (p: lib.hasInfix "iwcb-options-club.env" p) (envFiles "iwcb-telegram-bot-club");
assert lib.any (p: lib.hasInfix "iwcb-options-personal.env" p) (
  envFiles "iwcb-telegram-bot-personal"
);
# Three assertions per declared instance, all satisfied.
assert lib.length eval.config.assertions == 9;
assert lib.all (a: a.assertion) eval.config.assertions;
true
