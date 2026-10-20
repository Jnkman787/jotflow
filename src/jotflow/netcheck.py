"""Only send a secret to a private address (#234).

JotFlow's runner calls carry the runner secret, and ``POST /runs`` carries a run
token. The agent sends its run token back to JotFlow's MCP server. Every one of
those names is a Compose service name, such as ``agent-exec`` or ``jotflow``.
When that service isn't running, Docker's DNS doesn't know the name. The lookup
then falls back to the host's search domain, and a resolver that answers every
unknown name can point it at a public host.

So a name must resolve only to addresses in :data:`PRIVATE_NETWORKS` before a
secret goes to it. One public address in the answer refuses the whole lookup.
An operator who really runs a component elsewhere opts in explicitly.

The allowlist is explicit rather than :attr:`ipaddress.IPv4Address.is_private`,
which counts some documentation and benchmark ranges but not shared address
space. 100.64.0.0/10 is included because VPNs such as Tailscale put their nodes
there, and a VPN is how the docs suggest reaching a remote runner.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable, Sequence
from typing import Protocol

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

PRIVATE_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),  # shared address space: CGNAT, Tailscale
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


class AddressRefused(Exception):
    """A name resolved to a public address, or not at all. The message says which.

    ``unresolved`` is True when the name didn't resolve, which may pass once a
    service starts. A public answer is never retried.
    """

    def __init__(self, reason: str, *, unresolved: bool = False) -> None:
        super().__init__(reason)
        self.unresolved = unresolved


class Resolver(Protocol):
    async def resolve(self, host: str, port: int) -> Sequence[str]:
        """Every address ``host`` resolves to, as strings. Raises OSError if none."""
        ...


class SystemResolver:
    """The process's own resolver, as an HTTP client would use it."""

    async def resolve(self, host: str, port: int) -> Sequence[str]:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return [str(info[4][0]) for info in infos]


def _address(value: str) -> IPAddress:
    address = ipaddress.ip_address(value.split("%", 1)[0])  # drop an IPv6 zone id
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def is_private(value: str) -> bool:
    """Whether one address is in :data:`PRIVATE_NETWORKS`."""
    address = _address(value)
    return any(address.version == net.version and address in net for net in PRIVATE_NETWORKS)


def all_private(values: Iterable[str]) -> bool:
    """True only if there is at least one address and every one is private."""
    found = list(values)
    return bool(found) and all(is_private(v) for v in found)


def is_literal(host: str) -> bool:
    try:
        _address(host.strip("[]"))
    except ValueError:
        return False
    return True


async def checked_address(
    resolver: Resolver, host: str, port: int, *, allow_public: bool, opt_in: str
) -> str:
    """The address to dial for ``host``: resolved once, and private unless allowed.

    Raises :class:`AddressRefused` with a fixed reason naming ``host``, never the
    addresses, which a stranger's resolver chose.
    """
    bare = host.strip("[]")
    if is_literal(bare):
        addresses: Sequence[str] = [bare]
    else:
        try:
            addresses = await resolver.resolve(bare, port)
        except (OSError, UnicodeError):
            raise AddressRefused(
                f"{host} does not resolve; is that service running?", unresolved=True
            ) from None
        if not addresses:
            raise AddressRefused(
                f"{host} does not resolve; is that service running?", unresolved=True
            )
    if not allow_public and not all_private(addresses):
        raise AddressRefused(
            f"{host} resolves to a public address, so JotFlow won't send it a secret. "
            f"If it really runs elsewhere, set {opt_in}=1"
        )
    return str(_address(addresses[0]))
