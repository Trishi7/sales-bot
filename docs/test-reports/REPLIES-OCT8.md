# REPLIES-OCT8 test report — INCOMPLETE: the full regression has not finished

8 Oct 2026. Base commit `1dcfb36` plus NFT2-1069 and this change, all uncommitted. Plan:
`docs/plans/REPLIES-OCT8.md`. One session, no agent team. Everything was run OFFLINE with `ANTHROPIC_API_KEY=x`.
**Nothing has been checked in the live channel.**

## 1. Where this stands

The code, the plan, the new checks and the documentation exist. **The "all green" condition is not met yet**: the
full `python -m pytest -q tests/` run was stopped at about 60% by the machine running low on memory (no failure
had been printed up to that point), and the offline verify sweep has not been run for this change. Neither was
restarted. Section 5 lists exactly what did run and what did not.

## 2. What ran, and the result

| Command | When | Result |
|---|---|---|
| `python verify_replies_oct8.py --show` (new, 126 checks) | before the last three small edits (section 5) | ALL PASSED |
| `python -m replies`, `-m today`, `-m clock`, `-m wording`, `-m toolsets`, `-m nextaction`, `python drip.py` | after each module's change | ALL PASSED |
| `python -m pytest -q tests/test_rule13.py` | after the Next steps change | 191 passed (after one test was updated) |
| `python -m pytest -q tests/test_replies.py tests/test_env_example.py tests/test_answer_voice.py tests/test_notes_scope.py tests/test_profile_lookup.py tests/test_tone_decisions.py` | after all four parts | 547 passed, 15 skipped, 10 failed: all ten pinned the old behaviour (section 4) |
| the ten, after updating them, plus the other objectives, acknowledgement and wording cases (70 tests) | after the updates | 70 passed |
| `python -m pytest -q tests/` (everything) | final tree | **stopped at about 60% for low memory; no failure printed before it stopped** |
| the verify sweep (`verify_replies.py`, `verify_rule13.py`, `verify_replay_oct6.py`, `verify_s1/s2/s3.py` and the rest) | | **not run** |

## 3. Before and after

### The 8 Oct 11:21 exchange

Before (the live channel, as reported):

```
trishi7  @Saley what is in the news forthis hour?
Saley    Give me a moment, I'm looking into that.
trishi7  (reply) sure                 -> thumbs-up
trishi7  (reply) take ur time         -> treated as a new question
Saley    (reply to "take ur time") One sec — I'm pulling this together.
Saley    (reply to the question) the news answer
Saley    (reply to "take ur time") Today's a Wednesday, so here's what's on the schedule: Scheduled for today
         (Wed 7 Oct): AI news post at 2 PM (already posted) … AI news since yesterday 2 PM: …
```

After (the replay in `tests/oct8_checks.py`, live; test mode is the same):

```
trishi7  @Saley what is in the news forthis hour?
Saley    Give me a moment, I'm looking into that.
trishi7  (reply) sure                 -> one reaction, no message
trishi7  (reply) take ur time         -> one reaction, no message
trishi7  (reply) sure                 -> one reaction, no message
trishi7  (reply) ok                   -> one reaction, no message
Saley    **AI News, Thu 8 Oct**  … five stories …
trishi7  (reply to the answer) thanks, no rush   -> one reaction, no message
```

Exactly one news answer; the bot said two things in all (the interim line and the answer); no model call.

### "What is the team working on today?"

