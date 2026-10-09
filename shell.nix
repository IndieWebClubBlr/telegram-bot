{
  pkgs ? import <nixpkgs> { },
}:

let
  pythonEnv = pkgs.python3.withPackages (ps: [
    ps.python-telegram-bot
    ps.todoist-api-python
    ps.markdown-it-py
    ps.linkify-it-py
    ps.pytest
  ]);
in
pkgs.mkShell {
  packages = [
    pythonEnv
    pkgs.black
    pkgs.basedpyright
    pkgs.nixfmt
  ];

  shellHook = ''
    export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
    export PYTHONUNBUFFERED=1
  '';
}
