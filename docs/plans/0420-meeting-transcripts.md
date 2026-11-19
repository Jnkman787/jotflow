# A meeting-transcript source (design)

## Links and status

- Issue: #420
- Draft PR: #421
- Status: planning. Design only. The spike's first findings are folded in. Waiting on open questions 1 to 6 and on the lead's and security's review
- Last verified: 2026-11-19, against main at 59b7159
- Next safe action: the operator spike answers open questions 1 to 6 in a test Workspace

## Objective and done-when

A team that talks through ideas on a Google Meet call loses them unless someone writes them up in Slack afterwards. This design adds a second source next to Slack threads. After a configured call that the bot's own Google Workspace user account was invited to, JotFlow reads what the call left behind and hands it to one drafting run as untrusted text. The proposals appear as ordinary approval cards in one Slack thread per call, under a summary post.

A call can leave two kinds of input, and JotFlow reads one per call:

- **The meeting-notes doc (primary).** When someone turns on Meet's "Take notes" feature, Meet writes a model-generated notes document and shares it with each invitee. JotFlow reads it through the Drive API, with `drive.meet.readonly`, which reaches only files Meet created.
- **Transcript entries (optional).** When transcription is on, the Meet REST API serves the call's transcript as entries of raw speech, with `meetings.space.readonly`.

This plan is the design only. The operator spike's first findings are folded in below. No code is written until the remaining open questions are answered and the lead and security agree the decisions.

Done when, for this design:

- [x] The operator spike answers which input exists, access, scopes, timing, events and retention
- [ ] The spike answers questions 1 to 6
- [ ] The lead and security agree the decisions above, or record changes here
- [ ] Build issues are opened from this design, each naming the features it meets

**Out of scope:**

- Calls on other platforms.
- Reading recordings, Meet chat, or the transcript's Docs file. Transcripts are read as entries through the Meet API only.
- Mapping attendees or speakers to Slack users or GitHub logins.
- Writing to the notes doc, posting into the call, or joining it.
- Routing a call by anything other than its organizer or meeting code, such as calendar events, which would need a Calendar scope.
- Translation. Input goes to the agent in the language Meet wrote it in.

## Design

### What the spike found

Generalised from an operator's test deployment:

- **Notes, not transcripts, are what teams have.** The deployment uses meeting notes, and no transcript documents exist. So notes are the primary input and transcripts are optional.
- **Access comes from the invite.** The organizer's account shares the notes doc with each individually invited guest, as a writer. An invite through a group doesn't share it. The Meet REST API may not list a conference record for an account that was invited but never joined. So the Drive path is the robust one, and nothing in this design depends on the bot attending.
- **Scopes.** `drive.meet.readonly` (a restricted scope, covering only files Meet created) for the notes doc. `meetings.space.readonly` (a sensitive scope) for conference records and transcript entries, needed only when a meeting uses transcripts. Never `drive.readonly`, never Gmail, never Calendar.
- **Timing.** The notes doc reaches invitees roughly 1 to 4 hours after the meeting starts, and the share lags the doc's creation by 1 to 90 minutes. Transcript entries are kept for 30 days, and can be incomplete just after the transcript is reported generated.
- **Events.** An invitee's Workspace Events subscription gets only `conference.started` and `transcript.fileGenerated`, not notes events. So JotFlow polls Drive for newly shared Meet docs after a call, with retries and backoff, and treats events only as an optional hint.

### Shape

