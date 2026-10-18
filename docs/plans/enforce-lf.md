# Enforce LF line endings

## Links and status

- Issue: #74
- Draft PR: #75
- Status: implementing
- Last verified: audit of the index on origin/main, 2026-10-18
- Next safe action: add `.gitattributes`, `.editorconfig`, the ruff setting, and the CI check

## Objective and done-when

Every file in the repository uses LF line endings, on every platform and with every Git client, and CI rejects anything else. See the done-when list on #74.

**Out of scope:** changing any file's content. The audit shows every committed file is already LF, so nothing needs renormalising in the repository itself.

## Design

- **`.gitattributes`**: `* text=auto eol=lf`. Git detects text files, stores them as LF, and checks them out as LF regardless of `core.autocrlf`. Common binary types are marked `binary` so Git never touches them, for when images or archives arrive later.
- **`.editorconfig`**: `end_of_line = lf`, `insert_final_newline = true`, `charset = utf-8`, plus the indentation the repo already uses. Most editors honour it, so files are written correctly before Git sees them.
- **ruff**: `[tool.ruff.format] line-ending = "lf"`, so `make format` and the format check enforce LF on Python files even if `.gitattributes` were bypassed.
- **CI**: a `Line endings` job in `ci.yml` that runs `git ls-files --eol` on the PR's checkout and fails on any committed file whose index entry is CRLF or mixed. Added to the ruleset's required checks.
- **Make target**: `make eol` runs the same check locally; `make check` includes it.
- **AGENTS.md**: a rule under Commits and pull requests.

Audit on origin/main: 63 files `i/lf`, one empty file (`tests/__init__.py`, `i/none`), no `i/crlf`.

## Risks and open questions

- A contributor whose Git has `core.autocrlf=true` will still see LF files after this, because `eol=lf` overrides it. Their editor might still write CRLF; Git converts on commit, and CI catches anything that slips through.
- None open.

## Progress checklist

- [x] Confirm the issue is ready
- [ ] Commit this plan and open the draft PR
- [ ] `.gitattributes` and `.editorconfig`
- [ ] ruff `line-ending = "lf"`
- [ ] `make eol` and inclusion in `make check`
- [ ] CI `Line endings` job, proven to fail on a CRLF file
- [ ] AGENTS.md rule
- [ ] `make check` passes
- [ ] Merge, make the job required, restore the maintainer's checkout

## Validation

To be filled in.

## Handoff notes

None yet.
