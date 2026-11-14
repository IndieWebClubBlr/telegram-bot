# Common development commands. Run `just` to list them.

# List the available recipes.
default:
    @just --list

# Run the bot from src/ with credentials from the environment.
run:
    python -m todoist_bot

# Format Python and Nix sources.
fmt:
    black src tests
    nixfmt *.nix

# Check formatting without changing files.
fmt-check:
    black --check src tests
    nixfmt --check *.nix

# Type-check Python.
typecheck:
    basedpyright --pythonpath "$(command -v python)"

# Run the Python test suite.
test:
    pytest -q

# Lint Python.
lint:
    ruff check --select F401,DTZ --no-cache src tests

# Verify the release version is identical in all three places.
version-check:
    @set -eu; \
    py=$(grep -m1 '^version = ' pyproject.toml | cut -d'"' -f2); \
    nix=$(grep -m1 '^  version = ' default.nix | cut -d'"' -f2); \
    init=$(grep -m1 '__version__' src/todoist_bot/__init__.py | cut -d'"' -f2); \
    test "$py" = "$nix" && test "$py" = "$init" || { \
        echo "version mismatch: pyproject=$py default.nix=$nix __init__=$init" >&2; exit 1; \
    }

# Evaluate the NixOS module (prints true when the instances are wired up).
module-test:
    nix-instantiate --eval --strict ./module-test.nix

# Build the package; pytest runs in the Nix sandbox.
build:
    nix-build

# Smoke-test the built binary: no configuration must exit 2.
smoke:
    #!/usr/bin/env bash
    set -euo pipefail
    status=0
    env -i ./result/bin/todoist-telegram-bot || status=$?
    if [[ $status -ne 2 ]]; then
        echo "expected exit 2, got $status" >&2
        exit 1
    fi
    echo "smoke ok (exit 2)"

# Run the full battery: formatting, types, tests, lint, module eval, build, smoke.
check: fmt-check typecheck test lint version-check module-test build smoke