# Pin Actions to commit SHAs and lint the workflows

## Links and status

- Issue: #60
- Status: ready for review
- Next safe action: reviewer reviews

## Objective and done-when

- [x] No workflow references an action by tag only. `actions/checkout` is pinned to v7.0.1 and `astral-sh/setup-uv` to v7.6.0, the newest v7, so nothing changes behaviour. Each pin has its version in a comment. #174's `release.yml` is pinned the same way. Dependabot's existing `github-actions` entry keeps the pins current.
- [x] The linter runs in CI and passes. A new `Workflow lint` job runs:
  - zizmor 1.31.1, online with the job's read-only token, so it also checks that each pinned SHA really belongs to its action;
  - actionlint 1.7.12, a binary pinned by sha256, which runs every `run:` script through the runner's shellcheck.
- [x] Push protection is confirmed. The repository API reports `secret_scanning` and `secret_scanning_push_protection` as enabled, along with Dependabot security updates.

## Design

zizmor audits supply-chain risks: unpinned uses, excessive permissions, template injection, persisted checkout credentials, and impostor commits. actionlint catches syntax, expression and shell errors.

Its shellcheck findings in `ci.yml` are fixed:
- the compose and token arguments are now bash arrays, which fixes the word splitting;
- one intentionally single-quoted `$JOTFLOW_CONFIG`, expanded inside the container, carries a commented `shellcheck disable`.

## Validation

- `zizmor .github/workflows`, run online, reports no findings for `ci.yml` and `pr-title.yml`, and for #174's `release.yml` too.
- `actionlint` with shellcheck 0.11.0 passes on every workflow.
- The CI run on this PR runs the same job.
