# Contributing to JotFlow

Thanks for your interest. JotFlow is early, so design feedback on [docs/architecture.md](docs/architecture.md) and [docs/roadmap.md](docs/roadmap.md) is as useful as code.

The full working rules, for people and coding agents alike, are in [AGENTS.md](AGENTS.md). This page is the short version.

## How work flows

1. **Find or open an issue.** Search open issues first; most planned work already has one, attached to a milestone and its epic. Open a new issue only for work nobody owns yet. Titles take a type prefix such as `feat(slack): ...` or `fix: ...`.
2. **Branch and plan.** Create a branch like `feat/short-topic`. For anything beyond a small fix, copy [docs/plans/TEMPLATE.md](docs/plans/TEMPLATE.md) to `docs/plans/NNNN-short-topic.md`, where `NNNN` is the issue number padded to four digits, for example `docs/plans/0007-jotflow-init.md` for #7. A plan that serves several issues takes the number of the one it closes or mainly serves, and lists the others on its "Also serves" line. Fill it in and commit it before writing code.
3. **Open a draft PR early.** Link the issue with `Fixes #N` if the PR completes it, or `Refs #N` if it is partial work.
4. **Keep the plan current** as you go, then mark the PR ready for review.

## Run from a checkout

The README's quickstart installs JotFlow's published package and images, with no clone. To change JotFlow, or to run a commit that isn't released yet, work from a checkout instead.

- **Git**, from <https://git-scm.com/downloads>, and **uv**, from [uv's installation guide](https://docs.astral.sh/uv/getting-started/installation/).
- **Docker with BuildKit**, which builds JotFlow's images from source. Docker Desktop and Docker's own packages include it. On Ubuntu with the `docker.io` package, also install `docker-buildx` and `docker-compose-v2` with `sudo apt install docker-buildx docker-compose-v2`. Check with `docker buildx version`.

```
git clone https://github.com/Jnkman787/jotflow.git
cd jotflow
uv run jotflow init
docker compose up -d
```

In a checkout, setup leaves the checkout's own Compose files alone, and `docker compose up -d` builds both images from this source. Set `JOTFLOW_IMAGE_REF` and `JOTFLOW_AGENT_IMAGE_REF` in `.env` to run other images, such as a published release or your own per-commit tag. `make check` runs every check CI runs.

## Ground rules

- All changes land through pull requests. Direct pushes and force pushes to `main` are blocked, and only maintainers listed in `.github/CODEOWNERS` can merge.
- PR titles follow [Conventional Commits](https://www.conventionalcommits.org/). CI checks this.
- One concern per PR. Design changes go in the docs in the same PR as the code that depends on them.
- `make check` must pass. It runs lint, format check, type check, and tests, exactly as CI does.
- Do not commit secrets, tokens, workspace identifiers, local databases, or agent CLI local settings.
- Never add a workflow that uses `pull_request_target` to check out fork code.

## Principles that are not up for debate

Contributions that violate these will not be merged:

- JotFlow's code never uses GitHub credentials. The team's agent performs every GitHub write, in its own container, and only when JotFlow hands it an approved proposal.
- JotFlow stores pointers, not message content.
- JotFlow never calls an LLM itself; the team supplies the agent.
- No hosted component, no telemetry.

## Licensing

By contributing you agree that your contributions are licensed under the Apache License 2.0 that covers the project.
