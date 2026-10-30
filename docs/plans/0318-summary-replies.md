# Replies in the weekly summary's thread reach the idea they name

## Links and status

- Issue: #318
- Draft PR: #352, for the design. Built in this PR, which brings the plan in
- Status: ready for review. The design was agreed with security and the lead on 2026-10-29
- Last verified: 2026-10-29, against main
- Next safe action: review

## Objective and done-when

The weekly summary (#308) lists a channel's ideas, and people discuss them in its thread. A reply there about one idea should reach that idea, but JotFlow can't move a Slack message. Drafting reads only its own thread (#269).

From #318:

- [ ] A design is agreed with security
- [ ] A reply that names an item reaches it, under the agreed rules, with tests

**Out of scope:** routing by title. JotFlow guessing which item a reply means. Any agent run reading the summary's thread.

## Design

### Naming an item: by number, with `re:`

- **Numbers:** each item in the summary gets a number, "1. Export report tables as CSV". The order is stored with the post in a `summary_items` table `(channel_id, slot, position, topic_id)`, migration 22 or the next free one, written before posting as `summary_topics` is (#340).
- **The reply:** in the thread of a channel's posted summary, a reply starting `re: 2` names item 2. That's the `re:` keyword people already use for `re: web#42`. JotFlow parses it, never the agent. A bare "2" or a title names nothing.
- **Which summary:** only the channel's latest summary, whose posted ts is the thread's parent, and only a number it listed. That's the rule Not this week follows, so there's one rule to learn. In an older summary's thread, a `re: N` gets a private reply instead, "That summary has passed; reply in the idea's own thread".

### What routing does: a pointer and a note, no drafting

- **The pointer:** JotFlow links the reply to the item's topic as the poster's own decision, `linked_by = "person"`, exactly as `re: web#42` does, and confirms with its usual reaction.
- **The note:** JotFlow posts one note in the item's own thread: "Alex replied about this in the weekly summary: <permalink|see the reply>". The replier's display name is plain, escaped text, never an `<@U>` mention, so nobody is pinged in a thread they didn't post in, as security asked. The text is fixed, and the link is a permalink built from the workspace URL, the channel and the ts. No reply text is copied, and there's at most one note per reply.
- **A summary's thread never arms drafting.** Today, since #308, a reply in the summary's thread arms a drafting run for that thread, as any reply does. That run's packet mixes every listed idea's title with the replies. This design stops it: the debouncer never arms a posted summary's thread, whatever the reply says, and only `re: N` routes a reply. Security called this the most important part. The item's thread isn't drafted again because of the note, since the note is JotFlow's own message.
- **A new idea posted in the summary's thread** isn't drafted. Its author gets a private reply, at most once per person per summary: "To suggest a new idea, post it in the channel, not in the summary's thread." The message itself is left alone.
- **A person continues it.** To change the draft, someone replies in the item's thread, or uses Request Changes on its card, where #304 and #330 already work.

### Who may route

- **A member:** a member of this workspace, not a guest or external user, in a listed, unpaused channel. That's the rule for `re:` today: guests' and external users' keywords are already refused by ingest.
- **Approvers:** with approvers on, anyone may still route, since a pointer and a note change nothing anyone approved.
- **Removed ideas:** an item number whose topic's proposal was rejected or cleared since gets "That idea is no longer open," and nothing is routed. That keeps #308's removed-content rule.
- **Private replies:** an unknown number ("That summary has no item 7"), an older summary, a removed idea and a new idea in the thread each get a fixed ephemeral reply. These are limited per person, so a flood of replies can't make JotFlow flood ephemerals.

### What's stored

`summary_items` holds only pointers: channel, slot, position and topic id. The reply's link to the topic is today's `message_refs` row. No text is kept.

## Decisions

Security, on 2026-10-29:

1. **A pointer plus the note is enough.** The item's drafting never reads a message from another thread, which keeps the per-thread packet boundary that #304 and others rely on. To get something drafted, people reply in the item's own thread, and the note's link takes them there.
2. **Only the latest summary's numbers route.** An older one gets the private reply.
3. **A bad number gets an ephemeral reply,** limited per person.
4. **A new idea in the summary's thread gets an ephemeral reply,** once per person per summary, and isn't drafted.
5. **The note never pings:** the display name is plain, escaped text.

The lead, on 2026-10-29, from the reviewer's notes:

6. **Migration 22,** with #335 taking 23.
7. **A bot mention in a summary's thread arms no drafting either.** Question runs may still answer there: they only read, and the summary is already posted in the channel.
8. **Notes are capped,** at one per person per item per summary. Later replies are only reacted to.
9. **`re: N` only means an item in the latest summary's thread.** Elsewhere `re: 2` keeps today's meaning, and a test pins it.

Also from the reviewer:
- `summary_items` is pruned as `summary_topics` is.
- With no workspace URL, the note goes out without a link.
- A linked summary reply counts toward the topic's messages. But #307's credits come from the idea's own thread, so the replier isn't credited, which is fine. #339's feed check would see the summary's thread as feeding the topic, which is harmless, since that thread never drafts.

## Progress checklist

- [x] Security decides the open questions
- [x] `summary_items`, and numbers in the summary
- [x] `re: N` in a summary thread: parse, check, link, react, note
- [x] Tests on both stores:
  - numbering and routing
  - a guest refused
  - an unknown number, an older summary and a removed idea, each with its private reply
  - a new idea in the thread replied to once
  - the note's fixed text, with no mention, and its permalink
  - a summary's thread never armed
  - the private-reply limit
- [x] Docs: usage.md, architecture.md, the threat model and the CHANGELOG
- [ ] `make check`, then mark ready

## Validation

- `tests/test_summary_replies.py` covers the routing rules on both stores.
- `tests/test_service.py` checks two things. A new idea, a bot mention and a `re: 1` in a summary's thread leave the debouncer empty, and the keyword handler never sees them. A `re: 2` in an ordinary thread still attaches to web#2.
- `make check` passes.

**Security's second look:** a reply that lands before the summary's row is marked posted, or under a row a crash left failed, was missed by `summary_for_thread`. Now a reply whose `parent_user_id` is JotFlow's bot is handled as a summary-thread reply whatever the row says, and the debouncer re-checks at flush. `tests/test_service.py` covers the window and `tests/test_debounce.py` the flush. `_pointed` and `_noted` are pruned by slot, as the reviewer asked. Replies to the help guide, JotFlow's other top-level post, are now never drafted either. So nothing is silently dropped, each author gets one private nudge per thread, "To suggest a new idea, post it in the channel, not in a reply to JotFlow's post." A service test covers it.

## Handoff notes

Fictional examples only: `C0EXAMPLE1`, Alex.

**Which posts the guard covers:** replies under any of JotFlow's own top-level posts, meaning the weekly summary, the help guide, the pause, resume and auto-mode announcements, and `jotflow check`'s live test post, are never drafted. Their authors get a one-time private nudge instead (#362).
