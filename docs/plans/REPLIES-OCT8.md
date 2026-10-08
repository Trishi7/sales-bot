# REPLIES-OCT8 — replies to "give me a moment", the "today" answer, the Next steps layout, the test clock

8 Oct 2026. One session, no agent team. Built on NFT2-1063 (`replies.py`, `wording.py`, `replyguard.py`), Rule 13
and NFT2-1069. Report: `docs/test-reports/REPLIES-OCT8.md`. Nothing is committed.

Four things the human reported in the live test of 8 Oct. Each is a section: what happened, why, what was built,
and what I chose where the prompt left a choice.

---

## 1. A reply started a second answer

### What happened

```
trishi7 11:21  @Saley what is in the news forthis hour?
Saley   11:21  (reply) Give me a moment, I'm looking into that.
trishi7 11:21  (reply to that) sure            -> thumbs-up (right)
trishi7 11:21  (reply to that) take ur time    -> treated as a new question
Saley   11:22  (reply to "take ur time") One sec — I'm pulling this together.
Saley   11:22  (reply to the question) the news answer
Saley   11:22  (reply to "take ur time") "Today's a Wednesday, so here's what's on the schedule: ..."
```

### Why

`bot._handle_query` gives a message one reaction when `replies.is_ack` says it is only an acknowledgement.
`ACK_PHRASES` had "sure", "ok", "thanks" and friends, and not "take ur time". So the message went on to the engine
as a question, with the interim line quoted above it as its context, and the engine answered it.

Two more things were wrong underneath and would have bitten next:

- Nothing said "a reply to an interim line is not a new question". Only the phrase list stood in the way.
- "no rush" and "no worries" contain "no". `approvals.read_vote` finds a vote word anywhere, so under an open offer
  those would have been read as a NO and declined it.

### What was built

| Where | Change |
|---|---|
| `replies.ACK_PHRASES` | plus what people say while waiting and the ways they spell thanks: "take your time", "take ur time", "no rush", "no hurry", "no worries", "no problem", "all good", "fine", "carry on", "sure thing", "okie", "thank u", "tysm", "thanks a lot", "awesome", "gotcha" and the like |
| `replies.ACK_FILLERS` | small words allowed between them ("and", "pls", "then", "so") so "ok and thanks" and "sure, pls take ur time" read as what they are |
| `replies.ACK_MAX_WORDS` | 5 → 8 ("ok no rush, take your time, thanks" is seven). A bare yes/no keeps 5 (`VOTE_MAX_WORDS`) |
| `replies.ack_is_vote` (new) | does this acknowledgement contain a phrase that is itself a yes or a no? "sure" does, "no rush" does not. `_maybe_acknowledge` asks this instead of `read_vote`, so "no rush" under an open offer is a reaction and the offer stays open |
| `replies.after_ack` (new) | the message with the acknowledgements it opens with taken off: "take ur time, also any news on ElevenLabs?" → "any news on ElevenLabs?" |
| `bot._replies_to_interim` (new) | is this a direct reply to one of the bot's interim lines? By what the bot remembers saying (`_said` kind "interim") and by the line's text, so it holds after a restart |
| `bot._handle_query` | a reply to an interim line: if what is left after the acknowledgement is question-shaped (`_looks_like_question`), the message is answered **as its own question**: that text, with the interim line dropped from its context (`_as_its_own_question`). Otherwise it stays a reply to the interim line |
| `bot._maybe_acknowledge` | a reply to an interim line that got this far carries no question: one reaction, whatever it says ("take ur time", "yes", "hmm interesting") |

### The rule, as a table

| A reply to ... | that says ... | gets |
|---|---|---|
| an interim line | only an acknowledgement, a yes/no, or a remark with no question | one reaction, no text, no model call |
| an interim line | a question, with or without politeness in front | that question answered, as its own |
| the answer, or a quiet line | an acknowledgement | one reaction (as before; the new phrases count) |
| a message with an open offer | "thanks", "no rush", "no worries" | one reaction; the offer stays open |
| a message with an open offer | "sure", "ok" | a yes to the offer (unchanged) |

"One question, one answer" follows: the only thing that produces an answer is a question.

### Choices

- **"No question in the reply" is decided by code**, with the detector the bot already uses to decide whether
  something is worth the engine (`_looks_like_question`: a question mark, or it opens with a question or request
  word). It is liberal, so "take ur time, show me Acme" is a request and is answered. A remark such as "I am in a
  meeting till lunch" gets a reaction.
- **The interim line is dropped from the question's context**, not quoted to the model. It says nothing, and on
  8 Oct quoting it is what led the model to talk about the schedule.

---

## 2. "What are we doing today?"

### What happened

The `today` route offered `show_todos` and `todays_objectives`, and `todays_objectives` pasted the day's posts word
for word. On 7 Oct the reply opened "Today's a Wednesday, so here's what's on the schedule: Scheduled for today
(Wed 7 Oct): AI news post at 2 PM (already posted) …". The human wants the team's day: meeting notes, the channel,
what is due around today, and a very short summary of the day's posts without the news.

### What was built

**The answer is built by code, with no model call** (`bot._answer_today`, reached from `_route_query` when
`toolsets.route(text) == ["today"]`, before the extractor and the router). `today.py` is a new pure module that
renders it; `bot._today_brief` reads the sources.

