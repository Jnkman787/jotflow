"""Unit tests never reach the network (#406): the conftest guard's address rule."""

from __future__ import annotations

import socket

import pytest

from tests.conftest import _local


@pytest.mark.parametrize(
    "address", [("127.0.0.1", 8766), ("::1", 443, 0, 0), ("localhost", 80), "/tmp/agent.sock"]
)
def test_this_machine_is_allowed(address: object) -> None:
    assert _local(address)


@pytest.mark.parametrize("address", [("3.134.44.134", 443), ("2600:1f18::1", 443, 0, 0)])
def test_anything_else_is_refused(address: object) -> None:
    assert not _local(address)


def test_a_loopback_connection_still_works() -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.socket()
    try:
        client.connect(server.getsockname())
    finally:
        client.close()
        server.close()
