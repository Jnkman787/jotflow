# Releasing JotFlow

This is the maintainer's runbook for cutting a release, written for v0.1.0, the first one. A later release skips the one-time setup and follows the rest.

A release is one `vX.Y.Z` tag. Pushing it runs [release.yml](../.github/workflows/release.yml), which does four things:
- checks that the tag, `pyproject.toml`, and `__version__` agree
- builds both images for amd64 and arm64 and pushes them to GHCR with build provenance
- builds and checks the Python package
- publishes the package to PyPI through trusted publishing

Nothing else creates the GitHub release: you do that by hand at the end.

## One-time setup

Do these once, before the first tag.

### A PyPI pending publisher

A pending publisher lets the first upload create the `jotflow` project on PyPI, with no API token anywhere.

1. Sign in to [pypi.org](https://pypi.org) with the account that should own the project, with two-factor authentication on.
2. Open **Your account**, then **Publishing**, then **Add a new pending publisher**, and choose **GitHub**.
3. Fill in exactly:

   | Field | Value |
   |---|---|
   | PyPI Project Name | `jotflow` |
   | Owner | `Jnkman787` |
   | Repository name | `jotflow` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

4. Save. Once the first upload succeeds, the pending publisher becomes the project's trusted publisher, and the project shows under **Your projects**.

A pending publisher doesn't reserve the name. If someone else registers `jotflow` first, the upload fails, so don't leave a long gap between this step and the tag.

### The `pypi` environment on GitHub

The PyPI job runs in an environment called `pypi`, and PyPI trusts only that environment. A required reviewer means a tag alone can't publish to PyPI: a person approves the job first.

1. In the repository, open **Settings**, then **Environments**, then **New environment**, and name it `pypi`.
2. Under **Deployment protection rules**, turn on **Required reviewers** and add yourself. Leave **Prevent self-review** off if you're the only maintainer, or you can't approve your own release.
3. Under **Deployment branches and tags**, choose **Selected branches and tags**, then add a tag rule for `v*.*.*`. A branch can then never deploy to it.
4. Add no secrets. Trusted publishing needs none.

### Optional: protect release tags

A repository ruleset can stop anyone else from creating, moving, or deleting `v*` tags. Open **Settings**, then **Rules**, then **Rulesets**, and create a tag ruleset targeting `v*.*.*`. Restrict creations, updates, and deletions, with yourself on the bypass list.

## Before the tag

Make these changes in one pull request, `chore(release): 0.1.0`, through the normal review. Main is protected, so the tag must point at a commit already on main.

1. **The version**, in three places that must agree:
   - `version` in `pyproject.toml`
   - `__version__` in `src/jotflow/__init__.py`
   - `uv.lock`, by running `uv lock` after the first two. CI's `uv sync --locked` fails the pull request if you forget, and the release's `uv lock --check` fails the tag
2. **CHANGELOG.md:** change `## [0.1.0] - unreleased` to today's date, as `## [0.1.0] - 2026-10-20`.
3. **README.md:** make the status banner say the release, for example `**Status: 0.1.0, the first release.**`. Remove any line that still says "preparing".
4. **docs/releases/v0.1.0.md:** read it once more against the CHANGELOG.

`make check` passes, and the pull request also runs release.yml's dry run, because it touches `pyproject.toml`. That dry run builds both images for both architectures, and the package, and pushes nothing, so a release is proven before its tag exists. Merge only when every check is green.

## Tag and push

From an up-to-date main, at the merge commit of that pull request:

```sh
git switch main
git pull --ff-only
git log -1 --oneline   # the chore(release): 0.1.0 merge
grep -n '^version' pyproject.toml   # version = "0.1.0"
git tag -a v0.1.0 -m "JotFlow 0.1.0"
git push origin v0.1.0
```

Push the one tag by name. Never push with `--tags`, which pushes every local tag.

## What to watch in Actions

Open **Actions**, then the **Release** run for `v0.1.0`. The jobs run in this order:

1. **Version matches the tag.** It fails at once if `uv.lock` is stale, as `uv lock --check` reports, or if the tag, `pyproject.toml`, and `__version__` disagree. Nothing has been published yet, so see [A bad tag](#a-bad-tag).
2. **Build the Python package**, alongside the image builds. It also installs the wheel alone and renders the published Compose files.
3. **Build jotflow and jotflow-agent**, once per architecture. The arm64 builds run on GitHub's arm64 runners and take the longest.
4. **Publish jotflow and jotflow-agent.** Each tags one index as `:0.1.0`, and as `:latest` when this is the highest release, then attests it.
5. **Publish to PyPI.** It waits for your approval in the `pypi` environment. Approve it from the run page with **Review deployments**. It only runs after both images are published, so a version on PyPI always has its images.

The run concludes green with every job done. A failed job in steps 2 to 4 can be re-run from the run page. The builds are reproducible from the tag, and a re-run pushes the same version again.

## After the release

1. **Make both GHCR packages public.** New packages start private, so `docker compose pull` fails for everyone else until this is done. For each of `jotflow` and `jotflow-agent`, open the package from the repository's **Packages** list, then **Package settings**, then **Change visibility**, and choose **Public**. Check from a machine that isn't signed in to GHCR:

   ```sh
   docker logout ghcr.io
   docker pull ghcr.io/jnkman787/jotflow:0.1.0
   docker pull ghcr.io/jnkman787/jotflow-agent:0.1.0
   ```

2. **Verify the attestations.** Each should report a verified provenance from this repository's release workflow:

   ```sh
   gh attestation verify oci://ghcr.io/jnkman787/jotflow:0.1.0 --repo Jnkman787/jotflow
   gh attestation verify oci://ghcr.io/jnkman787/jotflow-agent:0.1.0 --repo Jnkman787/jotflow
   # The wheel and sdist exactly as PyPI serves them.
   mkdir -p /tmp/jotflow-dist && cd /tmp/jotflow-dist
   curl -sS https://pypi.org/pypi/jotflow/0.1.0/json \
     | python3 -c 'import json, sys; print("\n".join(u["url"] for u in json.load(sys.stdin)["urls"]))' \
     | xargs -n1 curl -sSLO
   for f in jotflow-0.1.0*; do gh attestation verify "$f" --repo Jnkman787/jotflow; done
   ```

3. **Create the GitHub release.** Open **Releases**, then **Draft a new release**, and choose the existing `v0.1.0` tag. Title it `JotFlow 0.1.0`, and paste [docs/releases/v0.1.0.md](releases/v0.1.0.md) as the description, without its first heading. Leave it marked as the latest release.

   Or from a checkout:

   ```sh
   tail -n +3 docs/releases/v0.1.0.md > /tmp/notes.md
   gh release create v0.1.0 --verify-tag --title "JotFlow 0.1.0" --notes-file /tmp/notes.md
   ```

4. **Install it on a clean machine.** Use a machine or VM that has never had JotFlow, with only Docker and uv, and no clone. In an empty folder:

   ```sh
   uvx jotflow init
   docker compose up -d
   docker compose ps   # jotflow becomes healthy
   ```

   Follow the README alone, with a Slack workspace you can test in. Post in a watched channel and approve the card. This run is the evidence for these boxes:
   - #87: the clean-machine `uvx jotflow init` box
   - #39: the box for the `:X.Y.Z` and `:latest` tags
   - #41: the README-only install

   Comment the result on each issue, tick the boxes, and close what's done.

## Rollback

A published version is permanent. PyPI never accepts the same version twice, even after deletion, and people may already have pulled the images. So fix forward with a new patch version, and never move or reuse a tag that has published anything.

### A bad tag

If the **Version matches the tag** job failed, nothing was published. Delete the tag, fix main through a pull request, and tag again:

```sh
git push origin :refs/tags/v0.1.0
git tag -d v0.1.0
```

The same applies if you notice a mistake before any publish job has started. Cancel the run first, then delete the tag.

### Images published, PyPI failed

Re-run the **Publish to PyPI** job. If the cause is outside GitHub, such as the pending publisher's fields or the environment name, fix that first. Don't push a new tag for the same version.

### A bad release on PyPI

**Yank it, don't delete it.**
- A yanked version stays downloadable for anyone who pins it exactly. Installers and `uvx jotflow` skip it otherwise.
- A deleted version breaks those pins, and its number can never be used again.

To yank, open the project on PyPI, then **Manage**, then the release's **Options**, then **Yank**. Give the reason, which installers show.

Then release the fix as the next patch version, such as `v0.1.1`, through this runbook from [Before the tag](#before-the-tag). Note the yank under the fix's CHANGELOG entry.

### Bad images

Don't delete a published image version. Fix forward with the next patch version. Its publish job moves `:latest` to the fix, and `jotflow init` pins new installs to it. Anyone who pinned the bad version, in `.env` or in a Compose file setup wrote, can still pull it, and nothing tells them to move. So the GitHub release for the bad version is where they find out. Edit it to name the version to use instead, and mark it as not the latest.
