# NixOS module for the IWCB Telegram Bot.
# Source: https://github.com/IndieWebClubBlr/telegram-bot
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.iwcb-telegram-bot;

  # Non-secret settings, as an environment file so that systemd's
  # EnvironmentFile > Environment= precedence cannot be subverted by
  # the secrets file. Option types (alphanumeric ID, int list, IANA
  # time zone pattern) keep every value free of newlines and spaces.
  optionsEnvFile = pkgs.writeText "iwcb-options.env" (
    ''
      TODOIST_PROJECT_ID=${cfg.projectId}
      ALLOWED_CHAT_IDS=${lib.concatMapStringsSep "," toString cfg.allowedChatIds}
      MORNING_ANNOUNCE_TIME=${cfg.morningAnnounceTime}
      MORNING_ANNOUNCE_WHEN_EMPTY=${lib.boolToString cfg.morningAnnounceWhenEmpty}
      REACTION_REPLIES=${lib.boolToString cfg.reactionReplies}
      LOG_LEVEL=${cfg.logLevel}
    ''
    + lib.optionalString (cfg.timeZone != null) "TIMEZONE=${cfg.timeZone}\n"
    + lib.optionalString (cfg.assigneeMentions != { }) (
      "ASSIGNEE_MENTIONS=${
        lib.concatStringsSep ";" (
          lib.mapAttrsToList (name: target: "${name}=${target}") cfg.assigneeMentions
        )
      }\n"
    )
  );
