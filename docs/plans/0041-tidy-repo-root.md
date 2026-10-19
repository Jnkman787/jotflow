# Tidy the repository root before v0.1.0

## Links and status

- Issue: #41
- Draft PR: to be opened with this plan
- Status: implementing
- Last verified: origin/main f70e473, 2026-10-19
- Next safe action: `git mv` the files, then fix each reference

## Objective and done-when

Paths become public links once v0.1.0 ships, so the maintainer wants the repository root tidied first.

Done when:

- [ ] `Dockerfile`, `Dockerfile.agent`, `docker-compose.yml` and `docker-compose.policy.yml` are in `docker/`.
- [ ] `jotflow.example.toml`, `agent.env.example` and `agent-exec.env.example` are in `examples/`.
- [ ] `ci/` is in `.github/ci/`.
- [ ] Each move uses `git mv`, so history follows.
- [ ] The root keeps README, LICENSE, CHANGELOG, CONTRIBUTING, SECURITY, AGENTS.md, CLAUDE.md, pyproject.toml, uv.lock, .python-version, the Makefile and the dotfiles.
- [ ] Every reference is updated:
  - Compose build contexts and Dockerfile paths.
  - ci.yml, including the Codex gate, and release.yml, including its compose-pin read.
  - The Makefile, and `.dockerignore`, which must still send the same files to each build.
  - The template drift tests, the pin tests, setup's checkout detection, and every reader of the example toml.
  - Every doc link, including the README's full GitHub URL to the example toml.
- [ ] A checkout still starts with one documented command.
- [ ] Proven by `make check`, both image builds, the release dry run in CI, and a grep with no stale root paths.

**Out of scope:**
- Restructuring the README beyond its paths. Product splits it after this lands.
- A team's own deployment folder. Setup still writes `docker-compose.yml`, `docker-compose.policy.yml`, `.env` and the agent env files side by side there, so docs about that folder keep those bare names.

## Design

**Two places, one text.** The package's templates must stay byte-identical to the checkout's compose and env files, and a test enforces that. A team's folder holds the compose files next to `.env`, `agent.env` and `jotflow.toml`. So the compose files keep those paths relative to the project directory, and only the `build:` blocks change. `published_compose` strips the build blocks for a team anyway.
- `jotflow` builds with `context: .` and `dockerfile: docker/Dockerfile`.
- Both agent services build with `dockerfile: docker/Dockerfile.agent`.

**Starting from a checkout.** Compose sets the project directory to the first compose file's folder, which would now be `docker/`. That would look for `.env`, `agent.env` and `jotflow.toml` there. A local test showed that `COMPOSE_FILE` alone can't change this, and neither can a `COMPOSE_PROJECT_DIRECTORY` variable. Only `--project-directory` can. So:
- The Makefile gets `COMPOSE = docker compose --project-directory . -f docker/docker-compose.yml -f docker/docker-compose.policy.yml`, plus `make up` for `$(COMPOSE) up -d` and `make compose ARGS="…"` for any other command.
- CONTRIBUTING documents `make up`, and the full command for anything else.
- In a checkout, setup no longer writes `COMPOSE_FILE` to `.env`, since root files with that name are gone. Its last step says `make up` instead of `docker compose up -d`. Outside a checkout nothing changes.

**Checkout detection.** `in_checkout` looks for `docker/Dockerfile`, with the `pyproject.toml` name check kept as the fallback.

**Agent env examples.** `_example` read `agent.env.example` from beside the file in a checkout. It now reads the packaged copy only. The drift test keeps that copy identical to `examples/`.

**The rest:**
- `deploy.py`'s docstring changes, and so does the template test, which compares the templates with `docker/` and `examples/`. `make templates` copies from there.
- ci.yml runs Compose with `--project-directory .` and the `docker/` files, builds with `--file docker/Dockerfile.agent`, and runs the gate's `npm ci` in `.github/ci/codex`.
- release.yml reads the pin from `docker/docker-compose.yml` and builds with the moved Dockerfiles.
- `.dockerignore` stays at the root, which is still the build context, so the same files are sent. BuildKit would prefer a `docker/Dockerfile.dockerignore`, and none is added.
- User-facing text that names the example toml points to `examples/jotflow.example.toml`.

## Risks and open questions

- **A contributor's existing `.env`** may hold `COMPOSE_FILE=docker-compose.yml:…`. `make up` passes `-f`, which overrides it. Plain `docker compose up` in a checkout then fails with a missing file, and CONTRIBUTING says to use `make up`.
- **Dependabot or other config** may name `ci/codex`. A grep of every tracked file found none.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [ ] Commit this plan and open the draft PR
- [ ] `git mv` the files
- [ ] Fix the compose build blocks, the Makefile, ci.yml and release.yml
- [ ] Fix setup, deploy.py and the tests
- [ ] Fix the doc links
- [ ] `make check`, both image builds, `make up` config, and the stale-path grep
- [ ] Mark the PR ready for review

## Validation

## Handoff notes