1. **Discovery is polling.** Every `poll_minutes`, JotFlow lists files Meet created that were newly shared with the bot. For meetings that use transcripts, it lists conference records for the configured meeting codes. An optional Workspace Events subscription, pulled from Pub/Sub, only makes the next poll come sooner.
2. **JotFlow matches the file to a channel** by its owner, the meeting's organizer, as Drive reports it (see "Routing"). A file from an unconfigured organizer is ignored.
3. **JotFlow waits for the input to settle.** A notes doc is read once it is unchanged for `settle_minutes`. Transcript entries are read once two listings, `settle_minutes` apart, return the same entries.
4. **JotFlow checks the rest of its scope:** the channel isn't paused, the bot is still in the channel, the day's cap isn't reached, and the input isn't already handled. Anything else produces nothing (see "Missing or late input").
5. **JotFlow posts the call's root message** in the channel: "Reading the notes from *weekly sync* on Tue 27 Oct. Ideas from it will appear in this thread." Its `ts` is the call's thread.
6. **A call run starts.** It's a new run kind with its own token, tools and instructions. It reads the input in pages, splits it into topics, and proposes new issues or updates to filed ones.
7. **The card loop posts each card** in the call's thread, as for any thread. The root message is edited into the call's summary.
8. **People approve, edit or reject** each card. Call cards always wait for a person in `auto` mode, unless an operator has turned that off and the call qualifies (see "Approval").
9. **Replies in the call's thread** are ordinary Slack messages. They revise pending drafts (#304) and are credited (#307) as in any thread. No later run reads the notes or transcript again.

Principles kept:

- **JotFlow never calls an LLM.** The team's agent drafts, as today. The notes were written by Meet's model, not JotFlow's.
- **JotFlow stores pointers, not content.** It keeps Drive file ids, revision ids, Meet resource names, offsets and counts. It never stores notes text, transcript text or speaker names. That text exists only in the call run's packet and in drafts, and drafts are cleared as #102 says.
- **No GitHub credentials in JotFlow,** unchanged. The Google credential is new and read-only, and it stays in JotFlow's container. No agent or runner ever sees it.
- **No hosted component.** Polling needs no inbound connection. The optional events hint uses a Pub/Sub pull subscription in the team's own Google Cloud project, the way Slack events arrive over Socket Mode.

### Trust model

**Both inputs are untrusted text,** exactly as a Slack message is. The two kinds differ in what can be trusted about them:

| | Meeting notes | Transcript entries |
|---|---|---|
| What it is | A model-written summary of the call | Raw speech, as Meet transcribed it |
| Size | Short: about a page for an hour | Long: about 50 to 60 KiB for an hour |
| Wording | Already paraphrased. Decisions and next steps read as settled, even when the call left them open | Verbatim, with transcription errors, false starts and crosstalk |
| Who said what | Loose. A line such as "Bob will look into exports" names people, but nothing says Bob said it or agreed | Each entry has a speaker label, which is unreliable: a room device carries several people, turns are mis-assigned, and an anonymous joiner names themselves |
| Who can change it | Every invitee holds a writer grant, so anyone invited can edit or add lines after Meet writes it. Nothing records who wrote which line | Nobody edits entries after they're written |
| Prompt injection | A speaker's injected words may survive the summary, or be laundered into confident prose. Any invitee can also type instructions straight into the doc | A speaker's words arrive as said |
| What can be missing | Anything the notes model left out or got wrong | Anything said while transcription was off |

The card says which kind a proposal came from: "From the call's notes (written by Meet, editable by every invitee)" or "From the call's transcript (speaker labels are Meet's, not confirmed)". The approver reads the draft knowing that.

The defences are the ones drafting already has, plus these:

