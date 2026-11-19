# Meeting notes and transcripts as a source (design)

## Links and status

- Issue: #420
- Draft PR: #421
- Status: planning. Design only, revised after security's and the plan reviewer's reviews, with the lead's decisions. Waiting on the spike to test assumptions A1 to A11
- Last verified: 2026-11-19, against main at 59b7159
- Next safe action: the operator spike tests assumptions A2, A5, A6 and A9 first, in a test Workspace

## Objective and done-when

A team that talks through ideas on a Google Meet call loses them unless someone writes them up in Slack afterwards. This design adds a second source next to Slack threads. After a configured call, JotFlow reads the call's meeting-notes document, which the bot's own Google Workspace user account can open. It hands the document to one call run as untrusted text. The proposals appear as ordinary approval cards in one Slack thread per call, under a summary post. Every card from a call waits for a person, in every channel mode.

**One input with two parts, plus an optional second source:**

- **The meeting-notes doc (primary).** When someone turns on Meet's "Take notes" feature, Meet writes a Google Doc that holds two parts: a model-written **summary**, and a **transcript**. One plain-text export returns both, at about 72 KB for an hour's meeting, about 78% of it transcript. The call run reads the transcript part in pages, and uses the summary as a guide. Both parts are untrusted.
- **Native transcript entries (optional, `source = "transcript"`).** These are for deployments that turn on Meet's own transcription. They are entries of raw speech, served by the Meet REST API. This path may need the bot to attend the call (assumption A9).

This plan is the design only. It folds in the operator spike's findings, security's review, and the plan review on #421, with the lead's decisions on both. No code is written until the spike tests the assumptions below.

Done when, for this design:

- [x] The operator spike answers which input exists, access, scopes, timing, events and retention
- [x] Security and the plan reviewer review the design, and the lead decides their findings
- [ ] The spike tests assumptions A1 to A11, recorded here with fictional names
- [ ] Build issues are opened from this design, each naming the features it meets

**Out of scope:**

- Calls on other platforms.
- The Workspace Events hint, and Pub/Sub (security S4).
- Joining a call, posting into it, or writing to the notes doc.
- Reading recordings or Meet chat.
- Mapping attendees or speakers to Slack users or GitHub logins.
- Routing by anything other than a configured meeting and its organizer, such as calendar events.
- An approval setting for call cards. If one is ever wanted, it needs revision-author counting first (security question 7).
- Translation.

## Design

### What the spike found

Generalised from an operator's test deployment:

- **Teams have notes, not native transcripts.** In the test deployment, transcription is off. The notes doc carries its own transcript part, so `conferenceRecords.transcripts` is empty there. Notes are the primary input. Native transcripts are optional.
- **Access goes to hosts.** Meet shares the notes doc with the organizer and the co-hosts only. An ordinary invited guest gets the calendar link but no access. An invite through a group doesn't share it either. So the bot's account can read a call's notes only when one of these holds:
  - it is a **co-host** of the meeting, or
  - the Workspace's notes-sharing setting is "invited guests in your organization", and the bot was invited individually.

  JotFlow can't check either from its side. The docs state it, and JotFlow logs it once at startup: "Meeting notes reach the bot only if it's a co-host, or if notes are shared with invited guests in your organization."
- **No attendance needed for notes.** The Meet REST API may not list a conference record for an account that never joined. The Drive path doesn't depend on the bot attending.
- **Scopes.** `drive.meet.readonly` (restricted: only files Meet created) for the notes doc. `meetings.space.readonly` (sensitive) for conference records, needed for native transcripts and for tying a doc to its meeting (A2). It's never `drive.readonly`, `documents.readonly`, Gmail or Calendar.
- **The export.** A single `files.export` as `text/plain` returns the whole doc, every part included, with no per-tab export. The parts come in this order:
  1. an optional short preamble that repeats the next steps
  2. a header, with a date line and labels for invited people, attachments and meeting records
  3. the summary sections: Summary, Decisions, Next steps and Details. Details paragraphs are split by lines of underscores
  4. a date line, a "Transcript" heading, and the transcript
  5. a one-line footer

  In the transcript, a line of `HH:MM:SS` (time elapsed since the start, about once a minute) is followed by one `Name: text` line per turn, with no time on those lines. Names are display names, not addresses. Headings are localised and may change.
- **No tie on the file.** The doc has no Drive properties or description that name its meeting. What ties it to a meeting is its owner (the organizer), its title (which editors can change), and the calendar event's attachment, whose link carries a Meet notes marker. See "Routing" for the choice.
- **Timing.** Meet creates the notes doc at about the meeting's *actual* end, and shares it 1 to 15 minutes later, once 90 minutes later. A meeting can overrun its slot by hours, so JotFlow never assumes the scheduled end. It discovers a doc when the doc is shared.
- **Events.** An invitee gets only `conference.started` and `transcript.fileGenerated`, never notes events. Entries can be incomplete just after `fileGenerated`. Native transcript entries are kept for 30 days. **v1 uses polling only.** The events hint is left out, because it would need a Pub/Sub credential and scope for at most one poll interval's gain (security S4).

### Assumptions for the spike

Each assumption is numbered, and cross-referenced where the design leans on it. Any that fails changes the design as noted.

