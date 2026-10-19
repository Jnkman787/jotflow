# Threat model (#40, the security part)

## Links and status

- Issue: #40, the threat model only. The deployment guide and the agent adapter guide are product's and the architect's.
- Draft PR: #178
- Status: ready for review
- Next safe action: the reviewer reviews the document against architecture.md's Security posture

## Objective and done-when

`docs/threat-model.md` states what JotFlow protects, from whom, how each defence is enforced, and what v1 accepts.

- [x] **Assets:** the Slack bot and app tokens, the GitHub read and write PATs, the runner secrets, per-run MCP tokens, thread text and drafts, and the approval itself.
- [x] **Trust boundaries:** Slack, then JotFlow, then the agent containers, then GitHub, plus the installer's machine during setup.
- [x] **Adversaries:**
  - a channel member;
  - a guest or external Slack Connect user;
  - a prompt-injected drafting agent;
  - a prompt-injected or misbehaving execution agent;
  - malicious repo text;
  - a compromised dependency or image;
  - someone on the host network.
- [x] Each mitigation links to the code that enforces it, or to the decision behind it.
- [x] **Accepted v1 gaps,** each with its reason and follow-up:
  - #173: identity squatting;
  - names in free text under `issue_links = "none"`;
  - the Codex preset is experimental (#172);
  - runs of one kind can read each other's environment (#54);
  - open approval mode by default (D7).
- [x] It's reviewed against architecture.md's Security posture section, with every bullet there traceable to the threat model.
- [x] SECURITY.md links to it.

## Design

The document is laid out as tables of threats against defences, by boundary. It describes no new behaviour. Where the document and the code disagree, the code is checked and the document follows.

## Validation

- Every relative link in `docs/threat-model.md` resolves to an existing file, checked by script.
- Each claim was checked against the code: the auto-mode guest warning in `modes.py`, `--log-bodies` refused in `logs.py`, the per-run caps in `write_tools.py`, the 64 KiB body cap and 30 s timeout in `server.py`, the 1024-token registry bound and two-hour TTL in `tokens.py`, and the 0600 file with `secure_delete` in `sqlite.py`.
- Each of the eight Security posture bullets maps to a section, in the document's traceability table.
- SECURITY.md and architecture.md's Security posture link to the document.
- `make check` is green.