- **The packet marks every line untrusted,** with the same fixed notice, and none of it reaches a tool description. A notes packet gives the doc as numbered blocks (heading, paragraph, list item) with no author. A transcript packet gives entries with speaker labels.
- **A call run can do less than a drafting run.** It may read (`get_work`, `get_call_input`, `list_topics`, `get_topic`, and the repo docs tools where `read_paths` is set), link passages of the input to topics, and propose. It has no `ask_channel`, no `point_to_help`, and no `link_message` (it is served no Slack messages). It may not revise a draft outside its own call's thread. Its words reach Slack only on a card or as a title in the summary, both `plain_text`.
- **Every call proposal needs a person by default** (see "Approval").
- **Outside people are marked, and failing to confirm counts as outside.** These are allow-lists, so a failed lookup never makes someone internal.
  - **Notes:** the doc has no speakers. JotFlow reads the doc's sharing list, which a writer can see, and counts invitees whose address is outside `org_domains`, plus any link-sharing setting. An invitee is not proof they attended, so this overcounts, which is the safe direction. If the list can't be read, the call counts as having outside people.
  - **Transcripts:** an entry's speaker is internal only when Meet reports a signed-in user and an optional directory lookup confirms them within `org_domains`. Without the directory scope, every signed-in speaker is "not confirmed". Anonymous joiners and phone callers are always outside.
  - The packet carries `outside: true|false|unknown` per invitee count or per speaker. Cards and the summary say when outside people were part of the call.
