# A weekly summary of each channel's ideas

## Links and status

- Issue: #308
- Draft PR: #315
- Status: approved by the lead and security on 2026-10-29, with the decisions below. Code waits behind #305
- Last verified: 2026-10-29, against main
- Next safe action: review

## Objective and done-when

A team asked for one post a week listing the channel's ideas, so a weekly meeting can go through them. It's a scheduled post per channel, at a set day, time and timezone, with an **Open card** button for each idea still waiting, and **Not this week** to put one off to the next summary.

Done when, from #308's boxes:

- [ ] `[channels.<id>.summary]` sets `day`, `time` and `timezone` per channel. It's off by default, survives restarts, and posts once a week
- [ ] The summary lists every idea from the window with the fields below, within Slack's limits
- [ ] Open card and Not this week work, under the channel's approval rules
- [ ] Paused channels get no summary
- [ ] `make check` passes

**Out of scope, moved to #318 by the lead's decision on 2026-10-29:** the issue's "replies in the summary's thread that name an item are routed to it". It isn't in the done-when list, and routing someone's reply into another thread's draft needs its own design. JotFlow can't move a Slack message, and treating a reply in one thread as input to another thread's drafting run widens what a run reads, which #269 kept to the run's own thread. It's #318.

## Design

### Settings

```toml
[channels.C0123ABCD.summary]
day = "monday"          # monday to sunday
time = "09:30"          # 24-hour, in the timezone below
timezone = "Europe/London"
```

- **All three are required** when the table is present. With no table, there's no summary.
- **The timezone** must be an IANA name the system knows. It's checked with `zoneinfo` at load, so a typo fails at startup, and `jotflow check` names it. The images carry the system timezone data.
- **Setup keeps the table** when it rewrites `jotflow.toml`.

### Which ideas, and what each shows

**The window** is the seven days up to the post time. **An idea is a topic**, by the lead's decision, since one thread can hold several ideas. A topic counts when a proposal to create its issue was made in the window. Its item shows that topic's newest create proposal, which is its draft as it stands. Updates to filed issues aren't ideas. A topic deferred from last week is listed again, marked "from last week".

**Never resurrect removed text** (security). An item is left out when:
- its newest proposal's draft was cleared (`draft_cleared_at`)
- it was rejected, Same as included, or superseded with no newer proposal
- every one of its source messages has been deleted in Slack

Someone who deleted their message, or rejected a card, never sees it reposted at the top of the channel.

Each item has:

- **The title:** the draft's title, cleaned, or the topic's title once the draft text was cleared.
- **Who suggested it:** the thread root's author, the same Slack-confirmed lookup the issue footer uses.
- **Who added to it:** the same contributors as the footer (#307). Names are plain text, which can't ping.
- **The day:** the date the idea was proposed, in the channel's timezone, such as "Tue 27 Oct".
- **The status:**

  | State | Shown as |
  |---|---|
  | Pending | Waiting for approval |
  | Approved, executing or reconciling | Being filed |
  | Applied | Filed as `web#42`, linked |
  | Failed | Couldn't be filed |
  | Deferred | Put off to next week, by whom |

- **A link to its thread**, as a permalink.

### The post

- **Blocks:** a header, "This week's ideas in #channel, 20 to 27 Oct". Then one section per item in `plain_text`, so nothing in a title or name becomes markup or a mention, and a context line with counts.
- **Each waiting item gets two buttons:**
  - **Open card,** a link button to the card's permalink, from `chat.getPermalink` on `card_ts`. It needs no new scope. A link button needs no handler beyond Slack's acknowledgement.
  - **Not this week.**
- **Slack's limits, and a cap** (security): a message holds at most 50 blocks, and each item takes two. So a summary shows at most 20 items: waiting ones first, then the rest, newest first. Beyond that, a closing line says "and 7 more: see the channel". Each title is cut to the card's own title limit, and each section stays under 3,000 characters.
- **Where:** a top-level message in the channel, with link unfurls off. The summary's own `ts` is stored.

### Not this week

- **Who may click:** Approve's rule (D7, #151). That's a member of this workspace, not a guest or external, never in a paused channel, and only an approver when approvers are on.
- **What it does:** it records a deferral for that proposal until the next summary, with who and when. It doesn't change the proposal's state, so the card can still be approved meanwhile. If it is, the next summary shows its real status.
- **The button** is replaced on the summary with "Put off to next week by Alex", by editing the summary message.
- **A deferred idea** is listed again in the next summary, then drops out as usual.

### Once a week, across restarts

- **A new `summaries` table** holds a row per channel and week, with its slot time, the post's `ts` and when it was posted. Its primary key is the channel and the slot, so a second post for the same slot can't be stored.
- **A `summary_deferrals` table** holds a proposal id, until which slot, who and when. Both are pointers, Slack ids and times, never text. Both come in migration 19, by the lead's numbering: #304 has 16, #309 has 17 and #305 has 18.
- **The loop** runs once a minute. For each channel with a summary that isn't paused, it works out this week's slot in the channel's timezone, and posts if the slot has passed and no row exists for it.
  - The row is written before posting, then given the `ts`.
  - If Slack fails, the row is marked failed and retried with backoff, as thread notices are (#32).
- **Daylight saving:**
  - A time that doesn't exist that day, in a spring-forward gap, posts at the first minute after the gap.
  - A time that happens twice, in an autumn fall-back, posts once, at the first.
  - `zoneinfo` gives both, and tests pin them.
- **Downtime:**
  - A slot missed by up to 12 hours is posted late, saying so.
  - Older than that, it's skipped and logged, so a team that restarts on Wednesday doesn't get Monday's summary.

### Paused channels

A paused channel gets no summary. Its slot is skipped, not saved for later, and logged. Paused is checked again at posting time, not only when the slot comes due. `Not this week` is refused in a paused channel, as every card action is. If the bot is no longer in the channel, the week is skipped and logged. Nothing is posted elsewhere to complain about it (security).

## Risks and open questions

1. **Decided by security:** `plain_text`, the same channel, and the three additions above are enough.
2. **Decided by the lead:** discussion routing is #318.
3. **Decided by the lead:** one item per topic.

## Progress checklist

- [x] The lead approves the plan, and security answers question 1
- [x] Commit this plan and open the draft PR
- [ ] Settings, validation, the setup round trip and `jotflow check`
- [ ] The store's tables and migration
- [ ] Choosing the items, the statuses and the blocks, within the limits
- [ ] The loop, with daylight-saving, downtime and pause handling
- [ ] Not this week and the summary edit
- [ ] Tests:
  - the clock at slot boundaries and across daylight-saving changes
  - restarts, so it posts once
  - a paused channel
  - 25 items against the block limit
  - injected titles and names staying plain text
  - who may click Not this week
- [ ] usage.md, architecture.md, the example config, the CHANGELOG (the follow-up for discussion routing is #318)
- [ ] CodeRabbit once, `make check`, mark ready

## Validation

To come.

## Handoff notes

All examples are fictional: `C0123ABCD`, `acme-co/web`, Alex.