Before (7 Oct, the model's words): "Today's a Wednesday, so here's what's on the schedule: Scheduled for today (Wed
7 Oct): AI news post at 2 PM (already posted) …", followed by the day's posts pasted in full.

After (fixture day Thu 15 Oct: a meeting note, channel messages, a P1 due Mon 19 Oct, a registration closing
Fri 16 Oct, a meeting tomorrow, a reminder):

```
**From today's meetings**
- Vaishnavi: send the pilot proposal to Acme Labs by Friday (Pipeline review)
- Sid: book the PolyAI demo (Pipeline review)

**Due soon**
- Tomorrow: meeting with Dev Shah (Borealis)
- Tomorrow: registration for Voice AI Summit closes
- Mon 19 Oct: MSA template (Legal)
- Mon 19 Oct: reminder: the pulse overview doc

**In the channel today**
- Sid: I'll call PolyAI about the quote tomorrow
- Vaishnavi: Please send the Wispr Flow deck to Trishi

**Also today**
- My posts today cover prospects to contact and next steps for connected contacts.
```

The note said "book the PolyAI demo at 4 PM" and the message "tomorrow at 11 AM": the times are taken out. The AI
news post was due that day too and is not in the last line. A day with nothing:

```
Nothing on for today that I can see: no meeting notes, nothing due in the next couple of days and nothing to pick up from the channel.
```

### The Next steps post

Before:

```
**Next steps**
@Vaishnavi
Where a few of our connections stand:
• Arjun Aryaa (Gnani.ai): Next Steps says Send email 1. Has it gone out? If so, mark 1st Email Sent and the date.
• Oliver Shoulson (PolyAI): Next Steps says Send email 1. Has it gone out? If so, mark 1st Email Sent and the date.
• Ariya Rastrow (Wisprflow.ai): Next Steps says Send email 2. Has it gone out? If so, mark 2nd Email Sent and the date.
```

After (through the real planner and sender):

```
**Next steps**
@Vaishnavi
Next Steps says Send email 1. Has it gone out? If so, mark 1st Email Sent and the date.
- Arjun Aryaa (Gnani.ai)
- Oliver Shoulson (PolyAI)
- Priya Rao (Acme Labs)
- Dev Shah (Borealis)

Next Steps says Send email 2. Has it gone out? If so, mark 2nd Email Sent and the date.
- Ariya Rastrow (Wisprflow.ai)
```

An "advance" group: `Email 1 has gone out. Time to set Next Steps to Send email 2.` then
`- Priya Rao (Acme Labs): sent 5 Oct`. **The opener line is dropped.** "done for Arjun" replied to the post still
gets Arjun's line; "sent" with nobody named still asks "Which one?".

## 4. Existing tests that pinned the old behaviour (updated, none deleted)

| Test | Before | After |
|---|---|---|
| `test_replies.py::test_is_ack_false` | six "sure"s is too long to be an acknowledgement (limit 5 words) | nine is (limit 8) |
| `test_replies.py::test_exact_texts` | `NOTHING_TODAY == "Nothing's due today."` | the new one-line text, and the "couldn't read" line |
| `o_objectives_text_is_what_the_posts_contain` | the answer is the day's posts, word for word | the posts are one line naming what they cover; no line of a real post is pasted; what is due soon is there; same forbidden-word, ping, model and write checks |
| `o_nothing_today_is_one_line` | a Saturday is the one line; unreadable send record gives "couldn't read" and nothing else | a Saturday has no posts line but still shows what is due in the next two working days; unreadable send record: the rest comes through and the last line names what is missing |
| `o_asked_through_on_message…` | the model writes a to-do part after the code's text; two model calls at most | the reply is the brief exactly; zero model, router and extractor calls; the tool still returns only a pointer |
| `e3b…`, `x6…`, `x7…`, `q4…`, `o6…` | read the pasted post text | read the one-line summary: a sent post still counts after the sheet changes; no ping and none of the post's text; a held-back or DM-bound post is not named |
| `x8…` | `OBJECTIVES_SHOW_OFFERS` is off | the constant is gone (nothing is pasted); the news types are excluded by type |
| `tests/test_rule13.py` (5 tests) | three openers; one bullet a person | no opener; groups, each ask once; what is personal after the colon; a call's group first |
| `verify_rule13.py` (c) | five bullets in sheet order under the day's opener | the same five people once each under their asks; **edited, not yet run** |
| `drip.py`, `wording.py`, `replies.py`, `clock.py` self-tests | old layout, old limits | new layout, new phrases, the expiry |

## 5. Not run, and not covered

- **Not run on the final tree:** the full pytest suite (stopped, see section 1) and every verify script other than
  `verify_replies_oct8.py`. In particular `verify_rule13.py` and `verify_replies.py` were edited or are affected and
  have not been run since.
- **Three edits were made after the last run of `verify_replies_oct8.py`:** the "couldn't read" last line
  (`wording.today_unread`), the removal of `OBJECTIVES_SHOW_OFFERS`, and an empty `history()` on the test channel.
  The 70-test subset ran after the first and third; nothing ran after the second except a compile.
- **Never run:** `verify_poc_lookup.py`, `verify_layouts.py`, `verify_simulation.py`, anything in the live channel.
- **Real meeting-note files.** The "today" scenario stands in for `notes.list_notes` / `notes.read_note`; the note
  parser itself is not exercised by it.
- **Real channel history.** `query.channel_recent_activity` is a stand-in in the scenario.
- **The to-do sheet** is off in the test world, so its line and its due items are covered only by `python -m today`.

## 6. Questions for the human

1. **The channel part quotes people; it does not summarise.** A message is shown when it has an action word in it
   ("I'll", "need to", "please", "by Friday", "tomorrow" …), in the person's own words, at most 5. Do you want a
   model to write that part instead? It would read better and could talk about the schedule again.
2. **Nothing overdue is in "Due soon"** (today through 2 working days, as specified). Should an overdue P1 be there?
3. **Only P1 deliverables**, as rule 4 does. Should P2 and P3 show here?
4. **The opener line is gone from the Next steps post.** Say if you want it back.
5. **A remark under "give me a moment" gets a thumbs-up** ("I am in a meeting till lunch"). Only a question is
   answered. Is that right?

## 7. Environment variables, and the lines for both `.env` files

- **NEW:** `TODAY_LOOKAHEAD_WORKING_DAYS` (default 2).
- **CHANGED default:** none. **RETIRED:** none.

Laptop `.env` and server `/opt/sales-bot/.env`, the same optional line (the default is what was asked for):

```
TODAY_LOOKAHEAD_WORKING_DAYS=2
```

The NFT2-1069 lines from `docs/test-reports/NFT2-1069.md` section 9 still apply to both files. Then, on the laptop,
`python tools/redact_env.py`. No database migration.

## 8. Live-channel checklist (not executed)

1. Ask something slow enough to get "Give me a moment…". Reply to that line with `take ur time`: a thumbs-up and
   no message. Then `ok no rush thanks`: the same. One answer arrives, to the question.
2. Reply to an interim line with `take ur time, also any news on ElevenLabs?`: that question is answered.
3. Reply `thanks, no rush` to the answer, and to a quiet line: a thumbs-up each.
4. Under a post that ends on an offer ("Want me to remind you again on Monday?") reply `no rush`: a thumbs-up, and
   a later `yes` on that post still sets the reminder.
5. `@Saley what is the team working on today?`: the groups that have something, no AI news, no time of day, no
   "scheduled", no rule number, nobody pinged. Compare "Due soon" with the sheet: P1 deliverables, registrations
   closing, reminders, meetings, from today through two working days.
6. Ask it before 2 PM and after: an answer both times.
7. The 3 PM Next steps post: heading, tags line, then each ask once with the names under it; a call group first
   when there is one; no opener line. Reply `done for <first name>`: that person's line comes back.
8. In the test channel: `make it Monday`, then `what time is it`. The next morning, `what time is it` again: the
   real date, and the log has one `[clock] the pretend day (...) has ended because the real day changed`.
