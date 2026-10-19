# Proposal capture: closing out #24

## Links and status

- Issue: #24
- Status: ready for review
- Next safe action: reviewer reviews, then merge

## Objective and done-when

- [x] Pending proposals visible in the store with their sources. #109 already does this: `propose_issue` and `propose_update` store a pending proposal with its packet-checked sources. Supersession is #103 and #104.
- [x] A run that ends without proposals is recorded as such. Before this, the run record held no link to its proposals, apart from the proposals' own `run_id`, which nothing queried.

## Design

`Store.run_proposals(run_id)` returns the proposals a run produced, oldest first, in both stores. SQLite uses the existing `proposals_run` index, so no migration is needed. A finished drafting run with none returns an empty list, so the outcome is queryable from the run's id. `RunLifecycle.finish` logs `run <id> ended (<status>) with N proposal(s)` for drafting runs, so the count appears in the log too. It's ids and a count only, never text.

## Validation

- A contract test in both stores: a finished drafting run with no proposals returns `[]`, and a run with two returns exactly its own two.
- A lifecycle test checks the log line. `make check` is green.
