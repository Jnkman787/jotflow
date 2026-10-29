# Credit everyone who contributed, in the issue footer

## Links and status

- Issue: #307
- PR: to be opened
- Status: ready for review
- Last verified: 2026-10-29, `make check` on the branch
- Next safe action: review

## Objective and done-when

The footer named who raised the idea, and the cited sources' authors as "looped in". A team using JotFlow found that people who added to an idea in its thread went uncredited unless the draft happened to cite their message. And an update's comment credited the thread's first poster, not whoever added the new information.

- [x] The footer lists the suggester and every contributor, with no duplicates (tested)
- [x] Update comments credit who added the new information (tested)
- [x] Names never become mentions, on GitHub or Slack (tested)

**Out of scope:** crediting people by GitHub login, and the card, which doesn't show the footer.

## Design

- **A new issue:**
  - "Raised in Slack by Sam, with contributions from Alex and Kim." The opening "Raised in Slack" is unchanged.
  - Contributors are the Slack-confirmed authors of the draft's sources, then of every other message linked to its topic in the same thread, in time order. The topic is the draft's `topic_id`, or the one its sources are linked to.
  - Never a message from another thread of the same topic, never one marked as chatter, and never the agent-written participants list. At most 30 messages are looked up, one Slack call each, inside the existing 10-second budget.
- **An update:** "Updated from Slack by Kim." The authors of the update's own sources are credited, and the thread's first poster isn't. With nobody found, it says "Updated from Slack."
- **Names:**
  - Each person is named once, compared without case, and the poster isn't repeated.
  - After nine names the rest are counted: "and 6 others".
  - `clean_name` already strips `@`, Slack markup, links and invisible characters, so no name can ping, and a test pins that.
  - The footer goes only to GitHub, so nothing new is posted in Slack.
- **`issue_links = "none"`:** the marker alone, as before.
- **Files:** `execution/writes.py` (`footer`, `_credited`) and `execution/attribution.py` (`_contributed`, `_authors_of`). The store reaches `execution_footer` from the read tools and replay.

## Validation

- `make check` passes.
- New tests in `tests/test_execution_attribution.py` cover:
  - contributors from the topic, with another thread's message and chatter left out and no ping
  - a topic found through a source
  - an update's credit
  - `issue_links = "none"`
- The existing footer tests are updated for the new wording and the "N others" cap.

## Handoff notes

None.
