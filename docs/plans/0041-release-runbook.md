# A release runbook for the maintainer

## Links and status

- Issue: #41 (Refs). Also serves #87 and #39, whose last boxes need the first real release
- Draft PR: to be opened
- Status: draft
- Last verified: 2026-10-19, against main and release.yml
- Next safe action: review; the maintainer follows it for v0.1.0

## Objective and done-when

Cutting the first release has one-time steps outside the repo, such as PyPI's pending publisher and the `pypi` environment. It also has steps after the workflow, such as package visibility, the GitHub release, and the clean-machine install. None of that is written down. A runbook makes v0.1.0 repeatable and lets someone other than its author check each step.

Done when:

- [x] `docs/releasing.md` covers the one-time setup: the PyPI pending publisher with its exact fields, and the `pypi` environment with a required reviewer and a tag-only deployment rule
- [x] It covers the pre-tag pull request: the version in `pyproject.toml`, `__version__` and `uv.lock`, the CHANGELOG date, and the README banner
- [x] It covers the tag and push commands, and what each release.yml job does in Actions, including approving the PyPI job
- [x] It covers after the release: both GHCR packages made public, `gh attestation verify` for both images and both package files, the GitHub release from docs/releases/v0.1.0.md, and the clean-machine install for #87, #39 and #41
- [x] It covers rollback: a bad tag before publishing, a failed PyPI job, a bad PyPI release yanked and never deleted, and bad images fixed forward
- [x] CONTRIBUTING.md links it
- [x] Every job name, field and command matches release.yml and the repo as they are

## Design

- **Fix forward.** PyPI never reuses a version, so after anything publishes, the answer is the next patch version.
- **The version bump goes through a pull request,** since main is protected. That pull request also runs release.yml's dry run.
- The `pypi` environment doesn't exist yet, and PyPI's `jotflow` name was unregistered on 2026-10-19. The runbook says to set both up close to the tag.

## Validation

- `make check` passes.
- The jobs, the environment name, the image names, and the version checks were read from release.yml.