- **A1. Reading every part.** *Confirmed by the spike:* one `text/plain` export returns every part. What remains is whether `drive.meet.readonly` alone allows that export, which the build checks first.
- **A2. Tying a doc to its call.** `conferenceRecords.smartNotes` lists the notes doc's file id to a co-host, including one who never joined. The conference record also gives its space, meeting code, and the space's organizer. The spike hasn't tested this yet. *If not:* routing falls back to the owner rule in "Routing", with its weaker opt-in.
- **A3. The owner.** Drive reports the organizer as the notes doc's owner, including for a meeting created on someone else's calendar. Another user can't make a Meet doc owned by a configured organizer without that organizer's action. *If not:* the owner rule is dropped, and notes need A2.
- **A4. Listing.** `files.list` under `drive.meet.readonly` returns only Meet-created files, and accepts a query on owners (`'alice@example.org' in owners`) and on the time a file was shared with the bot. *If not:* JotFlow lists newest first, with a page cap, and filters on its side.
- **A5. The sharing list.** The bot can list the doc's permissions under `drive.meet.readonly`, including link-sharing grants and their roles. *If not:* every notes call counts as having outside people, and with the `skip` default nothing is read. So this assumption is required for notes.
- **A6. A revision marker.** Drive gives a value that changes on every edit of a native Google Doc, such as `version`, `modifiedTime`, or `headRevisionId` if it's filled in for Docs. *If not:* the "read once" rule rests on `modifiedTime` alone, which is coarser.
- **A7. Settling.** The notes doc stops changing within minutes of being shared, unless a person edits it. That gives the default `settle_minutes`.
- **A8. The Internal client.** An OAuth client marked Internal, in the operator's own Google Cloud project, can hold `drive.meet.readonly` without Google's app verification.
- **A9. Native transcripts need attendance.** A co-host that never joined can list the conference record and its transcript entries. *If not:* `source = "transcript"` works only when the bot joins, and joining is out of scope. Native transcripts then wait for a later issue. Notes are unaffected.
- **A10. Native entries don't change** once their transcript is generated, apart from the late completion the spike saw.
- **A11. What Meet announces.** Meet tells everyone present when notes or transcription are on.

### Shape

