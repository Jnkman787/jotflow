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
