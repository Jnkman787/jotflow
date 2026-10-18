# JotFlow container image and Compose file

## Links and status

- Issue: #9
- Draft PR: pending
- Status: planning
- Last verified: plan written, nothing built yet
- Next safe action: commit this plan, push, open the draft PR

## Objective and done-when

Package JotFlow as a container image and a Compose file that a team can start with one command. This issue builds only the JotFlow container. The agent container arrives in M3 (#54, D6).

Done when:

- [ ] The image builds from the locked dependencies, runs as a non-root user, and contains no Node and no agent CLI
- [ ] `docker compose run --rm jotflow jotflow --version` prints the version
- [ ] The database lives on a named volume and survives `docker compose down` followed by `up`
- [ ] The image builds in CI on every pull request, with read-only permissions and no push
- [ ] `docker compose up` with a filled `.env` reaches the connected state. This needs `jotflow run` from #8 and a live Slack workspace, so this PR uses `Refs #9`, not `Fixes #9`.

**Out of scope:** the agent container and runner (#54), publishing the image to GHCR (#39), `jotflow run` itself (#8), and any change to `config.py`, `models.py`, or `cli.py`.

## Design

See the Deployment section of [architecture.md](../architecture.md) and decision D6 in [roadmap.md](../roadmap.md).

**Dockerfile**, two stages on the same pinned `python:3.12-slim` base so the virtual environment's interpreter path matches:

- Builder: copies `uv` from its pinned official image, installs dependencies from `uv.lock` with `--locked --no-dev` before copying the source so the layer caches, then installs the project non-editable.
- Runtime: copies only `/app/.venv`. Creates an unprivileged `jotflow` user with a fixed uid, and a `/data` directory it owns. No uv, no Node, no build tools.
- Environment sets `JOTFLOW_DATABASE_PATH=/data/jotflow.db`, so no config change is needed.
- Base images are pinned by tag and digest. Dependabot's `docker` ecosystem keeps them current.

**.dockerignore** excludes `.env` and its variants, `*.db` and `*.sqlite`, `.git`, `.venv`, caches, and agent settings, so secrets and local state can never enter the build context.

**docker-compose.yml**, one `jotflow` service:

- `env_file: .env`, marked optional so `--version` and CI work without one; settings validation still rejects a real start without tokens.
- A named volume `jotflow-data` mounted at `/data`.
- No published ports.
- Hardening: read-only root filesystem, a `tmpfs` for `/tmp`, all capabilities dropped, `no-new-privileges`.
- Two networks. `egress` lets JotFlow reach Slack. `agents` is `internal: true`, with no route out, and is where the M3 agent service and its runner join. Adding the agent later is one new service block plus the MCP host setting.

**CI**: a `docker` job in `ci.yml` that builds the image with plain `docker build`, runs `jotflow --version` in it, and validates the Compose file. It inherits the workflow's `contents: read` and pushes nothing.

Alternatives considered: `docker/build-push-action` would add a third-party action and a token surface for no benefit while nothing is pushed. A single-stage image would ship uv and build caches.

## Risks and open questions

- `jotflow run` does not exist until #8 lands, so the default command fails until then. `--version` is the smoke test meanwhile.
- The MCP server will need `JOTFLOW_MCP_HOST` set to listen on the `agents` network in M3. Today it binds to `127.0.0.1`, which is correct while there is no agent container.
- Named volume ownership relies on Docker copying the mount point's ownership into an empty volume. A bind mount would need the host directory to be writable by the image's uid.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [ ] Commit this plan and open the draft PR
- [ ] Dockerfile and .dockerignore
- [ ] docker-compose.yml
- [ ] CI image build job and Dependabot docker ecosystem
- [ ] Verify locally: build, `--version`, non-root, no Node, volume persistence, no secrets in context
- [ ] Update documentation
- [ ] `make check` passes
- [ ] Mark the PR ready for review

## Validation

To be filled in.

## Handoff notes

None yet.
