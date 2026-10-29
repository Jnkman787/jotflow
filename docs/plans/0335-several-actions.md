# One message with several actions

## Links and status

- Issue: #335
- Draft PR: #351, with #331's and #332's plans. It will move to its own PR once built
- Status: planning. The store change needs the architect's and the lead's ok
- Last verified: 2026-10-29, against main
- Next safe action: the architect and the lead decide open question 1

## Objective and done-when

A pasted email or set of meeting notes holds three or four separate actions, often with different owners. Today `link_message` gives a message one topic, so the paste becomes one proposal. From #335:

- [ ] The drafting instructions say how to handle a message with several actions. Clearly separate actions become separate topics, or one issue with a checklist when they belong together. The agent asks when unsure
- [ ] One message can feed several topics, if the design needs that, with each topic quoting only its own part
- [ ] A replay fixture covers a pasted email with three actions

**Out of scope:** more than 3 proposals in a run. The per-run cap stays, and a fourth action waits for the next run or a question.

## Design

### The instructions, which always apply

drafting.md, step 2 or 6:

- **Separate work:** if one message holds several clearly separate actions, such as different machines, owners or repos, give each its own topic and proposal.
- **One piece of work:** when they belong together, make one issue with a `- [ ]` checklist, one item per action.
- **When unsure:** ask which they want.
- **Quote only its part:** each proposal summarises only its own part of the message.

### The store: one message, several topics

Today `message_refs` has one `topic_id` per message. There are two options.

- **A, my recommendation: a small `message_topics` table,** `(channel_id, ts, topic_id)`, in migration 22. It records the extra topics a message also feeds.
  - The message's own `topic_id` stays its first topic, so everything that reads `message_refs` keeps working.
  - `link_message` gains `decision = "also_topic"`, which starts or joins another topic for an already-linked message. It's allowed only by the agent, never over a person's decision, and at most 3 topics a message.
  - What changes: `topic_threads` and `get_topic` list the message for each of its topics, and a follow-up or the weekly summary finds the extra topic through it.
- **B: no store change.** The agent starts the extra topics from the paste with `propose_issue`'s topic omitted, which it may not do today. Each issue cites the paste as its source. The extra topics then have no message linked, so follow-ups, Same as and the summary can't find their thread. I'd avoid B.

### The fixture

`pasted-email.json`: an email pasted with "Mixer:", "Labels:" and "Sensor timing:" sections, each with "OURS: we need to…".

- The mixer and sensor actions are separate work in `acme-co/web` and `acme-co/api`, so they become two topics and two proposals.
- The labels section is a checklist item on the mixer issue.
- Each body quotes only its own part.

## Risks and open questions

1. **The architect and the lead: option A's store change and its migration number.** It touches `link_message`, topic reads, follow-ups and the summary. If that's too much for 0.5.0, the instructions alone are a fallback: separate actions become a checklist issue, and the agent asks whether to split it.
2. **drafting.md bytes:** about +350 with option A's tool wording, pinned.
3. **Per-run cap:** three actions use all three proposals.

## Progress checklist

- [ ] The architect's and the lead's decision on question 1
- [ ] Store: migration and `also_topic`, if A
- [ ] drafting.md, with pins
- [ ] The fixture, and unit tests for `also_topic`'s rules
- [ ] Docs and CHANGELOG
- [ ] `make check`, then mark ready

## Validation

To come.

## Handoff notes

Fictional examples only.
