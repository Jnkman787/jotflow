# JotFlow container image and Compose file

## Links and status

- Issue: #9
- Draft PR: #62
- Status: ready for review
- Last verified: local build and smoke tests on Docker 28.3 and Compose 2.38, 2026-10-18
- Next safe action: review and merge #62; after #8 lands, check the connected state with `docker compose up` against a live workspace

## Objective and done-when

Package JotFlow as a container image and a Compose file that a team can start with one command. This issue builds only the JotFlow container. The agent container arrives in M3 (#54, D6).

Done when:

- [x] The image builds from the locked dependencies, runs as a non-root user, and contains no Node and no agent CLI
- [x] `docker compose run --rm jotflow jotflow --version` prints the version
- [x] The database lives on a named volume and survives `docker compose down` followed by `up`
- [x] The image builds in CI on every pull request, with read-only permissions and no push
- [ ] Deferred to #8: `docker compose up` with a filled `.env` reaches the connected state. This needs `jotflow run` from #8 and a live Slack workspace, so this PR uses `Refs #9`, not `Fixes #9`.

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

- `jotflow run` is a stub until #8 lands, so the default command does nothing useful until then. `--version` is the smoke test meanwhile.
- The MCP server will need `JOTFLOW_MCP_HOST` set to listen on the `agents` network in M3. Today it binds to `127.0.0.1`, which is correct while there is no agent container.
- Named volume ownership relies on Docker copying the mount point's ownership into an empty volume. A bind mount would need the host directory to be writable by the image's uid.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] Dockerfile and .dockerignore
- [x] docker-compose.yml
- [x] CI image build job and Dependabot docker ecosystem
- [x] Verify locally: build, `--version`, non-root, no Node, volume persistence, no secrets in context
- [x] Update documentation: a short image section under Deployment in architecture.md
- [x] `make check` passes
- [x] Mark the PR ready for review

## Validation

Run locally on 2026-10-18 with Docker 28.3.2 and Compose 2.38.2:

- `docker build -t jotflow:local .` succeeds. The image is about 330 MB uncompressed; most of it is the Python base and the dependency tree, which #6 trims by dropping githubkit.
- `docker compose run --rm jotflow jotflow --version` prints `jotflow 0.0.1`.
- `id` in the container reports uid and gid 10001. `node`, `npm`, `uv`, and `gcc` are absent, and so are ruff, pytest, and mypy.
- `slack_bolt`, `mcp`, `aiosqlite`, and `pydantic_settings` import from the installed, non-editable package.
- A file written to `/data` survives `docker compose down` and a new run. `/app` is read-only, `/tmp` is writable.
- With a dummy `.env` and `local.db` in the checkout, a throwaway build that copies the whole context sees only `LICENSE`, `README.md`, `pyproject.toml`, `src/`, and `uv.lock`.
- `make check` passes after rebasing on main with #63, and the image still prints `jotflow 0.0.1`.
- CodeRabbit (local CLI, free allowance): 1 finding, 0 fixed, 1 rejected. It asked to change the default command because `jotflow run` was missing. Since #63 the command exists as a stub that #8 implements, so `jotflow run` is the right default and changing it now would only need reverting.
- The CI `image` job runs the same build, a version check, a uid check, and a check that Node, npm, and uv are absent.
- CI on #62 is green: lint, type check, and tests on Python 3.12 and 3.13, the image build, and the PR title check.

## Handoff notes

- The default command is `jotflow run`. Since #63 it exists as a stub that prints "not implemented yet"; #8 implements it. The last done-when item, reaching the connected state, is checked once #8 lands, against a live workspace.
- In M3 (#54), set `JOTFLOW_MCP_HOST` for the jotflow service so the MCP server listens on the `agents` network, add the service name to its `Host` allowlist, and add the agent service on `agents` plus its own egress network.
- Decision taken here: an allowlist `.dockerignore` (deny everything, allow the five build inputs), so a new secret file can never slip into the context by default.
