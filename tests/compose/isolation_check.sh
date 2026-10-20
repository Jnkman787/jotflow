#!/usr/bin/env bash
# Per-run uid check (#181): the agent image with docker-compose.isolation.yml and
# JOTFLOW_RUNNER_ISOLATION=uid. Two concurrent stand-in runs must each run as a
# pool uid with no capabilities, fail every cross-read, and leave nothing behind.
# Run from the repository root with the agent image built. CI runs it.
set -euo pipefail

compose=(docker compose --project-directory . -f docker/docker-compose.yml -f docker/docker-compose.isolation.yml)

probe_command() {
  python3 - <<'PY'
import base64, json
code = base64.b64encode(open("tests/compose/isolation_probe.py", "rb").read()).decode()
print(json.dumps(["python", "-c", f"import base64;exec(base64.b64decode('{code}'))"], separators=(",", ":")))
PY
}

cleanup() {
  "${compose[@]}" --profile agent down --remove-orphans >/dev/null 2>&1 || true
  rm -f .env agent.env agent-exec.env
}
trap cleanup EXIT

for file in .env agent.env agent-exec.env; do
  if [ -e "$file" ]; then
    echo "refusing to overwrite $file; run this in a clean checkout" >&2
    exit 2
  fi
done

secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
: > .env
: > agent-exec.env
printf 'JOTFLOW_RUNNER_SECRET=%s\nJOTFLOW_AGENT_COMMAND=%s\nJOTFLOW_GITHUB_READ_TOKEN=ci-read\nJOTFLOW_RUNNER_ISOLATION=uid\nJOTFLOW_MCP_URL=http://127.0.0.1:8765/mcp\n' \
  "$secret" "$(probe_command)" > agent.env
chmod 600 .env agent.env agent-exec.env

"${compose[@]}" up -d --no-deps agent
for _ in $(seq 1 30); do
  if "${compose[@]}" exec -T agent python -c 'import socket; socket.create_connection(("127.0.0.1", 8766), 1)' 2>/dev/null; then
    break
  fi
  sleep 1
done

"${compose[@]}" exec -T -e SECRET="$secret" agent python - <<'PY'
import json, os, time, urllib.request

def call(method, path, body=None):
    request = urllib.request.Request(
        "http://127.0.0.1:8766" + path,
        data=None if body is None else json.dumps(body).encode(),
        method=method,
        headers={"Authorization": "Bearer " + os.environ["SECRET"], "Content-Type": "application/json", "Host": "agent:8766"},
    )
    with urllib.request.urlopen(request) as reply:
        return json.loads(reply.read())

runs = ["iso-a", "iso-b"]
for run_id in runs:
    call("POST", "/runs", {"run_id": run_id, "kind": "drafting", "token": "jfr_" + "x" * 43})
deadline = time.monotonic() + 60
views = {}
while time.monotonic() < deadline:
    views = {r: call("GET", f"/runs/{r}") for r in runs}
    if all(v["state"] not in ("starting", "running") for v in views.values()):
        break
    time.sleep(0.5)
time.sleep(1)  # the sweep runs as each run ends
for run_id, view in views.items():
    assert view == {"run_id": run_id, "state": "exited", "exit_code": 0}, view
left = []
for name in os.listdir("/proc"):
    if name.isdigit():
        try:
            for line in open(f"/proc/{name}/status"):
                if line.startswith("Uid:") and 20001 <= int(line.split()[1]) <= 20064:
                    state = open(f"/proc/{name}/stat").read().rsplit(")", 1)[1].split()[0]
                    left.append(f"{name}:{state}:ppid={open(f'/proc/{name}/stat').read().rsplit(')', 1)[1].split()[1]}")
        except OSError:
            pass
assert not left, f"processes of pool uids survived: {left}"
assert not [n for n in os.listdir("/tmp") if n.startswith("jotflow-run-")], os.listdir("/tmp")
print("isolation: two concurrent runs, each its own uid, no cross-reads, nothing left behind")
PY

# The runner itself must refuse uid mode without the override.
refusal="$(mktemp)"
"${compose[@]}" --profile agent down >/dev/null 2>&1 || true
if docker compose --project-directory . -f docker/docker-compose.yml run --rm -T --no-deps agent 2>"$refusal"; then
  echo "the runner started in uid mode without the override" >&2
  exit 1
fi
grep -q "docker-compose.isolation.yml" "$refusal"
echo "isolation: refused without the override, as it should be"
