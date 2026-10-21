"""Codex exactly as the preset starts it, offline, and every host name it asks for (#290).

Run by the codex gate inside ``unshare -rnm``, after ``/etc/nsswitch.conf`` and
``/etc/resolv.conf`` are bind-mounted so that every lookup is plain DNS to
127.0.0.1. A listener there records each name asked for and answers REFUSED, so
nothing resolves and nothing leaves the namespace. Codex runs under strace with
the preset's own config and no override but those given on the command line.

Prints one JSON object: the names asked for, and the traced contacts that went
anywhere but the listener.

Usage: python -m tests.codex_startup_trace <strace> <workdir> [codex -c override ...]
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path

from jotflow.adapters.presets import CODEX, RunContext
from jotflow.adapters.runner import write_private
from jotflow.models import RunKind
from tests.outbound_trace import refused_contacts, strace_argv, trace_lines
from tests.test_codex_gate import _fake_github

DNS_PORT = 53
REFUSED = 5


def qname(packet: bytes) -> str | None:
    """The first question's name in a DNS query, or None if it can't be read."""
    labels: list[str] = []
    at = 12
    try:
        while packet[at]:
            size = packet[at]
            if size & 0xC0:
                return None  # a query doesn't compress its question
            labels.append(packet[at + 1 : at + 1 + size].decode("ascii", "replace"))
            at += 1 + size
    except IndexError:
        return None
    return ".".join(labels).lower()


def refusal(packet: bytes) -> bytes:
    """A REFUSED answer to ``packet``, with its question echoed."""
    if len(packet) < 12:
        return b""
    flags = struct.unpack("!H", packet[2:4])[0]
    answer_flags = 0x8000 | (flags & 0x7900) | REFUSED  # QR, opcode and RD kept
    return packet[:2] + struct.pack("!H", answer_flags) + packet[4:6] + b"\0" * 6 + packet[12:]


class Listener:
    """Records every name asked for over UDP or TCP on 127.0.0.1:53, refusing each."""

    def __init__(self) -> None:
        self.names: list[str] = []
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.bind(("127.0.0.1", DNS_PORT))
        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._tcp.bind(("127.0.0.1", DNS_PORT))
        self._tcp.listen()
        for target in (self._serve_udp, self._serve_tcp):
            threading.Thread(target=target, daemon=True).start()

    def _record(self, packet: bytes) -> bytes:
        name = qname(packet)
        self.names.append(name if name is not None else "<unreadable>")
        return refusal(packet)

    def _serve_udp(self) -> None:
        while True:
            packet, peer = self._udp.recvfrom(4096)
            self._udp.sendto(self._record(packet), peer)

    def _serve_tcp(self) -> None:
        while True:
            conn, _ = self._tcp.accept()
            with conn:
                size = conn.recv(2)
                if len(size) == 2:
                    packet = conn.recv(struct.unpack("!H", size)[0])
                    answer = self._record(packet)
                    conn.sendall(struct.pack("!H", len(answer)) + answer)


def main(argv: list[str]) -> int:
    strace, workdir, overrides = argv[0], Path(argv[1]), argv[2:]
    listener = Listener()
    run_dir = workdir / "run"
    run_dir.mkdir(mode=0o700)
    github = _fake_github(workdir)
    context = RunContext(
        kind=RunKind.DRAFTING,
        run_id="run-gate",
        run_dir=run_dir,
        config_path=run_dir / "mcp.json",
        # By address, as the gate's other runs do: the deployed name, jotflow, would be
        # one more lookup, and JotFlow isn't under test here.
        mcp_url="http://127.0.0.1:9/mcp",
        mcp_token="jfr_gate",
        github_token="github_pat_gate",
        github_mcp=str(github),
        model="gpt-5.5",
    )
    plan = CODEX.plan(context)
    for relative, text in plan.files.items():
        write_private(run_dir, relative, text)
    prefix = workdir / "trace" / "codex"
    prefix.parent.mkdir()
    # JotFlow's server isn't under test, and it's required: unreachable here, it would
    # stop Codex before the model is asked for.
    skip = ["-c", "mcp_servers.jotflow.enabled=false"]
    command = [*strace_argv(strace, prefix), *plan.argv[:2], *skip, *overrides, *plan.argv[2:]]
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(workdir / "home"),
        "TMPDIR": str(run_dir),
        "CODEX_API_KEY": "sk-gate-not-a-key",
        **plan.env,
    }
    # Offline, Codex retries the model until it gives up; what it asked is recorded.
    with contextlib.suppress(subprocess.TimeoutExpired):
        subprocess.run(
            command, env=env, cwd=run_dir, stdin=subprocess.DEVNULL, capture_output=True, timeout=20
        )
    print(
        json.dumps(
            {
                "names": sorted(set(listener.names)),
                "refused": refused_contacts(trace_lines(prefix), {DNS_PORT}),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
