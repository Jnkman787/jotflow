"""DNS inside a JotFlow container (#234). Run with the service names to expect.

Usage: python - <expect-private names,...> <expect-missing names,...> <public names,...>

- the container has no DNS search domain;
- each expected service name resolves, only to private addresses;
- each missing name fails to resolve, quickly, and never to a public address;
- each public name still resolves, so dns_search doesn't break egress.
"""

import socket
import sys
import time

from jotflow.netcheck import all_private


def addresses(name: str) -> list[str]:
    infos = socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def names(arg: str) -> list[str]:
    return [n for n in arg.split(",") if n]


private, missing, public = (names(a) for a in sys.argv[1:4])

with open("/etc/resolv.conf") as resolv:
    searches = [line.split()[1:] for line in resolv if line.startswith("search")]
assert all(entry in ([], ["."]) for entry in searches), f"a search domain is set: {searches}"

for name in private:
    found = addresses(name)
    assert all_private(found), f"{name} resolves to {found}"
for name in missing:
    started = time.monotonic()
    try:
        found = addresses(name)
    except socket.gaierror:
        found = []
    took = time.monotonic() - started
    assert not found, f"{name} should not resolve, but resolves to {found}"
    assert took < 5, f"{name} took {took:.1f}s to fail"
for name in public:
    assert addresses(name), f"{name} does not resolve"
print("dns ok:", ", ".join(private + missing + public))
