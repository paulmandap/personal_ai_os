"""Unit-test guarantees.

The unit suite must pass with Ollama stopped and the network unplugged. That
is not a nice-to-have: it is the evidence that the architecture -- not the
model -- owns the workflow. A test that quietly reaches a live server would
erode that claim without anyone noticing, so the claim is enforced here rather
than trusted.
"""

from __future__ import annotations

import socket

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if a unit test opens a real socket."""

    def blocked(self, address, *args, **kwargs):
        raise AssertionError(
            f"unit test attempted a real network connection to {address!r}. "
            "Use httpx.MockTransport or ScriptedModel instead -- the unit "
            "suite must pass with Ollama stopped."
        )

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
