"""Shared test setup.

Compose's service names, such as ``agent`` and ``agent-exec``, don't resolve on a
test machine. The runner client resolves each runner's name and refuses one that
isn't private (#234), so every test sees those names as Docker's DNS would: each
at its own private address. Any other name goes to the real resolver.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

import pytest

from jotflow import netcheck

COMPOSE_DNS = {
    "agent": "172.20.0.2",
    "agent-exec": "172.20.0.3",
    "jotflow": "172.20.0.4",
}


@pytest.fixture(autouse=True)
def compose_dns(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    real = netcheck.SystemResolver.resolve

    async def resolve(self: netcheck.SystemResolver, host: str, port: int) -> Sequence[str]:
        if host in COMPOSE_DNS:
            return [COMPOSE_DNS[host]]
        return await real(self, host, port)

    monkeypatch.setattr(netcheck.SystemResolver, "resolve", resolve)
    yield COMPOSE_DNS


# Unit tests never reach the network: a real connection with a fake token can hang,
# fail, or leak a socket that fails the test that opened it (#406). Loopback, as the
# in-process MCP server and runner stand-ins use, and Unix sockets are allowed.
_LOCAL = ("127.", "::1", "localhost")


def _local(address: object) -> bool:
    if not isinstance(address, tuple):
        return True  # a Unix socket path, or a socketpair
    host = str(address[0])
    return host.startswith(_LOCAL) or host in ("", "0.0.0.0", "::")


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Refuse any connection off this machine, and fail the test that tried, even when
    the code under test catches the error and only logs it, as the card loop does."""
    import socket

    tried: list[str] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def refuse(address: object) -> None:
        tried.append(repr(address))
        raise ConnectionRefusedError(f"a unit test tried to reach {address!r} (#406)")

    def connect(self: socket.socket, address: object) -> None:
        if not _local(address):
            refuse(address)
        real_connect(self, address)  # type: ignore[arg-type]

    def connect_ex(self: socket.socket, address: object) -> int:
        if not _local(address):
            refuse(address)
        return real_connect_ex(self, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    yield
    if tried:
        where = ", ".join(sorted(set(tried))[:3])
        pytest.fail(f"this test tried to reach the network, such as {where}; stub it (#406)")
