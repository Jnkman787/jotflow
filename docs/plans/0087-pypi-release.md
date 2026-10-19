# Publish the Python package, and install without cloning

## Links and status

- Issue: #87
- Also serves: #39 (the images, already published by `release.yml` from #174), #41 (the first tagged release), #88 (the Distribution section)
- Draft PR: #176
- Status: implementing
- Last verified: 2026-10-19, `make check` and actionlint pass, and the published Compose files pass `docker compose config` locally
- Next safe action: CodeRabbit, then ready; the maintainer adds the PyPI pending publisher

## Objective and done-when

A team installs JotFlow from an empty folder, with no clone:

```
uvx jotflow init
docker compose up -d
```

The package and the images are built from the same tag and version, and published together.

Done when:

- [ ] The `jotflow` package is published to PyPI on each `vX.Y.Z` tag through PyPI trusted publishing (OIDC). No API token is stored anywhere, and only the publish job gets `id-token: write`
- [ ] The sdist and wheel are built from the tagged commit, with build provenance attestations
- [x] Run outside a checkout, `jotflow init` also writes `docker-compose.yml` and `docker-compose.policy.yml`, pinned to `ghcr.io/jnkman787/jotflow:X.Y.Z` and `jotflow-agent:X.Y.Z`, and never overwrites an existing file without asking
- [ ] From an empty folder on a clean machine, `uvx jotflow init` then `docker compose up -d` reaches the connected state with no clone
- [x] The README quickstart leads with the no-clone install, and the clone path moves to CONTRIBUTING.md
- [x] The package version and the image tag cannot drift: the release job fails if they differ
- [x] `make check` passes, including the coverage floor

## Design

- **`release.yml`**, extended. The image jobs are #39's, already merged.
  - A `package` job runs on tags and on pull requests that touch packaging. It checks out the tagged commit and runs `uv build`, giving an sdist and a wheel. It checks that the wheel's version is the `version` job's, which already checks the tag against `pyproject.toml` and `__version__`. It installs the wheel into a fresh venv and smoke-tests it: `jotflow --version`, and rendering the published Compose files, which must parse with `docker compose config`. Then it uploads `dist/` as an artifact. Permissions: `contents: read`.
  - A `pypi` job runs on tags only, after the images are published, so a version on PyPI always has its images on GHCR. It uses the `pypi` environment and `pypa/gh-action-pypi-publish`, pinned by SHA, with trusted publishing and PEP 740 attestations. It also attests build provenance for `dist/*` with `actions/attest-build-provenance`. It alone has `id-token: write` and `attestations: write`.
  - The version cannot drift, because one `version` job's output names the images and the package, and the package job refuses a wheel whose version differs.
- **Templates in the wheel.** Hatch force-includes the repo's own `docker-compose.yml`, `docker-compose.policy.yml`, `agent.env.example`, and `agent-exec.env.example` under `jotflow/templates/`, so they can't drift from the checkout. `jotflow.deploy.published_compose(text, version)` makes the base file runnable without the source:
  - It removes each service's `build:` block.
  - It changes the image defaults `jotflow:local` and `jotflow-agent:local` to `ghcr.io/jnkman787/jotflow:X.Y.Z` and `...-agent:X.Y.Z`.
  - It keeps `${JOTFLOW_IMAGE_REF:-…}`, so a team can still override the image from `.env`, as #39 asks.
  - Tests pin the result: no `build:`, both pinned references, and everything else unchanged.
- **`jotflow init` outside a checkout.** "Outside a checkout" means the folder has no `Dockerfile` and no `pyproject.toml` naming `jotflow`. There, setup writes both Compose files and, for an agent preset, the agent env examples, after the last question, like everything else.
  - An existing file is kept unless the person agrees to replace it. The old one is saved as `<name>.bak`.
  - A development install, whose version has no published image, says so and writes no Compose file, so it never pins an image that doesn't exist.
  - `COMPOSE_FILE` is set as today.
- **Docs.** The README quickstart leads with `uvx jotflow init` then `docker compose up -d`. The clone-and-build path moves to CONTRIBUTING.md. The Distribution section in `docs/architecture.md` (#88) describes the release.
- **The maintainer's one-time setup**, which no code can do. On PyPI, add a pending trusted publisher for `jotflow`: owner `Jnkman787`, repo `jotflow`, workflow `release.yml`, environment `pypi`. On GitHub, create the `pypi` environment, with required reviewers if wanted. The name `jotflow` is free on PyPI today, so the pending publisher also reserves it. The handoff notes and the PR say this.

## Risks and open questions

- **The clean-machine box** can only be proven by the first real tag, as for #39's images. The package job's smoke test proves what it can before then.
- **Who owns `release.yml` now.** #39's image jobs are merged. This adds two jobs beside them and changes none, which I've asked the lead to confirm.
- **The name.** Until the maintainer adds the pending publisher, someone else could register `jotflow` on PyPI.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when; the maintainer's PyPI setup is outside the code
- [x] Commit this plan and open the draft PR
- [x] `jotflow.deploy`: the templates and `published_compose`, with tests
- [x] `jotflow init`: write the Compose files outside a checkout, ask before overwriting, with tests
- [x] `release.yml`: the package and pypi jobs
- [x] Packaging: force-include the templates, and test the built wheel has them
- [x] Docs: README quickstart, CONTRIBUTING.md, architecture's Distribution section
- [x] CodeRabbit once: one finding, fixed (checkout detection parses pyproject.toml)
- [x] `make check` passes
- [ ] Mark the PR ready for review

## Validation

- `make check` passes, and actionlint 1.7.12, CI's pinned version, passes on every workflow.
- `uv build` gives a wheel carrying the four templates under `jotflow/templates/`, built through the sdist.
- The published Compose file, with the policy file and the `agent` profile, passes `docker compose config` locally.
- `tests/test_deploy.py` shows:
  - the templates are byte-for-byte the checkout's files
  - the published file has no `build:` and both images pinned, with everything else unchanged
  - a build block ends at its service's next key
  - only a plain X.Y.Z release is pinned, and the wheel force-includes every template
- `tests/test_init.py` shows, outside a checkout, both Compose files written at 0644, and an existing different file kept unless the person agrees, then backed up. An unchanged file isn't asked about. A checkout, by `Dockerfile` or `pyproject.toml`, keeps its own files. A development version pins nothing. Agent files start from the packaged examples.
- Not provable before a tag: the PyPI upload and the clean-machine run (box 4). The package job's smoke test installs the wheel alone and renders the Compose files on every packaging PR.

## Handoff notes

- **Before the first tag, the maintainer needs to:**
  1. On PyPI, add a pending trusted publisher for `jotflow`: owner `Jnkman787`, repository `jotflow`, workflow `release.yml`, environment `pypi`. This also reserves the name.
  2. On GitHub, create the `pypi` environment under Settings, Environments. Required reviewers are optional, but recommended.
- After the first release, check box 4 on a clean machine: an empty folder, `uvx jotflow init`, then `docker compose up -d`.
