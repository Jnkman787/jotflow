"""Every network contact a traced process tree tried, from strace's output (#290).

The codex gate runs Codex under ``strace -f -ff`` and fails if any contact
reaches anything but the fake model and JotFlow's MCP server on loopback. It
checks every attempt, refused or not, so a fetch that fails quietly and falls
back still fails the gate. Blocking the network alone wouldn't show that.

What counts, per traced call that names an address (connect, sendto, sendmsg):

- IPv4 or IPv6 loopback, on one of the allowed ports: allowed
- netlink: allowed, since it's the kernel, as getifaddrs asks
- AF_UNSPEC: allowed, since it only dissolves a datagram socket's association
- a Unix socket that refused the connection: allowed, as glibc tries nscd first
  and then does any lookup itself, where it shows up as an address. One that
  connects is refused, since a lookup service such as nscd or systemd-resolved
  would hide which host was asked for
- anything else, a DNS server included: refused

``io_uring_setup`` is refused too, since a connect submitted through io_uring
never shows up as a system call.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

# The system calls strace must trace for :func:`contacts` to see everything.
TRACED = "trace=connect,sendto,sendmsg,sendmmsg,io_uring_setup,execve"

_FAMILY = re.compile(r"sa_family=(AF_[A-Z0-9]+)")
_PORT = re.compile(r"sin6?_port=htons\((\d+)\)")
_V4 = re.compile(r'sin_addr=inet_addr\("([0-9.]+)"\)')
_V6 = re.compile(r'inet_pton\(AF_INET6, "([0-9a-fA-F:.]+)"')
_RESULT = re.compile(r"\)\s*=\s*(-?\d+)")
# Exactly these, so a wildcard such as 0.0.0.0 or ::, which reaches the host's own
# loopback on Linux, is refused too.
LOOPBACK = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
_CALL = re.compile(r"^\[\d+ [^\]]*\] ")


def strace_argv(strace: str, prefix: Path) -> list[str]:
    """The strace command that writes one trace file per process, under ``prefix``."""
    return [strace, "-f", "-ff", "-qq", "-s", "64", "-e", TRACED, "-o", str(prefix)]


def trace_lines(prefix: Path) -> list[str]:
    """Every line strace wrote for the process tree, one file per process.

    Each line is prefixed with the process's id and program, as in
    ``[4242 codex] connect(...)``, so a failure says which process made the contact.
    """
    lines: list[str] = []
    for path in sorted(prefix.parent.glob(prefix.name + ".*")):
        pid = path.name.rsplit(".", 1)[1]
        program = _program(path)
        lines += [
            f"[{pid} {program}] {line}" for line in path.read_text(errors="replace").splitlines()
        ]
    return lines


def _program(trace: Path) -> str:
    """The program a trace file's process ran, from its execve. A thread has none: "?"."""
    for line in trace.read_text(errors="replace").splitlines():
        if line.startswith("execve("):
            name = line.split('"', 2)[1] if '"' in line else "?"
            return Path(name).name
    return "?"


def refused_contacts(lines: Iterable[str], allowed_ports: Iterable[int]) -> list[str]:
    """The traced calls that reached anything but loopback on ``allowed_ports``."""
    ports = set(allowed_ports)
    refused: list[str] = []
    for line in lines:
        call = _CALL.sub("", line, count=1)
        if call.startswith("io_uring_setup("):
            refused.append(line)
            continue
        family = _FAMILY.search(line)
        if family is None:
            continue  # a call on a connected socket, which names no address
        kind = family.group(1)
        if kind in ("AF_NETLINK", "AF_UNSPEC"):
            continue
        if kind == "AF_UNIX":
            result = _RESULT.search(line)
            if result is not None and int(result.group(1)) < 0:
                continue  # refused: nothing behind it, so any lookup is done in-process
            refused.append(line)
            continue
        if kind in ("AF_INET", "AF_INET6"):
            port = _PORT.search(line)
            address = (_V4 if kind == "AF_INET" else _V6).search(line)
            if (
                port is not None
                and address is not None
                and address.group(1) in LOOPBACK
                and int(port.group(1)) in ports
            ):
                continue
        refused.append(line)
    return refused
