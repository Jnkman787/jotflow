"""A stand-in agent for the per-run uid check (#181). Exits 0 only if isolation holds.

Run by the runner as a run's agent, with JOTFLOW_RUNNER_ISOLATION=uid. It waits
for its peer run, tries every cross-read the issue names, and leaves a
daemonized child behind for the runner's sweep to find.
"""

import errno
import os
import sys
import time

POOL = range(20001, 20065)
problems = []


def status(pid):
    fields = {}
    with open(f"/proc/{pid}/status") as handle:
        for line in handle:
            key, _, value = line.partition(":")
            fields[key] = value.split()
    return fields


me = status("self")
uid = os.getuid()
if uid not in POOL:
    problems.append(f"runs as uid {uid}, not a pool uid")
for key in ("CapEff", "CapPrm", "CapAmb"):
    if int(me[key][0], 16) != 0:
        problems.append(f"{key} is {me[key][0]}")
if me.get("NoNewPrivs", ["0"])[0] != "1":
    problems.append("NoNewPrivs is off")
if os.getgroups() not in ([], [os.getgid()]):
    problems.append(f"has supplementary groups {os.getgroups()}")


def others():
    found = {"run": [], "root": []}
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == os.getpid():
            continue
        try:
            owner = int(status(name)["Uid"][0])
        except (OSError, KeyError, IndexError):
            continue
        if owner == 0:
            found["root"].append(name)
        elif owner in POOL and owner != uid:
            found["run"].append(name)
    return found


# Wait up to 20 s for the peer run to be alive, so the reads are concurrent.
deadline = time.monotonic() + 20
while not others()["run"] and time.monotonic() < deadline:
    time.sleep(0.2)
seen = others()
if not seen["run"]:
    problems.append("never saw the peer run")
if not seen["root"]:
    problems.append("never saw the runner")


def refused(path, what):
    try:
        if os.path.isdir(path) and not path.endswith("environ"):
            os.listdir(path)
        else:
            with open(path, "rb") as handle:
                handle.read(1)
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EPERM, errno.ENOENT):
            problems.append(f"{what}: unexpected {errno.errorcode.get(exc.errno)}")
        return
    problems.append(f"{what} was readable")


for pid in seen["run"]:
    refused(f"/proc/{pid}/environ", "the peer run's environment")
    refused(f"/proc/{pid}/cwd", "the peer run's directory")
for pid in seen["root"]:
    refused(f"/proc/{pid}/environ", "the runner's environment")
refused("/tmp", "the list of run directories in /tmp")
try:
    with open("/tmp/jotflow-leak", "w") as handle:
        handle.write("x")
    problems.append("could write /tmp")
except OSError:
    pass

# Leave a daemonized child for the sweep: it must not survive the run.
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        time.sleep(600)
    os._exit(0)

time.sleep(2)  # overlap with the peer while it reads too
for line in problems:
    print(line, file=sys.stderr)
sys.exit(1 if problems else 0)