in
{
  options.services.iwcb-telegram-bot = {
    enable = lib.mkEnableOption "the IWCB Telegram Bot";

    package = lib.mkOption {
      type = lib.types.package;
      default = import ./default.nix { inherit pkgs; };
      defaultText = lib.literalExpression "import ./default.nix { inherit pkgs; }";
      description = "The bot package to run.";
    };

    projectId = lib.mkOption {
      type = lib.types.strMatching "[A-Za-z0-9]+";
      example = "6X4Vw2Hfmg73Q2XR";
      description = ''
        Todoist project ID: the part after the last dash of the project
        URL, https://app.todoist.com/app/project/<name-slug>-<id>.
        Loaded once at startup; the bot refuses to start if the ID does
        not exist. Alphanumeric only, so it is safe to write into the
        generated environment file.
      '';
    };

    allowedChatIds = lib.mkOption {
      type = lib.types.listOf lib.types.int;
      example = [ 123456789 ];
      description = ''
        Telegram chat IDs allowed to use the bot. Messages from any
        other chat are ignored.
      '';
    };

    timeZone = lib.mkOption {
      type = lib.types.nullOr (lib.types.strMatching "[A-Za-z0-9_+/-]+");
      default = null;
      example = "Asia/Kolkata";
      description = ''
        IANA time zone the bot uses for its "today" calculations
        (TIMEZONE). The server and the bot's users are often in
        different time zones, so this should usually be the users'
        time zone rather than the server's. When null, the bot falls
        back to the server's local time zone.
      '';
    };

    morningAnnounceTime = lib.mkOption {
      type = lib.types.strMatching "(([01][0-9]|2[0-3]):[0-5][0-9]|off)";
      default = "09:00";
      example = "08:30";
      description = ''
        Wall-clock time (HH:MM, in the time zone above) at which the
        bot proactively sends the day's task list to every allowed
        group chat — private chats are skipped
        (MORNING_ANNOUNCE_TIME). The sent list is actionable —
        /done, /undo and /edit work on it immediately. Set to "off"
        to disable the daily announcement. If the bot is down at the
        scheduled time, the list is not re-sent until the next day.
      '';
    };

    morningAnnounceWhenEmpty = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Whether the daily announcement is sent when nothing is due
        (MORNING_ANNOUNCE_WHEN_EMPTY). true (the default) posts the
        title over "Nothing due today"; false stays silent and clears
        the chat's stored list so the previous day's message stops
        updating.
      '';
    };

    reactionReplies = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Whether action commands answer with a reaction on the command
        message instead of a text reply (REACTION_REPLIES). true (the
        non-default) reacts in group chats — 👍 success, 🤨 soft
        refusal, 👎 failure — with no text fallback; private chats
        always keep text replies and list commands still send their
        messages. false (the default) keeps the historical text
        replies.
      '';
    };

    assigneeMentions = lib.mkOption {
      type = lib.types.attrsOf (lib.types.strMatching "(@[A-Za-z0-9_]+|[0-9]+)");
      default = { };
      example = {
        "Abhinav Nair" = "@abnv";
        "Priya Singh" = "123456789";
      };
      description = ''
        Mapping of Todoist collaborator names to Telegram mentions
        (ASSIGNEE_MENTIONS): values are @usernames or numeric user
        ids. Task lines in /today, /tomorrow, /week and the daily
        reminder show the assignee; mapped names become clickable
        mentions. Names are matched case-insensitively; unmapped
        assignees are shown as plain text. Names and values must each
        be unique (compared case-insensitively) — a duplicate aborts
        startup with exit code 2. Keys must not contain ";" or "=".
        Leave empty (the default) to show plain assignee names only.
        Can also be set via the secrets environment file instead, in
        which case leave this option empty.
      '';
    };

    logLevel = lib.mkOption {
      type = lib.types.enum [
        "DEBUG"
        "INFO"
        "WARNING"
        "ERROR"
      ];
      default = "INFO";
      example = "DEBUG";
      description = ''
        Bot log level (LOG_LEVEL). DEBUG logs each command action and
        Todoist API call with durations, plus numeric chat/user/task
        ids for correlation; it never logs names, usernames, chat
        titles, task text or command arguments. INFO (the default)
        logs only startup, warnings and errors.
      '';
    };

    environmentFile = lib.mkOption {
      type = lib.types.str;
      example = "/run/secrets-rendered/iwcb-bot.env";
      description = ''
        Absolute path to a systemd EnvironmentFile providing
        TELEGRAM_BOT_TOKEN and TODOIST_API_TOKEN, e.g. a sops-nix
        template path. Must be a string, not a Nix path literal —
        path literals are copied into the world-readable Nix store.
        Non-secret settings come from the options above and are
        written to a second environment file listed after this one,
        so the options win even if this file also sets
        TODOIST_PROJECT_ID, ALLOWED_CHAT_IDS or TIMEZONE.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = lib.hasPrefix "/" cfg.environmentFile;
        message = ''services.iwcb-telegram-bot.environmentFile must be an absolute path string, e.g. "/run/secrets-rendered/iwcb-bot.env"'';
      }
      {
        assertion = lib.all (name: !(lib.hasInfix ";" name || lib.hasInfix "=" name)) (
          lib.attrNames cfg.assigneeMentions
        );
        message = ''services.iwcb-telegram-bot.assigneeMentions keys must not contain ";" or "=" (they would break the ASSIGNEE_MENTIONS env encoding)'';
      }
    ];

    # systemd reads EnvironmentFile entries in order and later files
    # override earlier ones (and all files override Environment=), so
    # the generated options file must come last.
    systemd.services.iwcb-telegram-bot = {
      description = "IWCB Telegram Bot";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];

      # Start limits are [Unit] keys (systemd >= 230); under
      # serviceConfig they would land in [Service] and be ignored.
      unitConfig = {
        # Backstop so a persistent failure (e.g. bad bot token, HTTP 401)
        # stops after 5 attempts instead of looping every 5 s forever.
        StartLimitIntervalSec = 300;
        StartLimitBurst = 5;
      };

      serviceConfig = {
        ExecStart = "${cfg.package}/bin/iwcb-telegram-bot";
        EnvironmentFile = [
          cfg.environmentFile
          "${optionsEnvFile}"
        ];
        Environment = [ "PYTHONUNBUFFERED=1" ];

        Restart = "on-failure";
        RestartSec = 5;
        # Exit code 2 = configuration error: never restart on it.
        RestartPreventExitStatus = "2";
        TimeoutStopSec = 30;

        DynamicUser = true;
        UMask = "0077";
        NoNewPrivileges = true;
        CapabilityBoundingSet = "";
        AmbientCapabilities = "";

        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        PrivateDevices = true;
        PrivateMounts = true;
        PrivateIPC = true;
        PrivateUsers = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectKernelLogs = true;
        ProtectControlGroups = true;
        ProtectClock = true;
        ProtectHostname = true;
        ProtectProc = "invisible";
        ProcSubset = "pid";
        RemoveIPC = true;

        RestrictAddressFamilies = [
          "AF_UNIX"
          "AF_INET"
          "AF_INET6"
        ];
        RestrictNamespaces = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;
        MemoryDenyWriteExecute = true;
        SystemCallArchitectures = "native";
        SystemCallFilter = [
          "@system-service"
          "~@privileged"
        ];
        SystemCallErrorNumber = "EPERM";
      };
    };
  };
}
