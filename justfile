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
    nix-shell -p ruff --run 'ruff check --select F401,DTZ --no-cache src tests'

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
check: fmt-check typecheck test lint module-test build smoke