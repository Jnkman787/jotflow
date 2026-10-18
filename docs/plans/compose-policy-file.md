# Mount the policy file in Compose

## Links and status

- Issue: #9
- Draft PR: #68
- Status: implementing
- Last verified: Compose behaviour with a missing bind-mount source checked on Docker Desktop 28.3, Compose 2.38, 2026-10-18
- Next safe action: run the local CodeRabbit review, then wait for CI on #68

## Objective and done-when

Since #64, JotFlow reads its admin policy from `jotflow.toml`, at the path in `JOTFLOW_CONFIG`, defaulting to `jotflow.toml` in the working directory. In the container the working directory is the database volume, so nothing puts the team's policy file there. Compose must hand the file to the container read-only.

Done when:

- [x] With `jotflow.toml` in the checkout, `docker compose up` gives the container the file read-only at a fixed path, with `JOTFLOW_CONFIG` pointing at it
- [x] Without `jotflow.toml`, `docker compose run --rm jotflow jotflow --version` still works and nothing is created on the host
- [x] The container's unprivileged user can read the mounted file, and cannot write it
- [x] CI validates both Compose configurations and checks that the mounted example policy loads inside the image
- [x] Docs say how to start JotFlow with the policy file

**Out of scope:** `jotflow run` (#66) and `jotflow init` (#67). This change touches only Compose files, the CI image job, and docs. Writing `jotflow.toml` with a mode the container can read is #67's concern; this PR documents the requirement.

## Design

**Finding that shapes the design.** When a bind mount's source file is missing, Docker Desktop creates a root-owned directory with that name on the host, even with `bind.create_host_path: false`. The same happens with a top-level `configs:` entry. Mounting `./jotflow.toml` from the base Compose file would therefore leave a `jotflow.toml/` directory in the checkout of anyone who runs Compose before `jotflow init`. That directory then blocks `init` from writing the file and needs root to remove.

**Approach: a second Compose file that adds the policy.**

- `docker-compose.yml` stays as it is: it runs without a policy file, so `--version`, image checks, and CI keep working.
- `docker-compose.policy.yml` adds a read-only bind mount of `./jotflow.toml` at `/etc/jotflow/jotflow.toml` and sets `JOTFLOW_CONFIG` to that path.
- Starting JotFlow: `docker compose -f docker-compose.yml -f docker-compose.policy.yml up -d`. To make plain `docker compose up` do the same, set `COMPOSE_FILE=docker-compose.yml:docker-compose.policy.yml` in the shell or in the project `.env`, which Compose reads for its own settings.
- The policy is not a secret, so the file must be readable by the container's uid 10001: mode `0644`, not `0600`.

Alternatives considered:

- A mount in the base file: unsafe, as above.
- Mounting a committed directory such as `config/` that always exists: safe, but `jotflow init` writes `jotflow.toml` at the repo root, and moving it would collide with #67.
- Mounting the whole checkout read-only: it would expose `.git` and other files the container has no use for.
- `${VAR:-/dev/null}` as the mount source: hides a missing policy behind a confusing "missing required key" error.

**CI.** The image job tags its build as `jotflow:local`, the image name Compose uses, then:

- validates the merged configuration with `docker compose -f docker-compose.yml -f docker-compose.policy.yml config --quiet`
- copies `jotflow.example.toml` to `jotflow.toml` with mode `0644` and runs `load_policy` on `JOTFLOW_CONFIG` inside the container through the merged files, as uid 10001
- checks that the mounted file cannot be written

## Risks and open questions

- People will forget the second file. The `COMPOSE_FILE` line is the fix; #67 may choose to write it into `.env`. That is product's call and is suggested on #67, not done here.
- If `jotflow.toml` is created with mode `0600`, the container cannot read it and `jotflow run` fails with a read error. Documented here; #67 decides the mode.

## Progress checklist

- [x] Confirm the issue is ready: bounded objective, testable done-when, no open decision
- [x] Commit this plan and open the draft PR
- [x] `docker-compose.policy.yml`
- [x] CI image job: merged config validation and policy load check
- [x] Docs: Deployment section in architecture.md
- [x] Verify locally: with and without `jotflow.toml`, read-only, readable by uid 10001
- [ ] CodeRabbit local review
- [x] `make check` passes
- [ ] Mark the PR ready for review

## Validation

Run locally on 2026-10-18 with Docker Desktop 28.3.2 and Compose 2.38.2:

- Missing-source behaviour, in a scratch directory: a bind mount with `create_host_path: false` and a top-level `configs:` entry both left a root-owned `jotflow.toml/` directory on the host when the file was missing. This is the reason for the separate file.
- Both `docker compose config --quiet` and the merged config with `docker-compose.policy.yml` pass.
- Without `jotflow.toml`, `docker compose run --rm jotflow jotflow --version` prints `jotflow 0.0.1`, and no `jotflow.toml` appears in the checkout.
- With the example policy installed as `jotflow.toml` at mode `0644`, `load_policy(JOTFLOW_CONFIG)` succeeds inside the container as uid 10001. Appending to the file from inside the container fails, and the host file is unchanged.
- On a Linux filesystem, a `0600` file owned by the host user fails with `cannot read file: [Errno 13] Permission denied: '/etc/jotflow/jotflow.toml'`, and `0644` loads. A Windows-backed checkout under `/mnt/c` ignores modes, so this cannot be seen there.
- `make check` passes.

## Handoff notes

- Suggested to product for #67: write `jotflow.toml` with mode `0644`, and consider writing the `COMPOSE_FILE` line into `.env` so plain `docker compose up` picks up the policy. Neither is done here.