1. **Discovery is polling.** Every `poll_minutes`, JotFlow lists the Meet-created files shared with the bot whose owner is a configured organizer. The query itself filters by owner (A4, security S6), and the listing is capped at `max_list_pages` pages per poll. JotFlow never fetches the title or metadata of a doc it would ignore. A deployment with native transcripts also lists conference records for its configured meeting codes.
2. **JotFlow ties the doc to one configured meeting, and so to one channel** (see "Routing"). It ignores a doc it can't tie.
3. **JotFlow checks the sharing list** (A5). If a writer grant goes to a link ("anyone with the link can edit"), the call is skipped, whatever the setting. If outside people hold access and the channel's `outside_people` is `skip`, the default for notes, the call is skipped.
4. **JotFlow waits for the doc to settle.** It reads the doc once its revision marker (A6) is unchanged for `settle_minutes`. If the doc hasn't settled within `settle_limit_minutes` of first being seen, the call ends as nothing.
5. **JotFlow reads the doc once.** It streams the export, stops at `max_doc_kib`, and records the marker before and after. If the marker moved during the read, it settles again. The text stays in JotFlow's memory for the run's life only (see "Reading once").
6. **JotFlow checks the rest of the scope.** The channel isn't paused, the bot is still in the channel, the channel's and the deployment's daily caps aren't reached, and the call isn't already handled. Anything else produces nothing (see "Missing or late input, and failures").
7. **JotFlow posts the call's root message** in the channel: "Reading the notes from *weekly sync* on Tue 27 Oct. Ideas from it will appear in this thread." The date is in the channel's summary timezone (#308), or UTC. The root's `ts` is the call's thread, and the `calls` row records it.
8. **A call run starts.** It's a new run kind, `call`, with its own token, tools, guard and instructions. It pages through the transcript part, splits the call into topics, and proposes new issues or updates to filed ones.
9. **The card loop posts each card** in the call's thread. The root message is edited into the call's summary, with the final text for each outcome.
10. **People approve, edit or reject** each card. Call cards always wait for a person.
11. **Replies in the call's thread** are ordinary Slack messages. They're let through JotFlow's own-post guards only by the call's positive record (see "The call's thread"). They revise pending drafts (#304), and their authors are credited (#307). No later run reads the notes or the transcript again.

Principles kept:

- **JotFlow never calls an LLM.** The team's agent drafts, as today. Meet's model wrote the notes, not JotFlow.
- **JotFlow stores pointers, not content.** The database and logs hold file ids, revision markers, Meet resource names, offsets and counts. They never hold notes text, transcript text, the doc's title, or speaker names. The exceptions are a draft's own fields, which are cleared as #102 says. The input's text is held in process memory for one run, and nowhere else.
- **No GitHub credentials in JotFlow,** unchanged. The Google credential is new and read-only, and stays in JotFlow's container. No agent or runner ever sees it.
- **No hosted component.** Polling needs no inbound connection, and v1 has no Pub/Sub.

### Trust model

**Every part of the input is untrusted text,** exactly as a Slack message is.

| | Notes summary | Notes transcript part | Native transcript entries |
|---|---|---|---|
| What it is | A model-written summary | Meet's transcript, inside the doc | Raw speech from the Meet API |
| Size | About 16 KB an hour | About 56 KB an hour | About 50 to 60 KiB an hour |
| Wording | Paraphrased. Open questions and next steps read as settled | Verbatim, with transcription errors | Verbatim, with transcription errors |
| Who said what | Loose. "Bob will look into exports" names Bob, but nothing says Bob said it or agreed | `Name: text` turns with display names, as plain text, editable like any other line. Times are once a minute, not per turn | Each entry has a participant, which is unreliable: a room device carries several people, turns are mis-assigned, and an anonymous joiner names themselves |
| Who can change it | Everyone with edit access to the doc, with no record of which line they wrote | Same as the summary | Nobody, once generated (A10) |
| Prompt injection | A speaker's words may survive the summary, or be laundered into confident prose. An editor can type instructions in | A speaker's words, plus anything an editor types | A speaker's words, as said |

The card says which input a proposal came from: "From the call's notes (written by Meet, editable by its hosts)" or "From the call's transcript (speaker names are not confirmed)". The approver reads the draft knowing that.

The defences are the ones drafting already has, plus these:

- **Every call card needs a person, in every mode** (see "Approval"). There's no setting to change this.
- **The packet marks every block untrusted,** with the same fixed notice, and no input text reaches a tool description. A notes packet gives the summary and the transcript part as numbered blocks (heading, paragraph, list item), with no author. A native packet gives entries with participant labels.
- **The packet removes links and addresses.** Before the agent sees a block, JotFlow replaces every link and every email address with a fixed placeholder, such as `[link removed]` or `[address removed]`. That covers smart chips, Docs, Drive, Meet and Calendar links, and people's addresses (security S7).
- **Drafts can't carry them either.** `propose_issue`, `propose_update` and `revise_proposal` refuse a call draft that holds an email address, or any link to `docs.google.com`, `drive.google.com`, `meet.google.com` or `calendar.google.com`. They check after the same decoding and folding as `_check_no_slack_links`.
- **A call run can do less than a drafting run.** Its exact tool list is in "The call run". It has no `ask_channel`, `point_to_help` or `link_message`. It can't revise a draft outside its own call's thread. Its words reach Slack only on a card or as a title in the summary, both `plain_text`.
- **Outside people are counted, and an unknown counts as outside.**
  - **Notes:** JotFlow reads the doc's sharing list (A5). An address outside `org_domains`, a group it can't expand, or any link grant counts as outside. A link grant with *writer* access always skips the call. The list shows who can edit *now*, not who edited before (security B1). That's one reason `skip` is the default and no card is ever approved automatically.
  - **Native transcripts:** anonymous joiners and phone callers are outside. A signed-in participant is "not confirmed", because the API gives a user id, not an address, and this design asks for no directory scope.
  - **Defaults:** `outside_people` is `skip` for notes and `mark` for native transcripts. A team that calls clients can set `mark` for notes knowingly, and `jotflow check` says what that means.
- **No credits from the input alone (#307).** The footer credits only Slack-confirmed people: those who replied in the call's thread before the draft was made, and those a person added on the card.
- **No assignees from the input alone (#305, #325, #331).** A passage can never be the source for `volunteered`, `named_owner` or `named_in_text`. Each is refused with a fixed reason, and tested. A call run may still use `owner_hint`, which comes from `jotflow.toml`.
  - **`suggested_owner`:** when the input says someone will take the work, the agent may set `suggested_owner`, with the name as written (cleaned, at most 64 characters, `plain_text` on the card) and the passage. The card shows "The notes say Bob will take it (not confirmed)". Bob, or an approver, confirms with **Add me** or **Make assignee**.
  - The field is cleared with the draft (#102). It is outside the content hash, like #304's change marks, and never reaches the execution packet.
- **No poster directives from the input (#331).** A call has no poster. A repo, board or priority the agent took from the input is its own choice, with its usual reason, for a person to approve.
- **A call can't reach another channel.** Each meeting maps to exactly one channel, and the ways a doc is tied to a meeting can't cross channels (see "Routing"). The call run's token names that channel. Its topic index, repos and boards are that channel's only.
- **No second-approver check against the call.** With `require_second_approver = true` (#244), approval refuses someone who wrote the thread's root or a cited Slack message. Passages have no Slack author, so a person can approve the card for their own spoken idea. This is accepted and recorded in the threat model. Mapping attendees to Slack users would rely on names this design says can't be trusted.
- **Topic titles from calls travel.** A call topic's title, cleaned and capped as every title is, enters the channel's index and reaches later runs in other threads, including Slack threads in `auto` mode. The threat-model row for injected speech records this (security N6).

#### Reading once

`files.export` returns the doc as it is now, not a chosen revision (security S1, review B3). So:

- **JotFlow fetches the input once,** when it settles. It streams the export and stops at `max_doc_kib`, so a large doc is never downloaded in full and then cut.
- **It checks the revision marker (A6) before and after** the export. If the marker moved, it settles and fetches again.
- **It keeps the text in memory only,** for the run's life. `get_call_input` serves pages from that copy, never from Drive, so every page comes from one version. The 200-character and 100-character copy checks hash all of the input, not only the pages the agent read, as `doc_tools.py` does for repo docs.
- **On restart, the text is gone.** JotFlow fetches the doc again only if its marker still equals the recorded one. Otherwise the call ends as `nothing`.
- "Never stores text" means the database and the logs. Process memory holds the text for one run.

#### Parsing the notes export

Headings are localised and may change, so the parser goes by shape, not by words. Any doubt fails closed: if no transcript part is found, the call produces nothing, logged as "no transcript part found".

- **The transcript part** is the run of lines after the last heading-like line that is followed, within a few lines, by an `HH:MM:SS` line, up to the one-line footer. Each `Name: text` turn carries the last time seen. A turn before the first time line is given `00:00:00`.
- **The summary part** is the text between the header and the transcript's date line. Its sections are split at heading-like lines, and Details at lines of underscores. Each piece becomes a numbered block.
- **Dropped before the packet:**
  - the preamble, which repeats the next steps
  - the header, whose labels name invited people, attachments and meeting records
  - the date lines and the footer

  So the agent never sees the list of who was invited.
- **Speaker names** in the transcript part are display names typed as plain text. They aren't addresses, and anyone with edit access can change them, so JotFlow never maps them to anyone. Any match would be a lookup by name, and unreliable.
- **Passages** in the transcript part are a range of turns, by turn index, with their carried times. Passages in the summary part are block ranges.
- **Parser tests** use fictional exports, with placeholders for every name and title. They cover:
  - a localised heading
  - a missing preamble
  - a transcript with no time lines, which produces nothing
  - text that only looks like a heading inside a turn
  - a doc with no transcript part, which produces nothing

#### The Google credential and account

- **The token.** JotFlow holds an OAuth refresh token for the bot's account, with `drive.meet.readonly` and `meetings.space.readonly`. The second scope ties docs to their meetings (A2) and reads native transcripts. The token lives in JotFlow's `.env` (mode 0600), or a file it names, never in an agent env file.
- **What a leaked token reaches** (security S3). It reads every Meet-created file ever shared with the bot, including recordings, transcripts and notes from unconfigured organizers, plus every conference record the bot can see. A leak exposes the whole history, not one call. The threat model's asset row says so.
- **Refuse, don't warn.** On every token refresh, JotFlow reads the granted `scope` field. If it holds any scope outside the expected set, such as `drive.readonly`, JotFlow makes no Google call and logs why. `jotflow check` reports the same, plus whether the token works, never the token itself.
- **The bot's account is an asset.**
  - It holds edit access to every notes doc it can read.
  - As a co-host, it holds meeting controls on every call it hosts.
  - The docs say: no admin role, no human use, two-step verification with a security key, and no data except what Meet shares with it.
  - The bot never writes to a doc. Its token is read-only.
- **The OAuth client.** The docs recommend an Internal OAuth client in the operator's own Google Cloud project (A8).
- **Revoke and rotate.** Run `jotflow meetings auth` again, and revoke the old grant in the account's or the admin's third-party access settings.
- **The redactor** learns `ya29.` access tokens, `1//` refresh tokens, `GOCSPX-` client secrets, and the OAuth `code` and redirect URL that `jotflow meetings auth` handles. Payload fields that carry notes or speech are removed from logs, as Slack's `text` is.

**Threat-model rows to add:**
- the refresh token and its blast radius
- the bot's account
- injected speech, including call topic titles in the index
- instructions typed into a notes doc by anyone with edit access
- a spoofed speaker name
- an outsider invited to or joining a configured meeting
- someone with edit access sharing the doc with the bot (see "Routing")
- a guest who can modify the event adding the bot as a co-host
- a doc from an unconfigured organizer, which is never listed
- call text going to the agent's model provider
- the accepted second-approver gap

### Privacy

- **The summary shows proposals, never input text.**
  - It lists each proposal's kind, its title as drafted (`plain_text`), and its state, plus topics discussed that already have a card elsewhere.
  - It names the meeting only by its configured `nickname`.
  - JotFlow never logs, stores or packets the doc's title. It matches `title_prefix` in memory only (security N1).
  - It never lists invitees or speakers. Where outside people were part of the call under `mark`, it gives a count: "2 people from outside the organization had access".
- **Cards show the draft, which may summarise the call,** as a Slack-derived card does.
  - A call draft may not copy a long passage word for word. `propose_issue` refuses a title or body that repeats 200 characters of the notes summary, or 100 characters of transcript text, after normalising. It uses the sampled-hash mechanism #209 uses for repo docs, as a refusal.
  - The docs describe this as a best-effort guard against verbatim copying, not a privacy control. The real controls are the person approving, a summary that shows places rather than text, and **Drop this call**.
  - A card's sources are places, never text: "From the notes, *Next steps*", or "From the transcript, 12:04 to 12:09".
- **The input is read by one run.** Only the call run reads it. Follow-up runs in the call's thread, question runs, and runs in other threads get Slack text and pending drafts only, following #269.
- **Nothing in the store holds input text.** The new tables hold ids, markers, offsets, counts and states. The schema test that rejects body, text or content columns covers them.
- **Drafts are cleared as #102 says,** `suggested_owner` included, in the same transaction, with `secure_delete` and the WAL checkpoint.
- **Drop this call.** The summary has a **Drop this call** button. Any channel member who may reject a card may click it (Approve's rule, D7), including while the channel is paused, since it only removes data (security N5). It:
  - cancels the call run if it's still going, revokes its token, and moves the `calls` row to `dropped`, so the call guard refuses any later write from that run (security S2)
  - rejects every pending card from the call, which clears their drafts
  - removes the call's passage links
  - edits the summary to "Dropped by Alex"

  Approved and filed work stays. A scenario test drops a call during its run, then shows that no card appears afterwards.
- **A root message deleted by a person** counts as a drop, with no name shown.
- **Which channel a call's output goes to.** Exactly one: the channel whose table lists the meeting the doc was tied to. The summary, every card, every thread notice and the weekly summary's items go there, and nowhere else.
- **The operator's rule.** Configure a meeting only for a channel whose members should see what is said on that call. A call's ideas reach everyone in that channel, including any guests. `jotflow check` warns when a meeting is configured for a channel that's shared with another organization or has guests, reusing #161's scan. The docs say this plainly, and say that the input's text goes to the agent's model provider, as thread text does.
- **The opt-in, stated honestly** (review B4).
  - An organizer opts a meeting in by making the bot's account a co-host, or, where notes are shared with invited guests, by inviting it individually. An admin opts it in by configuring the meeting.
  - The opt-in isn't airtight. Anyone with edit access to a notes doc can share it with the bot, and a guest allowed to modify the event can add the bot.
  - So when A2 holds, JotFlow reads a doc only if the conference record ties it to a configured meeting in a space whose organizer is the configured one. A share from someone else then can't bring in a different call.
  - Without A2, a doc shared by an editor is read if its owner is a configured organizer. The docs and the threat model say so.
  - JotFlow never turns on notes or transcription. It reads only what someone on the call started, which Meet announces (A11).
- **The issue footer** says "Raised from a call in Slack", with the Slack-confirmed names and the permalink to the call's summary post. It never carries a Meet, Calendar, Drive or Docs link. Under `issue_links = "none"`, it is the marker alone.

### Routing: tying a doc to its configured meeting

An entry in `[[channels.<id>.meetings.calls]]` names a `meeting_code`, and the `organizer` who owns that meeting. The doc itself names no meeting, so three ties were weighed:

| Tie | What it needs | What can change it | Verdict |
|---|---|---|---|
| `conferenceRecords.smartNotes`: record to doc id | `meetings.space.readonly`, already needed for transcripts | Nothing an editor can touch. The record also gives the space, its code and its organizer | **Recommended,** if A2 holds |
| The calendar event's attachment, whose link carries a Meet notes marker | A Calendar read scope | Anyone who can modify the event can change its attachments | **Rejected.** It adds a scope that reads every event's title, description and attendees, which is more than this design needs, and the tie is still editable |
| Owner, plus a time window | The owner, and the meeting's time | The time window needs the meeting's time, which only the record or the calendar gives. With the record, smartNotes is exact anyway | **Owner alone is kept as the fallback.** The time window adds nothing without a time source |

JotFlow ties an input to an entry in this order, and stops at the first that holds:

1. **Exactly, through the conference record (A2).** JotFlow lists recent conference records for each configured meeting code. It checks that the space's organizer is the entry's `organizer`, because a long-lived space collects unrelated calls (security S5). Then it asks each record for its notes documents. A doc named by such a record belongs to that meeting. Nothing an editor can change takes part. When A2 holds, this is the only rule for notes, and a doc no record names is ignored. A native transcript is always tied this way, by its record.
2. **By owner, only if A2 fails, and only when unambiguous.** JotFlow matches the doc's owner, as Drive reports it (A3), against the entries' `organizer` addresses. It uses the match only when every entry with that organizer is in **one channel**, so a wrong match can't move a call into a channel that shouldn't see it.
   - Among that organizer's entries, `title_prefix` picks the one whose nickname the summary shows. The match is case-insensitive, and the longest prefix wins. Two entries with the same prefix are refused at load.
   - The prefix is only a label. Anyone with edit access can rename the doc, and the title is never stored.
3. **Otherwise, nothing.** The reason is logged.

Also:
- **An organizer outside `org_domains` is refused** when the policy loads. An outside organizer controls the doc and its sharing (security S5).
- **An organizer with entries in more than one channel** gets a load-time warning when A2 isn't confirmed, because their notes would then be ignored.
- **Only docs Meet created** are reachable, because of the scope.
- **Only docs shared within `max_age_hours`** are read, so turning the feature on doesn't draft a backlog. The default of 24 hours also covers a meeting that overruns its slot by hours.

### The call's thread

A call's thread sits under JotFlow's own post. Today, four guards keep every such thread from being drafted (review B1):
- `service.py` routes a reply under JotFlow's own post (`summary_replies.under_own_post`) to `on_unrecorded`.
- `packet.py` (`jotflow_root`) builds an empty packet.
- `read_tools.work` returns `UNDER_OWN_POST`.
- `_drafting_guard` refuses every write (#318, #373, #384).

A call's thread passes each of the four layers only on a **positive record**: a `calls` row whose `root_ts` is this thread. At ingest, the row may be in `drafting` or `posted`. For the packet and for writes, it must be in `posted`. A `dropped` row, or no row, is refused as today. There is never a "not a summary" check.

A scenario fixture shows that a follow-up reply and Request Changes in a call's thread revise a card. It also shows that a weekly-summary thread, the help guide's thread and a mode announcement's thread are still refused.

### The call run

- **The tool list, exactly:**
  - `get_work`, which serves the call's facts, the topic index and the repos, but not the input
  - `get_call_input` (pages)
  - `list_topics` and `get_topic`
  - `link_passage`
  - `propose_issue` and `propose_update`
  - `revise_proposal`, for drafts in its own call's thread only
  - `list_repo_docs` and `read_repo_doc`, where a repo sets `read_paths`
  - the GitHub MCP read tools, with the read token, as drafting runs have them, for the duplicate search

  The per-kind preset test pins this list for `call`, as it does for every kind (security S9).
- **The guard.** It's an allow-list, like `_drafting_guard`. Every call-kind write is refused unless:
  - the ledger shows `get_call_input` served this run's call
  - the `calls` row names this run
  - the row is in `drafting`

  A drop, a pause or a deleted root ends writes. A test calls each tool on an unserved run, on a dropped one, and on a paused one, and checks that each is refused. It also checks that the kind's tool set is exactly the list above (review S4).
- **Runner and presets** (review S5):
  - `RunKind` and `RUN_KINDS` gain `call`.
  - The three copies of `CONVERSATION_KINDS` (service, init and runner) include it.
  - It joins the drafting group in `RUNNER_GROUPS`.
  - A drafting runner that doesn't list `call` turns the feature off, and `check_runners` says so.
  - Each preset's per-kind tool list, and Codex's kinds, gain `call`.
  - `JOTFLOW_AGENT_MODEL_CALL` picks the model for call runs.
- **Concurrency.** At most one call run at a time, and a call run never takes the last free slot under `[agent] max_concurrent_runs`. With `max_concurrent_runs = 1`, a call run starts only when no drafting or question run is queued. So calls never starve Slack threads. They wait instead, and `wait_hours` bounds the wait.
- **Instructions.** The call run's rules go in their own file, `src/jotflow/whiteboard/instructions/calls.md`, with its own size cap and pin test.
  - `drafting.md` still needs a few rules for follow-up runs in a call's thread: a call topic has passages, not messages; revise its draft rather than proposing again; and `suggested_owner` is a name from a call, never an assignee.
  - That's budgeted at about 400 of the 1,002 bytes left under the 14,000-byte cap, with each phrase pinned (review S8).

### Missing or late input, and failures

**Nothing at all.** None of these posts anything in Slack. Each that JotFlow can observe logs one INFO line, with the call's id and a fixed reason, and `jotflow check` shows the last few:

- **No notes reach the bot,** because notes were off, or the bot wasn't a co-host and the sharing setting doesn't include invited guests. JotFlow never learns of the call.
- **The doc can't be tied to a configured meeting,** or its owner isn't configured, so it's never listed.
- **The doc is shared after `max_age_hours`.**
- **The doc never settles** within `settle_limit_minutes`, because someone keeps editing it.
- **The doc changes during every read,** three times running.
- **The bot can't read the doc:** it was unshared or deleted, or the API returns not found or permission denied.
- **Outside people make it `skip`,** or a writer link grant exists.
- **The channel is out of reach:** it's paused, the bot left it, or its or the deployment's daily cap is reached. "Per day" uses the channel's summary timezone, or UTC. A call lost this way isn't retried.
- **The policy changed while the call waited.** JotFlow resolves the tie again just before posting. A call whose meeting was removed, or moved to another channel, ends as nothing.
- **The doc is empty,** or its transcript part is shorter than `min_minutes`.
- **The call is already handled.** `calls` has one row per notes file id, and one per conference record.
- **A native transcript isn't generated,** or its entries never settle, within `wait_hours` of the call's end.

**After the root is posted,** the root is edited into its final text in every case. JotFlow's usual "did not finish" thread notice is never added to a call's thread.

| Outcome | The summary says |
|---|---|
| Proposals made | The list, as in "Privacy" |
| The run proposed nothing | "Nothing to file from the call *weekly sync*." |
| The run failed or timed out, and the restart rule allows no retry | "Couldn't finish reading the call *weekly sync*." Cards already proposed stay, and are listed |
| Every write was refused by a guard | "Nothing to file from the call *weekly sync*." The reason is logged |
| Dropped | "Dropped by Alex." |
| The root was deleted by a person | No edit. The call is dropped |
| Part of the input was past `max_transcript_kib` | The list, plus "The call ran longer than JotFlow reads, so its end wasn't read." |

Transient failures, such as a 5xx, a rate limit or a network error, are retried with backoff that honours `Retry-After`. Every Google fetch has a time budget, like `SLACK_PHASE_SECONDS`. Since pages are served from memory, `get_call_input` itself makes no Google call (review N3).

### Limits: size, cost and rate

| Limit | Default | Bound | Why |
|---|---|---|---|
| Export read, `max_doc_kib` | 1024 KiB | 4096 | An hour's export is about 72 KB, so this is about 14 hours. The export is streamed, and one past the limit ends as nothing |
| Summary part served | 32 KiB | fixed | About 22% of the export, roughly 16 KB an hour |
| Transcript part served, `max_transcript_kib` | 256 KiB | 1024 | About four hours of speech. Past it, the rest isn't read, and the summary says so |
| One `get_call_input` page | 48 KiB | fixed | Served from memory |
| Native entries read per call | 5,000 | fixed | A cap on Meet API pages |
| Proposals per call run, `max_proposals` | 5 | 10 | A call holds more topics than a thread. Replaces the run cap of 3, for this kind only |
| Revisions per call run | `max_proposals` | | So a restarted run can revise each draft (review S10) |
| Passage links per call run | 200 | fixed | Matches `link_message` |
| Calls per channel per day, `max_calls_per_day` | 10 | 50 | Model cost |
| Calls per deployment per day, `[meetings] max_calls_per_day` | 20 | 200 | Model cost across channels (security N4) |
| Call runs at once | 1 | fixed | Never the last free slot (see "The call run") |
| Call run timeout, `run_minutes` | 20 | 120 | The token's ceiling |
| Settle time, `settle_minutes` | 10 | 5 to 60 | Sharing comes 1 to 15 minutes after creation (A7) |
| Settle cap, `settle_limit_minutes` | 120 | 30 to 720 | A doc someone keeps editing. Must exceed `settle_minutes` |
| Oldest share read, `max_age_hours` | 24 | 1 to 72 | No backlog, and room for overruns |
| Native transcript wait, `wait_hours` | 6 | 1 to 24 | From the call's end. Must exceed the settle time |
| Shortest call, `min_minutes` | 5 | 1 to 60 | Skips a mistaken start |
| Poll interval, `poll_minutes` | 5 | 1 to 60 | Google API quotas |
| Listing pages per poll, `max_list_pages` | 5 | fixed | A flood of shares can't run up the listing (security S6) |

**Cost.** JotFlow can't see the agent's token use. An hour's export is about 72 KB: roughly 56 KB of transcript and 16 KB of summary, or about 18,000 input tokens, plus the instructions and the topic index. The docs state that estimate, and point to the daily caps and `max_transcript_kib` as the controls. `JOTFLOW_AGENT_MODEL_CALL` lets a team pick the model for call runs.

**Rate.** Drive and Meet calls share one client, with backoff on 429 and 5xx. The Drive poll is one owner-filtered listing (A4), capped in pages.

### Config shape

```toml
# Once per deployment: the bot's Google account.
[meetings]
org_domains = ["example.org"]          # addresses here are inside the organization
poll_minutes = 5
max_calls_per_day = 20                 # across every channel
# The OAuth client id, client secret and refresh token go in .env:
# JOTFLOW_GOOGLE_CLIENT_ID, JOTFLOW_GOOGLE_CLIENT_SECRET, JOTFLOW_GOOGLE_REFRESH_TOKEN

# Per channel: which meetings feed it, and how.
[channels.C0EXAMPLE1.meetings]
outside_people = "skip"                # notes default "skip"; "mark" reads calls with outside access, marked
max_calls_per_day = 10
max_proposals = 5
max_transcript_kib = 256
settle_minutes = 10
settle_limit_minutes = 120
max_age_hours = 24
min_minutes = 5
run_minutes = 20

[[channels.C0EXAMPLE1.meetings.calls]]
meeting_code = "abc-defg-hij"          # the meeting this entry opts in, from its link
organizer = "alice@example.org"        # who organizes it and owns its notes; must be in org_domains
nickname = "weekly sync"               # what the summary and footer call it
title_prefix = "Weekly sync"           # optional, only for the owner fallback; a label, never stored
# source = "notes"                     # the default; "transcript" reads native entries (A9)
```

- **There's no approval setting.** Call cards always need a person (security B1, the lead's decision).
- **Each meeting code belongs to one entry, in one channel,** checked at load. Every error names its key, such as `channels.C0EXAMPLE1.meetings.calls[0].meeting_code`.
- **An organizer's entries all use one `source`** (review S9). A notes entry and a native-transcript entry for the same organizer could otherwise give one call two runs. Mixing them is refused at load.
- **Validation:**
  - `meeting_code` must look like a Meet code: three, four and three lowercase letters, joined by hyphens.
  - `nickname` is required: 1 to 40 characters, letters, digits, spaces, `-`, `_` and `.` only. It goes into the root message's mrkdwn and the issue footer.
  - `organizer` must be an address in `org_domains`.
  - Every number is checked against its bounds in "Limits", and `settle_limit_minutes` must exceed `settle_minutes`.
- **No `[meetings]` table** means the feature is off, and JotFlow makes no Google call. If the table is removed while calls are `waiting`, those calls end as nothing at the next load. Calls already posted keep their cards.
- **Setup keeps the tables** when it rewrites `jotflow.toml`, and the `policy_writer` round-trip tests cover them. A `jotflow meetings auth` command runs the OAuth flow once and writes the refresh token. Its details belong to the build issue.
- **The startup log line** states the access precondition, as in "What the spike found".

### Approval

- **Call cards always wait for a person,** in `propose` and in `auto` mode. There is no setting.
- **The guard is an allow-list.** In `auto` mode, the card loop approves a proposal only when the store recorded `auto_allowed = true` for it.
  - The insert sets that only for a proposal from a Slack drafting run, in a thread that isn't a call's thread, and not from Add me.
  - Calls never set it. Neither do `revise_proposal`, Add me, or any proposal in a call's thread.
  - The column defaults to false. A new path that forgets the flag leaves its proposals for a person.
- **The migration** backfills `auto_allowed = true` for existing pending proposals from Slack drafting runs, so a card waiting at upgrade time behaves as before. Add me rows stay false (review N1).
- **`AutoApprover.approve_due`** replaces its deny check on `SELF_ADD_RUN` with the flag.
- **The #381 invariant** (security N7): `revise_proposal`, Add me, and every proposal in a call's thread leave `auto_allowed` false.
- **The card** says "From a call: needs a person's approval" in `auto` mode, so nobody waits for an automatic approval that won't come.
- **Who may approve** is the channel's rule, unchanged.

### Store changes

All of these are pointers, ids and states:

- **`calls`:** one row per input. It holds:
  - the channel and the input kind
  - the notes file id and the revision marker read, or the conference record's resource name
  - the meeting code, and how the doc was tied to it: by record or by owner
  - when the input was first seen and when it settled
  - the state (`waiting`, `drafting`, `posted`, `nothing`, `dropped`) and a fixed reason code
  - the root message `ts`
  - the call run's id
  - a count of outside people
  - who dropped the call, and when

  It's unique on the file id, and on the conference record.
- **`call_passages`:** the call, the part, the start and end (block numbers, or entry names and offsets), the topic, and whether the agent or a person placed it. A passage links to up to three topics.
- **`proposals.auto_allowed`,** defaulting to false, backfilled as above.
- **`proposals.suggested_owner`,** cleared with the draft.
- **A source kind on each cited source:** a Slack message or a call passage.

The migration number follows whatever lands first, as usual.

## Features it meets

Every feature a call shares a thread, card, store table or guard with, and how each combination is handled. Each build issue tests its combinations in a scenario fixture (#380) and, where it fits, a stateful invariant (#381).

**Threads.** A call's thread is its root message. Its `ts` is the thread for the call run's grant and run lock, for every card, and for later replies. So the rule of one run per thread at a time holds for a call too. The guards above let replies through.

**Topics (#12, #37).** The agent splits the call into topics. Each is either a new topic in the channel's index, with its own short title, or an existing topic from the index that was discussed. A topic's link to the call is a passage, stored as pointers in `call_passages`:
- for notes: the file id, the revision marker, the part (summary or transcript), and the first and last block numbers
- for native transcripts: the first and last entry resource names, and their offsets

Topic expiry, the index and `get_topic` work as before. `get_topic` lists passages as "notes from the call on Tue 27 Oct, *Next steps*", never text.

**Every "has messages" path counts passages** (review B2). These read `message_refs` or `message_topics` only today, and must also read `call_passages`, in both stores:
- `_has_messages` and `_drop_orphaned`
- `supersede_pending`, `_topics_in_thread` and `_split_siblings`
- `summary._newest_create`
- `write_tools._check_feeds`
- the store's source-on-topic check (`SourceOffTopicError`, #394, #395)

Without that, a follow-up's link in a call thread would orphan-drop a call topic's card, or supersede the other topics' cards, and call topics would never reach the weekly summary. The #381 state machine gains two invariants:
- a topic with a passage is never orphan-dropped
- a follow-up proposal in a call's thread never supersedes another topic's pending card

**Sources that are passages** (review S6). A cited source is either a Slack message or a call passage. Every consumer of `proposal.sources` gets a rule for passages:

| Consumer | Rule for a passage |
|---|---|
| `_packet_ref` | Served as a place, never text |
| `attribution.authors` and the footer | No author lookup. The footer links the call's summary post |
| The outbox's `issue_reaction` | No reaction, since there's no message to react to |
| The weekly summary's `_shown` | Shown, unless the call was dropped or its root deleted |
| The second-approver check | Skipped, as accepted above |
| Card source lines | "From the notes, *Next steps*" or "From the transcript, 12:04 to 12:09" |
| `content_hash` | Includes the passage's pointers, so an approval locks which passages a draft cites |

**New issues and updates.** For a new topic, the run calls `propose_issue`. For a topic already linked to a filed issue, it calls `propose_update`, a comment by default, as after #304. For a likely duplicate found by search, it proposes a new issue with `related` set, and the card offers **Same as** (#36).

**Message splits (#335, #374, #391).** One passage may belong to up to three topics, as one split message may. Each topic's proposal cites only its own passages. A topic that loses its last passage and its last message has its pending proposals superseded, as #374 says for messages.

**Supersede, in a call's thread.** Today a new proposal supersedes the thread's older pending ones, except those for a split message's other topics. A call's thread holds many topics. So within a call's thread, a new proposal supersedes only pending proposals for *the same topic*. That holds for the call run and for every later follow-up run. A test shows five topics keeping five cards through the call run and through a follow-up.

**Follow-ups and revisions (#304).** Replies in the call's thread start an ordinary drafting run on that thread, after the debounce window. Its packet holds the replies and each call topic's pending draft, but not the input. So "make that a bug, not a feature" revises the card, and the reply's author is credited. **Request Changes** works the same way.

**Revision caps (review S10).** A call run's revision cap equals its `max_proposals`, so a restarted run can revise every draft it made. A drafting run in a call's thread gets the same cap, so "make all of these bugs" works.

**A topic with a pending card in another thread.** The call run may link passages to it, but may not revise it, because that draft belongs to another thread. The summary lists it under "Also discussed", with a link to its card. Nothing is posted in the other thread.

**Credits (#307, #396).** As in "Trust model". The card's "Credited in the issue" line lists only Slack-confirmed people. The card gains a **Credit someone** user picker, under Approve's rule. It adds a Slack user to `card_credits` and redraws the card. The poster is "raised from a call", with no name.

**Assignees (#305, #325, #331, #378).** As in "Trust model". `added_on_card`, **Add me** and **Make assignee** work unchanged.

**Approval (D7, #27, #34, #244).** See "Approval". The approver list, the guest rule and the content-hash lock are unchanged. Reaction approval follows #304's rule, as for any card.

**Card edits (#160).** Repo, assignees and boards can be edited as on any card.

**Weekly summary (#308).** A call's topics are ideas like any other. Each is listed in the week its create proposal was made, which works once `_newest_create` counts passages. "Who suggested it" reads "From the call *weekly sync*". **Not this week** works unchanged. An item from a dropped call is left out, as is one whose root was deleted.

**Summary replies (#318).** Out of scope here, as it is for the weekly summary. The positive-record guard above keeps the weekly summary's thread refused.

**Boards (#309).** A call proposal may name boards from the channel's allowlist, with a reason. The approver sees them and can change them.

**Labels and prefixes (#306, #332).** `[issues] label` and `title_prefix` apply as to any new issue. A stated priority can map to a configured label, shown on the card.

**Attachments (#333).** A call has none. JotFlow never attaches the notes doc. Files posted in the call's thread are handled as in any thread.

**Questions (#57, #273, #329).** `ask:` in the call's thread starts an ordinary question run. It reads the channel's topics, and never the input.

**Paused channels (#28, #155).** A call whose doc settles while its channel is paused is skipped, logged, and lost for good. Paused is checked again before the root message is posted, and the call guard checks it on every write. **Drop this call** works while paused.

**Rebuild (#38, #256).** A call topic has no person message, so rebuild restores nothing for it from Slack. `--from-github` restores a filed call topic from its issue marker, as for any issue. Passage pointers are not rebuilt. That costs nothing, since no run reads the input again.

**Run records and lifecycle (#53, #107, #181).** A call run is recorded like any other, with kind `call`. Restart recovery gives an interrupted call run one fresh run, only if the doc's marker is unchanged (see "Reading once").

## Risks and open questions

Answered by the spike or the reviews, and dropped from this list:
- **Which input exists:** notes docs, carrying a summary and a transcript.
- **Who gets the doc:** hosts and co-hosts, or invited guests under the right sharing setting.
- **Attendance:** not needed for notes.
- **The base scopes:** see above.
- **The timing:** notes appear at the actual end, and are shared 1 to 15 minutes later.
- **The events an invitee gets,** and transcript retention.
- **Edition:** the operator's edition has meeting notes.
- **The export:** one `text/plain` export holds every part, in a known order (A1).
- **Ties on the file:** there are none. Routing recommends smartNotes, with the owner as the fallback, and rejects the calendar tie.
- **`require_approval`:** removed.
- **Notes with outside people:** default `skip`.
- **The directory scope:** not needed.

Still open, for the spike: assumptions **A1 to A11** above. Those that decide the shape are:

- **A2:** does `smartNotes` list the doc for a co-host? If so, it's the clean tie and the honest opt-in.
- **A5:** can the bot read the sharing list?
- **A6:** what revision marker does Drive give?
- **A9:** do native transcripts need attendance?

Risks:

- **Every invitee can edit the notes doc,** so its text is no more trusted than a message from any invitee, including outside ones, and nothing says who wrote a line.
- **Notes read as settled when the call wasn't.** A model's summary can turn "maybe" into a decision, and pin work on a name. Cards say where a draft came from, and nothing from the notes becomes an assignee or a credit without a person.
- **Tying a doc to its meeting** rests on A2, with A3 as the fallback. If neither holds, notes can't be opted in by meeting, and this design needs another fact an editor can't change.
- **The opt-in isn't airtight.** Anyone with edit access can share a notes doc with the bot. A2 limits what that can bring in.
- **The export's format isn't a contract.** Headings are localised and may change, so the parser goes by shape and fails closed.
- **Many "has messages" paths** must learn about passages, in both stores. Missing one drops or supersedes a call topic's card silently.
- **Call threads hold many topics at once,** which changes the supersede rule inside them. A missed path there would supersede another topic's card.

## Progress checklist

- [x] Open the design issue (#420)
- [x] Commit this plan and open the draft PR
- [x] Fold in the spike's findings: notes as the primary input, access by co-host, scopes, timing, the export's format, and how a doc is tied to its meeting
- [x] Security's review (5969623680) and the plan review (5969633569), with the lead's decisions
- [ ] The spike tests assumptions A1 to A11
- [ ] Revise this plan with the answers
- [ ] Open the build issues

## Validation

Design only, so nothing to run yet. The spike's findings are in "What the spike found".

## Handoff notes

No code exists for this. #420 is retitled to match. The lead's decisions on the reviews: no approval setting for calls (security B1), notes skip outside people by default (S8), no events hint in v1 (S4), positive-record thread guards (review B1), every "has messages" path counts passages (B2), read-once with a fallback (B3), and an honest opt-in (B4). All examples are fictional: `C0EXAMPLE1`, `example.org`, `example-project`, Alice, Bob and Alex.