- **No credits from the input alone (#307).** The footer credits only Slack-confirmed people, as today: people who replied in the call's thread before the draft was made, and people a person added on the card. A name in the notes, or a speaker label, is never credited until someone confirms it on the card (see Credits, under "Features it meets").
- **No volunteered or named assignees from the input alone (#305, #325).** A passage of notes or transcript can never be the source for `volunteered` or `named_owner`. `propose_issue` refuses one with a fixed reason. A call run may still use `owner_hint`, which comes from `jotflow.toml`. When the input says someone will take the work ("Bob will look into exports" in notes, or Bob saying "I'll take it"), the agent sets `suggested_owner` with the name as written and the passage. The card shows "The notes say Bob will take it (not confirmed)" and nothing more. Bob, or an approver, confirms with the existing **Add me** or **Make assignee** controls.
- **No poster directives from the input (#331).** A call has no poster. A repo, board or priority the agent chose from the input is the agent's choice, with its usual reason, for a person to approve.
- **A call can't reach another channel.** Each organizer, and each meeting code, maps to exactly one channel, checked when the policy loads. The call run's token names that channel, so its topic index, repos and boards are that channel's only.
- **No second approver check against the call.** With `require_second_approver = true` (#244), approval refuses someone who wrote the thread's root or a cited Slack message. Notes and transcript passages have no Slack author, so a person can approve the card for their own spoken idea. This is accepted and recorded in the threat model, because mapping attendees to Slack users would rely on the names this design says can't be trusted.
- **The input is read once, at one revision.** JotFlow records the notes doc's revision id when it reads it, and the run reads only that revision's text, so an edit made during the run doesn't change what it drafts from. Later edits to the doc are never read.

**The Google credential.**
- JotFlow holds an OAuth refresh token for the bot's account, with `drive.meet.readonly`, plus `meetings.space.readonly` only when a meeting uses transcripts, and an optional directory scope (open question 4).
- The docs recommend an Internal OAuth client in the operator's own Google Cloud project. An internal client stays inside the organization, and it avoids a public app review for a restricted scope.
- The token lives in JotFlow's `.env` (mode 0600), or a file it names, never in an agent env file.
- The bot never writes to the doc, even though its grant is a writer's, because its token's scopes are read-only.
- The redacting formatter learns Google's token shapes (access tokens, refresh tokens, client secrets). Payload fields that carry notes or speech are removed from logs, like Slack's `text`.
- `jotflow check` reports whether the token works and which scopes it holds, never the token. It warns on any scope beyond the ones listed here.

**Threat-model rows to add:**
- the Google refresh token, as an asset
- injected speech
- instructions typed into a notes doc by an invitee
- a spoofed speaker name
- an outsider invited to, or joining, a configured meeting
- a doc shared from an unconfigured organizer, which is ignored
- the accepted second-approver gap above

### Privacy

- **The summary shows proposals, never input text.**
  - It lists each proposal's kind, its title as drafted (`plain_text`), and its state, plus topics that were discussed but already have a card elsewhere.
  - It names the meeting only by its configured `nickname`. It never uses the doc's title or the calendar title, which people wrote.
  - It never lists invitees or speakers. When outside people were part of the call, it gives a count, such as "2 invitees from outside the organization".
- **Cards show the draft, which may summarise the call,** as a Slack-derived card does.
  - A call draft may not copy a long passage word for word. `propose_issue` refuses a title or body that repeats about 200 characters of the input. It uses the sampled-hash check #209 uses for repo docs, as a refusal rather than a card line. This holds for notes too: their wording is a model's, but it's still the call's content.
  - The instructions tell the agent to summarise, not quote.
  - A card's sources show as places, never text: "From the notes, section *Next steps*" or "From the transcript, 12:04 to 12:09".
- **The input is read once, by one run.** Only the call run reads it. Follow-up runs in the call's thread, question runs, and runs in other threads get Slack text and pending drafts only. This follows #269's rule that a run reads only its own thread.
- **Nothing in the store holds input text.** The new tables hold file ids, revision ids, resource names, offsets, counts and states. The schema test that rejects body, text or content columns covers them.
- **Drafts are cleared as #102 says.** A call draft's title and body are the only stored text derived from the call. They're cleared in the same transaction when the proposal is applied, rejected or superseded, with `secure_delete` and the WAL checkpoint.
- **Drop this call.** The summary has a **Drop this call** button. Any channel member who may reject a card may click it (Approve's rule, D7).
  - It rejects every pending card from the call and clears their drafts.
  - It removes the call's passage links, and edits the summary to "Dropped by Alex".
  - Approved and filed work stays.
  - It's for a call that should never have been read, such as one where a private matter came up.
- **Which channel a call's output goes to.** Exactly one: the channel whose `[channels.<id>.meetings]` table lists the call's organizer, or its meeting code for transcripts. The summary, every card, every thread notice, and the weekly summary's items go there, and nowhere else.
- **The operator's rule.** Configure an organizer or meeting only for a channel whose members should see what is said on that organizer's calls. A call's ideas reach everyone in that channel, including any guests. `jotflow check` warns when a meeting is configured for a channel that is shared with another organization or has guests, reusing the scan behind the `auto` warning (#161). The docs say this in plain words. They also say that the notes or transcript text goes to the agent's model provider, as thread text does.
- **JotFlow never turns on notes or transcription.** It reads only what someone on the call started, which Meet announces to everyone present. Inviting the bot individually is the organizer's opt-in for that meeting. Leaving the bot off an occurrence opts that occurrence out.
- **The issue footer** says "Raised from a call in Slack", with the Slack-confirmed names and the permalink to the call's summary post. It never carries a Meet, Calendar, Drive or Docs link. Under `issue_links = "none"`, it is the marker alone. `propose_issue` refuses a draft that holds a link to the notes doc, the transcript or a recording.

### Routing

- **A notes doc goes to the channel its organizer is configured for.** JotFlow reads the doc's owner from Drive, and matches it against `organizer` addresses in the policy. The owner is a fact Drive reports, and a writer can't change it. JotFlow ignores the title for routing, because any invitee with a writer grant can rename the doc.
- **An optional `title_prefix`** narrows which of that organizer's docs are read, for an organizer who invites the bot to calls meant for different purposes. It is a filter, not a security boundary: every match still goes to the organizer's one channel.
- **A transcript call goes to the channel its meeting code is configured for,** since the Meet API reports the code.
- **Only docs Meet created** are reachable, because of the scope. Only docs first seen within `max_age_hours` of their creation are read, so turning the feature on doesn't draft a backlog of old calls.

### Missing or late input produces nothing

None of these posts anything in Slack, opens a card, or counts as an error. Each that JotFlow can observe logs one INFO line, with the call's id and a fixed reason, and `jotflow check` shows the last few:

- **Notes never reach the bot,** because nobody turned notes on, the bot was invited through a group, or the organizer's sharing failed. JotFlow never learns of the call, so there is nothing to log.
- **A transcript isn't generated,** or its entries never settle, within `wait_hours` of the call's start. JotFlow stops waiting.
- **The bot can't read the input:** the doc was unshared or deleted, entries are past their 30-day retention, or the API returns not found or permission denied.
- **The input isn't in scope:** an unconfigured organizer or meeting code, a doc older than `max_age_hours`, a paused channel, a bot that left the channel, or a channel at its daily cap.
- **The input is empty,** or a transcript is shorter than `min_minutes`.
- **The input is already handled.** `calls` has one row per notes file id and per conference record, so a second poll or an events hint does nothing.

A notes doc that arrives late is still read, as long as it's within `max_age_hours`. The spike saw up to about 5.5 hours from the call's start to the share.

Transient failures, such as a 5xx, a rate limit or a network error, are retried with backoff that honours `Retry-After`, within the waiting window. After the window, the call ends as nothing, with the reason logged.

A call can have both notes and a transcript. A meeting entry reads one, its configured `source`, so one call never yields two runs.

### Limits: size, cost and rate

| Limit | Default | Bound | Why |
|---|---|---|---|
| Notes read per call, `max_notes_kib` | 64 KiB | 256 | Notes are short. A larger doc was likely edited into something else. Beyond the limit, the rest isn't read, and the summary says so |
| Transcript read per call, `max_transcript_kib` | 256 KiB | 1024 | About four hours of speech. Beyond the limit, the rest isn't read, and the summary says so |
| One `get_call_input` page | 48 KiB | fixed | Keeps each tool result well inside the 30-second request budget |
| Entries read per call | 5,000 | fixed | A cap on Meet API pages |
| Proposals per call run, `max_proposals` | 5 | 10 | A call holds more topics than a thread. Replaces the run cap of 3, for this run kind only |
| Passage links per call run | 100 | fixed | Like `link_message`'s 200, but each link is a range |
| Calls drafted per channel per day, `max_calls_per_day` | 10 | 50 | Model cost. Beyond it, calls produce nothing, logged |
| Call runs at once | 1 | fixed | Inside `[agent] max_concurrent_runs`, so a run of calls can't starve Slack threads |
| Call run timeout | 20 min | 2 h | The token's ceiling |
| Wait for the input to settle, `settle_minutes` | 15 | at least 5 | Notes may be edited just after sharing, and entries can be incomplete just after `fileGenerated` |
| Oldest doc read, `max_age_hours` | 24 | 72 | No backlog when the feature is turned on |
| Wait for a transcript, `wait_hours` | 6 | 24 | From the call's start |
| Shortest transcript call, `min_minutes` | 5 | | Skips a mistaken start |
| Poll interval, `poll_minutes` | 5 | at least 1 | Google API quotas |

**Cost.** JotFlow can't see the agent's token use.
- Notes for a one-hour call are about a page: a few thousand input tokens, plus the instructions and topic index.
- A transcript for the same call is roughly 8,000 to 10,000 words: 50 to 60 KiB, and about 15,000 input tokens.
- The docs state both estimates and point to `max_calls_per_day` and the size limits as the controls. A team may point call runs at a different model through the preset, as it may for execution runs.

**Rate.**
- Drive and Meet calls share one client, with backoff on 429 and 5xx.
- The Drive poll asks for files Meet created and shared with the bot since the last poll, which is one listing however many organizers are configured.
- The transcript poll lists conference records per configured meeting code, filtered to recent ones.
- An events hint moves the next poll forward and never makes extra calls.

### Config shape

```toml
# Once per deployment: the bot's Google account.
[meetings]
org_domains = ["example.org"]          # invitees and confirmed speakers from these are internal
poll_minutes = 5
# events_subscription = "projects/example-project/subscriptions/jotflow-meet"   # optional hint, Pub/Sub pull
# The OAuth client id, client secret and refresh token go in .env:
# JOTFLOW_GOOGLE_CLIENT_ID, JOTFLOW_GOOGLE_CLIENT_SECRET, JOTFLOW_GOOGLE_REFRESH_TOKEN

# Per channel: which calls feed it, and how.
[channels.C0EXAMPLE1.meetings]
require_approval = true                # default true: call cards wait for a person even in auto mode
outside_people = "mark"                # "mark" (default) or "skip": a call with any outside invitee or speaker produces nothing
max_calls_per_day = 10
max_proposals = 5
max_notes_kib = 64
max_transcript_kib = 256
settle_minutes = 15
max_age_hours = 24
wait_hours = 6
min_minutes = 5

[[channels.C0EXAMPLE1.meetings.calls]]
organizer = "alice@example.org"        # the notes doc's owner, as Drive reports it
source = "notes"                       # "notes" (default) or "transcript"
title_prefix = "Weekly sync"           # optional filter on the doc's title, not a security boundary
nickname = "weekly sync"               # what the summary and footer call it

[[channels.C0EXAMPLE1.meetings.calls]]
organizer = "bob@example.org"
source = "transcript"
meeting_code = "abc-mnop-xyz"          # required for transcripts, from the meeting's link
nickname = "design review"
```

- **Both opt-ins are needed.** A call is read only when its organizer, or meeting code, is configured here, *and* the bot's account was invited individually, so Meet shared the input with it. An invite through a group doesn't share the notes. The docs say so, and `jotflow check` can't detect it.
- **Each organizer and each meeting code belongs to one channel,** checked at load. Every error names its key, such as `channels.C0EXAMPLE1.meetings.calls[0].organizer`.
- **`require_approval = false`** lets `auto` mode apply to call cards, but only for a call JotFlow confirmed had no outside people. For notes, that means a readable sharing list with every invitee inside `org_domains`. For transcripts, it means every speaker confirmed through the directory scope. Without that scope, transcript calls always need a person. `jotflow check` warns whenever it's false.
- **No `[meetings]` table** means the feature is off, and JotFlow makes no Google call.
- **`meetings.space.readonly` is asked for** only when some meeting uses `source = "transcript"`.
- **Setup keeps the tables** when it rewrites `jotflow.toml`. A `jotflow meetings auth` command runs the OAuth flow once and writes the refresh token. Its details belong to the build issue.

### Approval

- **The rule.** In `propose` mode nothing changes: every card waits. In `auto` mode, the card loop approves a proposal automatically only when the store recorded `auto_allowed = true` for it.
  - The insert sets that flag for a proposal from a Slack drafting run whose thread isn't a call's thread.
  - It also sets it for a proposal from a call whose channel set `require_approval = false`, and which JotFlow confirmed had no outside people.
  - The column defaults to false. So call proposals, and every follow-up proposal in a call's thread, wait for a person unless the operator opted out. A new path that forgets the flag leaves its proposals for a person.
- **The card** says "From a call: needs a person's approval" in `auto` mode, so nobody waits for an automatic approval that won't come.
- **Who may approve** is the channel's rule, unchanged.

### Store changes

All of these are pointers, ids and states:

- **`calls`:** one row per input. It holds:
  - the channel and source kind
  - the notes file id and the revision read, or the conference record and transcript resource names
  - the organizer's address as Drive reported it, or the meeting code
  - when the input was first seen and when it settled
  - the state (`waiting`, `drafting`, `posted`, `nothing`, `dropped`) and a fixed reason code
  - the root message `ts`
  - a count of outside people, with whether it was confirmed
  - who dropped the call, and when

  It's unique on the file id and on the conference record.
- **`call_passages`:** call, start and end (block numbers for notes, entry names and offsets for transcripts), topic, and whether the agent or a person placed it. A passage links to up to three topics.
- **`proposals.auto_allowed`,** defaulting to false. A source kind on each cited source: a Slack message or a call passage.

The migration number follows whatever lands first, as usual.

## Features it meets

Every feature a call shares a thread, card, store table or guard with, and how each combination is handled. Each build issue tests its combinations in a scenario fixture (#380) and, where it fits, a stateful invariant (#381).

**Threads.** A call's thread is its root message. Its `ts` is the thread for the call run's grant and run lock, for every card, and for later Slack replies. So the rule of one run per thread at a time holds for a call too.

**Topics (#12, #37).** The agent splits the call into topics. Each is either a new topic in the channel's index, with its own short title, or an existing topic from the index that was discussed. A topic's link to the call is a passage, stored as pointers in `call_passages`:
- for notes: the file id, revision id, and the first and last block number
- for transcripts: the first and last entry resource names and their offsets

Topic expiry, the index and `get_topic` work as before. `get_topic` lists passages as "notes from the call on Tue 27 Oct, *Next steps*", never text.

**New issues and updates.** For a new topic the run calls `propose_issue`. For a topic already linked to a filed issue, it calls `propose_update`, a comment by default, as after #304. For a likely duplicate found by search, it proposes a new issue with `related` set, and the card offers **Same as** (#36).

**Message splits (#335, #374, #391).** One passage may belong to up to three topics, as one split message may. Each topic's proposal cites only its own passages. A topic that loses its last passage and message has its pending proposals superseded, as #374 says for messages.

**A meeting point: supersede.** Today a new proposal supersedes the thread's older pending ones, except those for a split message's other topics. A call thread holds many topics at once. So within a call's thread, a new proposal supersedes only pending proposals for *the same topic*. A test proves that five topics from one call keep five pending cards.

**Follow-ups and revisions (#304).** Replies in the call's thread trigger an ordinary drafting run on that thread, after the debounce window. Its packet holds the replies and each call topic's pending draft, but not the input: the call's thread feeds those topics through `call_passages`. So "make that a bug, not a feature" revises the card, and the reply's author is credited. **Request Changes** posts its reply in the call's thread and works the same way. A call run that is interrupted and restarted sees the pending drafts it already made, and revises them rather than proposing again.

**A topic with a pending card in another thread.** The call run may link passages to it, but may not revise it, because that draft belongs to another thread. The summary lists it under "Also discussed", with a link to its card. Nothing is posted in that other thread.

**Credits (#307, #396).** As in "Trust model" above. The card's "Credited in the issue" line lists only Slack-confirmed people. The card gains a **Credit someone** user picker, under Approve's rule, which adds a Slack user to `card_credits` and redraws the card. That is how a contribution made on the call is credited: a person confirms it. The poster is "raised from a call", with no name, since the bot posted the root.

**Assignees (#305, #325).** As in "Trust model" above. `added_on_card`, **Add me** and **Make assignee** work unchanged.

**Approval (D7, #27, #34, #244).** See "Approval" above. The approver list, the guest rule and the content-hash lock are unchanged. Reaction approval follows #304's rule, as for any card.

**Card edits (#160).** Repo, assignees and boards can be edited as on any card.

**Weekly summary (#308).** A call's topics are ideas like any other. Each is listed in the week its create proposal was made. "Who suggested it" reads "From the call *weekly sync*" instead of a name. **Not this week** works unchanged. The never-resurrect rule gains two cases: an item from a dropped call is left out, and so is one whose call summary post was deleted in Slack.

**Summary replies (#318).** Out of scope here, as it is for the weekly summary.

**Boards (#309).** A call proposal may name boards from the channel's allowlist, with a reason, as any proposal may. The approver sees and can change them.

**Labels and prefixes (#306, #332).** `[issues] label` and `title_prefix` apply as to any new issue. A priority stated in the call can map to a configured label, as #332 allows, shown on the card for approval.

**Attachments (#333).** A call has none. JotFlow never attaches the notes doc. Files posted in the call's thread are handled as in any thread.

**Questions (#57, #273, #329).** `ask:` in the call's thread starts an ordinary question run. It reads the channel's topics, which now include call topics by title, and never the input.

**Paused channels (#28, #155).** A call whose input arrives while its channel is paused is skipped and logged, not saved for later. Paused is checked again before the root message is posted, and before the run starts. **Drop this call** is refused while paused, like every card action.

**Rebuild (#38, #256).** The "Created" replies in a call's thread restore the links from topics to issues, as for any thread. Passage pointers are not rebuilt. That costs nothing later, since no run reads the input again.

**Run records and lifecycle (#53, #107, #181).** A call run is recorded like any other, with kind `call`. Restart recovery gives an interrupted call run one fresh run, provided the input can still be read.

**The `auto` guard (D7).** See "Approval". It's an allow-list, so a path that forgets to set it leaves the proposal for a person.

**Instructions.** The call run's rules go in their own file, `src/jotflow/whiteboard/instructions/calls.md`, with its own size cap and pin test. `drafting.md`'s 14,000-byte cap is untouched. The file covers both input kinds and their differences in "Trust model".

## Risks and open questions

Answered by the spike, and folded in above: which input exists (notes, primarily), how access arrives (an individual invite shares the doc, and attendance isn't needed), the scopes, the timing, the events an invitee gets, and transcript retention.

Still open for the spike:

1. **Edition.** Which Google Workspace editions produce meeting notes and transcripts, and whether the Meet REST API needs an admin to turn it on for the domain.
2. **The owner field.** Whether Drive reports the organizer as the notes doc's owner, both for a meeting Alice created and for one created through someone else's calendar. Also whether anything on the file, such as a property, ties it reliably to the meeting or its code.
3. **The sharing list.** Whether the bot, as a writer under `drive.meet.readonly`, can list the doc's permissions, which the outside-invitee count needs.
4. **The directory scope.** Which read-only scope, if any, lets a non-admin account confirm a signed-in speaker's domain. It's needed only to set `require_approval = false` for transcript calls. Without it, those calls always need a person.
5. **Reading the doc.** Whether `files.export` to plain text or Markdown works under `drive.meet.readonly`, and whether the export keeps the headings that passages point at. Also whether the notes doc changes after it is shared, which sets `settle_minutes`.
6. **Events.** Whether `conference.started` reaches an invitee who never joins. If it doesn't, the events hint only helps transcripts.

For the lead and security:

7. Whether `require_approval = false` should exist at all, and whether 200 characters is the right word-for-word limit for calls.
8. Whether a notes call with outside invitees should default to `skip` rather than `mark`, since every invitee can edit the doc.

Risks:

- **Every invitee can edit the notes doc,** so its text is no more trusted than a message from any invitee, including outside ones, and nothing says who wrote a line.
- **Notes read as settled when the call wasn't.** A model's summary can turn "maybe" into a decision, and pin work on a name. Cards say where a draft came from, and nothing from the notes becomes an assignee or a credit without a person.
- **Routing rests on the doc's owner** (question 2). If Drive doesn't report the organizer as owner, routing needs another fact a writer can't change.
- **Call threads hold many topics at once,** which changes the supersede rule inside them. A missed path there would supersede another topic's card.

## Progress checklist

- [x] Open the design issue (#420)
- [x] Commit this plan and open the draft PR
- [x] Fold in the spike's first findings: notes as the primary input, access, scopes, timing and events
- [ ] The spike answers questions 1 to 6
- [ ] The lead and security review the design
- [ ] Revise this plan with the answers
- [ ] Open the build issues

## Validation

Design only, so nothing to run yet. The spike's findings are in "What the spike found".

## Handoff notes

No code exists for this. The issue's title still says "transcript", but the design now covers both meeting notes and transcripts. All examples are fictional: `C0EXAMPLE1`, `example.org`, `example-project`, Alice, Bob and Alex.
