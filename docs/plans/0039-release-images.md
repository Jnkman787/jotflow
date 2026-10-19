# Publish the container images to GHCR on a version tag

## Links and status

- Issue: #39
- Also serves: #87 (PyPI, same tag), #60 (pinned actions, next)
- Draft PR: #174
- Status: implementing
- Last verified: actionlint 1.7.12 with shellcheck clean on release.yml, and the Compose overrides checked with `docker compose config`, on 2026-10-19
- Next safe action: product agrees the tag and version contract, then the reviewer reviews

## Objective and done-when

- [ ] Pushing tag `vX.Y.Z` publishes `ghcr.io/jnkman787/jotflow:X.Y.Z` and `:latest`, and the same two tags for `ghcr.io/jnkman787/jotflow-agent`, for linux/amd64 and linux/arm64.
- [ ] Each published image has a signed build provenance attestation from `actions/attest-build-provenance`, pushed to the registry, so `gh attestation verify oci://ghcr.io/jnkman787/jotflow:X.Y.Z --owner Jnkman787` passes.
- [ ] The workflow uses the minimum token permissions:
  - `contents: read` everywhere;
  - `packages: write` only where images are pushed;
  - `id-token: write` and `attestations: write` only where attestations are made;
  - no other permissions.
- [ ] The release refuses a tag that doesn't match the package version: `pyproject.toml`'s `version` and `jotflow.__version__` must both equal `X.Y.Z`.
- [ ] The base `docker-compose.yml` image references can be overridden from `.env`, with the defaults left as local builds, so source checkouts work exactly as today:
  - `image: ${JOTFLOW_IMAGE_REF:-jotflow:local}` for jotflow;
  - `${JOTFLOW_AGENT_IMAGE_REF:-jotflow-agent:local}` for both agent services.

  `JOTFLOW_IMAGE` stays the in-image marker and is never reused. The Compose file that `jotflow init` writes for a PyPI install pins `ghcr.io/jnkman787/jotflow:X.Y.Z` (#87, product's).
- [ ] On a pull request that touches the release workflow or a Dockerfile, the same workflow builds both images for both architectures and pushes nothing, so it's proven before a real tag.

## Design

`.github/workflows/release.yml`, with four jobs:

1. **version:** reads the tag, checks it against `pyproject.toml` and `__init__.py`, and outputs `X.Y.Z`. This job holds `contents: read` only.
2. **build:** a matrix of image × arch, running on native `ubuntu-24.04` and `ubuntu-24.04-arm` runners, so no QEMU is needed. It uses the runner's own `docker buildx`, so no third-party build action. Each run builds one platform and pushes it by digest only, with no tag, then uploads the digest as an artifact. The agent image takes `AGENT_NPM_PACKAGES` from the same pin as `docker-compose.yml`, which a step reads so the two can't drift. On pull requests, a run builds and doesn't push. This job holds `packages: write` on tag runs.
3. **publish:** per image, `docker buildx imagetools create` joins the two digests into one index, tagged `X.Y.Z` and `latest`. Then `actions/attest-build-provenance` attests the index digest with `push-to-registry: true`. This job holds `packages: write`, `id-token: write` and `attestations: write`.
4. Login uses `GITHUB_TOKEN` with `docker login --password-stdin`. No personal token or other secret is involved.

Actions are pinned to commit SHAs, with the version in a comment, ahead of #60.

## Risks and open questions

- **Tag and version, with product for #87:** the tag is `vX.Y.Z`, and the version lives in `pyproject.toml` and `__init__.__version__`. The workflow fails a mismatched tag. PyPI publishing can be a job in the same `release.yml`, or its own workflow on the same tag. Product's call.
- **First publish:** GHCR packages are created private. The maintainer makes them public once, in the package settings. That goes in the release notes.
- **`latest`** moves on every release tag, including a fix to an older line. v1 has one line, so that's accepted.
- CodeRabbit runs once if it's not rate-limited.

## Progress checklist

- [ ] Agree the tag and version contract with product
- [x] `release.yml`, with the dry-run build on pull requests
- [x] Compose overrides, with `.env` and README notes
- [x] Docs: README install from a release, and verifying an attestation
- [x] actionlint passes locally

## Validation

- actionlint 1.7.12, with shellcheck 0.11.0, reports nothing on `release.yml`. The findings it reports in `ci.yml` are #60's.
- `docker compose config` shows `jotflow:local` and `jotflow-agent:local` by default, and the `ghcr.io/...:X.Y.Z` references once `JOTFLOW_IMAGE_REF` and `JOTFLOW_AGENT_IMAGE_REF` are set.
- On this PR, the release workflow's dry run builds all four image and architecture pairs and pushes nothing. The publish job is skipped, which shows the tag-only path is gated.
- The first real tag is the maintainer's to push, and the attestation is verified then with `gh attestation verify`.
