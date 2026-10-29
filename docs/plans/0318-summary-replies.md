# Replies in the weekly summary's thread reach the idea they name

## Links and status

- Issue: #318
- Draft PR: to come
- Status: planning, for security's review
- Last verified: 2026-10-29, against main
- Next safe action: security and the lead decide open questions 1 to 3

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
- **Which summary:** only the summary whose posted ts is the thread's parent, and only a number it listed. An older summary's items still route: unlike Not this week, a reply is a pointer, not a decision for the coming week.

### What routing does: a pointer and a note, no drafting

- **The pointer:** JotFlow links the reply to the item's topic as the poster's own decision, `linked_by = "person"`, exactly as `re: web#42` does, and confirms with its usual reaction.
- **The note:** JotFlow posts one note in the item's own thread: "<@U0ALEX> replied about this in the weekly summary: <permalink|the reply>". The text is fixed, and the link is a permalink built from the workspace URL, the channel and the ts. No reply text is copied, and there's at most one note per reply.
- **No agent run reads the summary's thread.** Today, since #308, a reply in the summary's thread arms a drafting run for that thread, as any reply does. That run sees the summary, as JotFlow's own post, and the replies. This design stops it: a reply in a posted summary's thread doesn't arm drafting, and only `re: N` routes it. See open question 4. The item's thread isn't drafted again because of the note, since the note is JotFlow's own message.
- **A person continues it.** To change the draft, someone replies in the item's thread, or uses Request Changes on its card, where #304 and #330 already work.

### Who may route

- **A member:** a member of this workspace, not a guest or external user, in a listed, unpaused channel. That's the rule for `re:` today: guests' and external users' keywords are already refused by ingest.
- **Approvers:** with approvers on, anyone may still route, since a pointer and a note change nothing anyone approved.
- **Removed ideas:** an item number whose topic's proposal was rejected or cleared since gets "That idea is no longer open," and nothing is routed. That keeps #308's removed-content rule.

### What's stored

`summary_items` holds only pointers: channel, slot, position and topic id. The reply's link to the topic is today's `message_refs` row. No text is kept.

## Risks and open questions

1. **Security: is a pointer plus a note enough to "reach" the item?** The alternative is for a drafting run on the item to read routed replies. Its packet would add messages members explicitly routed to that topic with `re: N`, fetched one by one, marked as routed. That widens #269's own-thread rule, narrowly, and I'd keep it for a later issue unless you think it's needed now.
2. **Security: should older summaries' numbers route?** I propose yes, since a pointer is harmless. The alternative is the latest summary only, as Not this week does.
3. **The lead: the migration number,** and whether a mistyped "re: 9" gets a fixed private reply ("That summary has no item 9") or only no reaction. I propose the private reply, through the existing keyword refusal path.

4. **The lead and security: what about a new idea posted in the summary's thread?** Under this design it's never drafted. JotFlow could reply privately, once, saying "To suggest a new idea, post it in the channel, not in the summary's thread." Or the thread could keep drafting as today, for anything without `re: N`. I propose the private reply, since drafting the summary's thread mixes every idea's discussion into one packet.

## Progress checklist

- [ ] Security and the lead decide questions 1 to 3
- [ ] `summary_items`, and numbers in the summary
- [ ] `re: N` in a summary thread: parse, check, link, react, note
- [ ] Tests on both stores: numbering, routing, a guest refused, an unknown number, a removed idea, an older summary, the note's fixed text and permalink, and nothing armed
- [ ] Docs: usage.md, architecture.md, the threat model and the CHANGELOG
- [ ] `make check`, then mark ready

## Validation

To come.

## Handoff notes

Fictional examples only: `C0EXAMPLE1`, Alex.
