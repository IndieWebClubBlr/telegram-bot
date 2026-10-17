"""Autouse fixtures for the test suite.

The fakes and builders live in tests/helpers.py; this module only holds
the pytest fixtures that must be auto-discovered.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest

from iwcb_bot import messaging


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fail loudly on any accidental real outbound connection."""

    def _blocked(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("network access is disabled in command tests")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    yield


@pytest.fixture(autouse=True)
def _fresh_chat_locks() -> Iterator[None]:
    """Each test runs in its own event loop (asyncio.run); asyncio.Lock
    binds to a loop on first await, so stale locks must not leak across
    tests."""
    messaging._chat_locks.clear()
    yield