| Group | Source | Reader |
|---|---|---|
| **From today's meetings** | sales notes dated today, through the notes allowlist | `_today_notes` → `notes.list_notes(days=0)`, `notes.read_note`; the note's parsed action items |
| **Due soon** | from today through `TODAY_LOOKAHEAD_WORKING_DAYS` (2) working days | `_today_due`: P1 items not done on the Deliverables Checklist (`nextaction._deliverable_due`); AI events and the day registration closes when not registered (`gtm_sheet.parse_event_date`); open reminders (`db.scheduled_reminders_due_by`); to-do sheet items with a due date (`todos.open_items`); meetings on Outreach PoCs not yet completed |
| **In the channel today** | today's messages in the sales channels | `_today_channel_messages` → `query.channel_recent_activity(days=1)`, kept to today, minus messages addressed to the bot; `today.channel_lines` keeps the ones with something to do |
| **Also today** | what was sent today and what the plan still holds | `_today_posts` → `db.drip_sent_today` + `_plan_drip`; `today.posts_summary` names what they cover in one line (two when there are more than four kinds); then a line for the open to-dos |

Rendering rules (`today.py`): a group with nothing is skipped; nothing anywhere is one line
(`wording.NOTHING_TODAY`); first names; each quoted line is one line, cut at 160 characters, with mentions, links
and **clock times removed**; at most 6 meeting lines, 8 due lines, 5 channel lines.

What it never says, and why it cannot: no model writes it, so there is no "Today's a Wednesday"; the summary uses a
fixed label per kind of post and no rule number; the AI news post and the news-company screen are not in the
summary (`today.NEWS_TYPES`, `bot.OBJECTIVES_EXCLUDED_TYPES`); a channel message about the news is not quoted;
"scheduled" and "already posted" are not words the code has.

The route (`toolsets._ROUTES["today"]`) gains "what are we supposed to do today", "what is the team working on
today", "what are we doing today", "what do we have today", "what's there to do today". It still does not take
"any AI news today?", "who replied today?" or "what is Vaishnavi working on today".

### Choices

- **Code, not a model.** The prompt says the short summary is "built by code"; I built the whole answer that way.
  It is what guarantees the "never" list, it costs nothing, and it comes through when the model is down (Kushal's
  point from 6 Oct). The cost: the channel part is people's own sentences, picked by a word list, not a written
  summary. A message with no action word in it is not shown. Listed as a question in the report.
- **Meeting notes give their action items**, not a summary of the discussion ("action items and dates only").
- **P1 deliverables only**, as rule 4 and the strategy already say P2 and P3 are never mentioned.
- **Nothing overdue.** "From today through the next 2 working days", as specified. An overdue P1 is in Monday's
  checklist post, not here. Listed as a question.
- **No counts in the posts line.** A post that has gone out is remembered by its text, not by how many people were
  on it; a count that might be wrong is worse than none.
- **A source that cannot be read is named** in a last line (`wording.today_unread`) and the rest still comes.
  Before, an unreadable send record replaced the whole answer with "I couldn't read what's gone out today".
- **`todays_objectives` stays a tool**, for a model that reaches the subject another way; it now puts this answer
  in the reply. `OBJECTIVES_SHOW_OFFERS` is removed (nothing is pasted, so there is no offer line to show or hide).

---

## 3. The Next steps post, grouped by ask

`wording.next_step_post(people)` builds the lines; `drip.points_of` uses it for Rule 13.

- One group per **ask and email number** (and, for a call, per whether Next Steps still has to be set to Call the
  PoC, because that changes the sentence). The ask once (`wording.next_step_group`), then "- Name (Company)".
- What is personal goes after a colon on that person's line (`wording.next_step_detail`): "sent 5 Oct" for an
  advance, "DM sent 28 Sep" for a call, and any cells the row lacks.
- Groups open where their first person stands in the rule's pick order, so call reminders come first. A blank
  line between groups.
- **The opener line is dropped** ("A few next steps on people we're connected with:" and its two siblings;
  `NEXT_STEP_OPENERS` and `opener_index` are removed). With each group opening on its own ask, an opener was a
  third line saying "next steps" under a heading that says it. The heading and the tags line stay.
- The items now carry `who`, `when` and `set_call` (they already carried `ask`, `email_n`, `missing`).
  `wording.next_step_line` is kept: it is the one-sentence form the preview, the log and the report show.

Unchanged: who is picked, when, and Rule 13's state. The "done" replies read the post's stored record
(`db.next_step_post`), never its text, so they work on the grouped post as they did.

---

## 4. The test clock

`clock.py` stores the pretend day with `real_start`, the real moment it was set. New `_expired()`: when
`real_start`'s IST date is before the real today, clear the pretend day (memory and storage), log it once at
WARNING, and carry on with the real date. `_load()` asks it on every read, so both a restart the next morning and
a bot left running past midnight drop it at their first read. Within one real day nothing changes.

---

## 5. Files

New: `today.py`, `tests/oct8_checks.py`, `verify_replies_oct8.py`, `tests/test_replies_oct8.py`, this plan, the
report. Changed: `replies.py`, `bot.py`, `wording.py`, `toolsets.py`, `clock.py`, `drip.py`, `nextaction.py`,
`config.py`, `.env.example`, `README.md`, `DEPLOY.md`, `sales_policy.md`, `docs/rule13-for-the-sheet.md`, and the
tests that pinned the old behaviour (listed in the report with before and after).

## 6. Environment

NEW: `TODAY_LOOKAHEAD_WORKING_DAYS=2`. CHANGED default: none. RETIRED: none.

## 7. Tests

`tests/oct8_checks.py`, run by `verify_replies_oct8.py` and `tests/test_replies_oct8.py`: every case in the
prompt, live and in test mode, with every source probe a stand-in. Module self-tests: `python -m replies`,
`-m today`, `-m clock`, `-m wording`, `python drip.py`.
