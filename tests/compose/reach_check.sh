#!/usr/bin/env bash
# Cross-container run check (#54, #55): JotFlow launches a stub run in each
# agent container, and each stub reaches JotFlow's MCP server with its token.
# Run from the repository root with both images built. CI runs it; so can you.
set -euo pipefail

stub_command() {
  python3 - "$1" <<'PY'
import base64, json, sys
code = base64.b64encode(open("tests/compose/stub_agent.py", "rb").read()).decode()
run = f"import base64;exec(base64.b64decode('{code}'))"
print(json.dumps(["python", "-c", run, "{mcp_config}", sys.argv[1]], separators=(",", ":")))
PY
}

new_secret() { python3 -c 'import secrets; print(secrets.token_urlsafe(32))'; }

cleanup() {
  docker compose --project-directory . -f docker/docker-compose.yml --profile agent down --remove-orphans >/dev/null 2>&1 || true
  rm -f .env agent.env agent-exec.env
}
trap cleanup EXIT

for file in .env agent.env agent-exec.env; do
  if [ -e "$file" ]; then
    echo "refusing to overwrite $file; run this in a clean checkout" >&2
    exit 2
  fi
done

drafting_secret="$(new_secret)"
exec_secret="$(new_secret)"
: > .env
printf 'JOTFLOW_RUNNER_SECRET=%s\nJOTFLOW_AGENT_COMMAND=%s\nJOTFLOW_GITHUB_READ_TOKEN=ci-read\n' \
  "$drafting_secret" "$(stub_command drafting)" > agent.env
printf 'JOTFLOW_RUNNER_SECRET=%s\nJOTFLOW_AGENT_COMMAND=%s\nJOTFLOW_GITHUB_WRITE_TOKEN=ci-write\n' \
  "$exec_secret" "$(stub_command execution)" > agent-exec.env
chmod 600 .env agent.env agent-exec.env

docker compose --project-directory . -f docker/docker-compose.yml up -d --no-deps agent agent-exec
for service in agent agent-exec; do
  for _ in $(seq 1 30); do
    if docker compose --project-directory . -f docker/docker-compose.yml exec -T "$service" python -c 'import socket; socket.create_connection(("127.0.0.1", 8766), 1)' 2>/dev/null; then
      break
    fi
    sleep 1
  done
done

# JotFlow's side runs in its own image, on its own networks, reachable as
# "jotflow" (--use-aliases), with no Slack.
docker compose --project-directory . -f docker/docker-compose.yml run --rm -T --no-deps --use-aliases \
  -e JOTFLOW_RUNNER_SECRET="$drafting_secret" \
  -e JOTFLOW_RUNNER_EXEC_SECRET="$exec_secret" \
  jotflow python - < tests/compose/reach_probe.py

# DNS (#234): no search domain anywhere, service names resolve privately, a name
# that isn't running fails fast, and public names still resolve for egress.
docker compose --project-directory . -f docker/docker-compose.yml run --rm -T --no-deps --use-aliases \
  jotflow python - agent,agent-exec not-a-jotflow-service slack.com < tests/compose/dns_probe.py
for service in agent agent-exec; do
  docker compose --project-directory . -f docker/docker-compose.yml exec -T "$service" \
    python - "" jotflow,not-a-jotflow-service github.com,api.anthropic.com,api.openai.com \
    < tests/compose/dns_probe.py
done
echo "cross-container runs ok"
