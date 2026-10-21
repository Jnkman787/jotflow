"""The codex gate's reading of strace's output (#290). Runs everywhere: no strace needed."""

# The fixtures are real strace lines, kept whole.
# ruff: noqa: E501

from __future__ import annotations

from pathlib import Path

from tests.outbound_trace import refused_contacts, strace_argv, trace_lines

API, MCP = 38999, 57045
MODEL = 'connect(36, {sa_family=AF_INET, sin_port=htons(38999), sin_addr=inet_addr("127.0.0.1")}, 16) = -1 EINPROGRESS (Operation now in progress)'
JOTFLOW = 'connect(39, {sa_family=AF_INET, sin_port=htons(57045), sin_addr=inet_addr("127.0.0.1")}, 16) = -1 EINPROGRESS (Operation now in progress)'
NSCD_MISSING = 'connect(3, {sa_family=AF_UNIX, sun_path="/var/run/nscd/socket"}, 110) = -1 ENOENT (No such file or directory)'
NETLINK = "sendto(4, [{nlmsg_len=32, nlmsg_type=RTM_GETADDR, nlmsg_flags=NLM_F_REQUEST|NLM_F_DUMP, nlmsg_seq=1, nlmsg_pid=0}, {ifa_family=AF_UNSPEC}], 32, 0, {sa_family=AF_NETLINK, nl_pid=0, nl_groups=00000000}, 12) = 32"
CONNECTED = 'sendto(36, "POST /v1/responses HTTP/1.1"..., 512, MSG_NOSIGNAL, NULL, 0) = 512'
UNSPEC = 'connect(5, {sa_family=AF_UNSPEC, sa_data=""}, 16) = 0'


IO_URING_OFF = "io_uring_setup(256, {flags=IORING_SETUP_NO_SQARRAY, sq_thread_cpu=0}, 0x7f) = -1 EPERM (Operation not permitted)"


def test_the_model_jotflow_and_local_plumbing_are_allowed() -> None:
    lines = [
        MODEL,
        JOTFLOW,
        NSCD_MISSING,
        NETLINK,
        CONNECTED,
        UNSPEC,
        'connect(3, {sa_family=AF_UNIX, sun_path="/run/x.sock"}, 110) = -1 ECONNREFUSED (Connection refused)',
        IO_URING_OFF,  # the kernel refused it, so Codex falls back to plain sockets
        f"[4242 codex] {IO_URING_OFF}",
        "+++ exited with 0 +++",
    ]
    assert refused_contacts(lines, {API, MCP}) == []


def test_every_other_contact_is_refused() -> None:
    other = [
        # A real host, by IPv4 and IPv6.
        'connect(7, {sa_family=AF_INET, sin_port=htons(443), sin_addr=inet_addr("104.18.33.45")}, 16) = -1 EINPROGRESS',
        'connect(7, {sa_family=AF_INET6, sin6_port=htons(443), sin6_flowinfo=htonl(0), inet_pton(AF_INET6, "2606:4700::6812:212d", &sin6_addr), sin6_scope_id=0}, 28) = -1 EINPROGRESS',
        # DNS, to a stub resolver on loopback or a real one.
        'connect(5, {sa_family=AF_INET, sin_port=htons(53), sin_addr=inet_addr("127.0.0.53")}, 16) = 0',
        'sendto(5, "\\x12\\x34"..., 40, MSG_NOSIGNAL, {sa_family=AF_INET, sin_port=htons(53), sin_addr=inet_addr("10.255.255.254")}, 16) = 40',
        # Loopback, but not one of the allowed ports.
        'connect(8, {sa_family=AF_INET, sin_port=htons(8080), sin_addr=inet_addr("127.0.0.1")}, 16) = 0',
        'connect(8, {sa_family=AF_INET6, sin6_port=htons(38999), sin6_flowinfo=htonl(0), inet_pton(AF_INET6, "fe80::1", &sin6_addr), sin6_scope_id=0}, 28) = 0',
        # A wildcard, which reaches the host's own loopback on Linux.
        'connect(8, {sa_family=AF_INET, sin_port=htons(38999), sin_addr=inet_addr("0.0.0.0")}, 16) = 0',
        'connect(8, {sa_family=AF_INET6, sin6_port=htons(38999), sin6_flowinfo=htonl(0), inet_pton(AF_INET6, "::", &sin6_addr), sin6_scope_id=0}, 28) = 0',
        # Loopback by another 127/8 address.
        'connect(8, {sa_family=AF_INET, sin_port=htons(38999), sin_addr=inet_addr("127.0.0.2")}, 16) = 0',
        # A lookup service that answered, which would hide the host asked for, and an
        # abstract socket that answered.
        'connect(3, {sa_family=AF_UNIX, sun_path=@"/tmp/dbus-AbC"}, 20) = 0',
        # A listener with a full backlog is still a listener.
        'connect(3, {sa_family=AF_UNIX, sun_path="/var/run/nscd/socket"}, 110) = -1 EAGAIN (Resource temporarily unavailable)',
        'connect(3, {sa_family=AF_UNIX, sun_path="/run/systemd/resolve/io.systemd.Resolve"}, 110) = 0',
        # A family nobody expects, and io_uring, which hides its connects.
        "connect(9, {sa_family=AF_PACKET, sll_protocol=htons(ETH_P_ALL)}, 20) = 0",
        "io_uring_setup(8, 0x7ffd0c8a8f10) = 5",
        "[4242 codex] io_uring_setup(256, {flags=IORING_SETUP_NO_SQARRAY, sq_thread_cpu=0}) = 11",
    ]
    assert refused_contacts(other, {API, MCP}) == other


def test_a_line_with_no_parsable_address_is_refused() -> None:
    odd = "connect(7, {sa_family=AF_INET, sin_port=htons(443), ...}, 16) = 0"
    assert refused_contacts([odd], {443}) == [odd]


def test_trace_files_are_read_per_process(tmp_path: Path) -> None:
    prefix = tmp_path / "trace"
    (tmp_path / "trace.101").write_text(
        'execve("/usr/local/bin/codex", ["codex"], 0x7ffd /* 9 vars */) = 0\n' + MODEL + "\n"
    )
    (tmp_path / "trace.202").write_text(NSCD_MISSING + "\n")
    (tmp_path / "unrelated").write_text("connect(...)\n")
    lines = trace_lines(prefix)
    assert lines[1:] == [f"[101 codex] {MODEL}", f"[202 ?] {NSCD_MISSING}"]
    # Labelled lines read the same, and a refusal names the process that made it.
    leak = 'connect(7, {sa_family=AF_INET, sin_port=htons(443), sin_addr=inet_addr("1.1.1.1")}, 16) = 0'
    assert refused_contacts([*lines, f"[303 node] {leak}"], {API}) == [f"[303 node] {leak}"]
    argv = strace_argv("/usr/bin/strace", prefix)
    assert argv[:3] == ["/usr/bin/strace", "-f", "-ff"] and argv[-2:] == ["-o", str(prefix)]


def test_the_listener_reads_a_query_name_and_refuses_it() -> None:
    from tests.codex_startup_trace import REFUSED, qname, refusal

    query = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x03api\x06openai\x03com\x00\x00\x01\x00\x01"
    assert qname(query) == "api.openai.com"
    answer = refusal(query)
    assert answer[:2] == b"\x12\x34" and answer[3] & 0x0F == REFUSED and answer[2] & 0x80
    assert answer[12:] == query[12:]
    assert qname(b"\x00" * 5) is None and refusal(b"short") == b""
    assert qname(b"\x00" * 12 + b"\xc0\x0c") is None
